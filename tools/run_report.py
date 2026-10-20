"""Workload and trend report for a run (read-only; streams the event log, memory stays small).

Usage:
  python tools/run_report.py RUN_DIR [options]            a run launched by `fabctl launch` (run.json)
  python tools/run_report.py ATTEMPT_DIR ... [options]    attempt directories, oldest first
  python tools/run_report.py ATTEMPT_DIR --lineage        an attempt and its parents (process.json)
  python tools/run_report.py OUTPUT_DIR                   one run's output directory (events.jsonl, manifest.json)

Options:
  --window DAYS      report over the last DAYS complete simulated days (default 7)
  --top N            tool groups listed in the report (default 15; the CSV has all)
  --groups G,G       always list these groups
  --compare A0 A1    also show tool-group shares over simulated days [A0, A1)
  --csv DIR          write groups.csv, throughput.csv and samples.csv to DIR
  --out FILE         also write the report to FILE
  --state FILE       incremental state (default RUN_DIR/report/state.json for a run directory);
                     each call then reads only events added since the previous call
  --rescan           ignore the saved state and read everything again
  --steady-state     add a steady-state assessment (WIP, flow balance, cycle time, internals,
                     first PM cycles, bottleneck budget) with PASS/FAIL per criterion

Sections: throughput (releases, exits, WIP, cycle time per product), trends (slope with a 90%
confidence interval), workload of every tool group (processing, setup, PM, breakdown, idle share
of tool time; queue; queue wait), where the WIP is (waiting/processing, route fifth, area), and
long downtimes. Waiting lots include lots in transport to their next tool.

How incremental reading stays exact across resumes: a resumed attempt replays the events after
its checkpoint, so each attempt is counted only up to the event sequence where the next attempt
begins. The newest attempt is counted permanently only up to its latest checkpoint; events after
that are read for the report but not saved, because a later resume may replace them. If a run is
resumed from an earlier checkpoint than the state has already counted, the report rescans.
"""
import argparse, collections, copy, csv, hashlib, json, math, os, sys
from pathlib import Path

DAY = 86400e6
SAMPLE = 0.25 * DAY
LONG_DOWN = 12 * 3600e6
NEEDLES = (b'"lot_admitted"', b'"lot_exited"', b'"visit_entered"', b'"processing_started"', b'"unload_completed"',
           b'"maintenance_started"', b'"maintenance_recovered"', b'"service_completed"')
T90 = {1: 6.31, 2: 2.92, 3: 2.35, 4: 2.13, 5: 2.02, 6: 1.94, 7: 1.89, 8: 1.86, 9: 1.83, 10: 1.81,
       12: 1.78, 14: 1.76, 16: 1.75, 20: 1.72, 30: 1.70}
STATE_SCHEMA = 'run-report-state-v2'


def t90(df):
    return (T90.get(df) or T90[max(k for k in T90 if k <= df)]) if df >= 1 else float('inf')


def fit(xs, ys):
    """OLS slope and its 90% half-width."""
    n = len(xs)
    if n < 3:
        return float('nan'), float('inf')
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if not sxx:
        return float('nan'), float('inf')
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    res = sum((y - my - b * (x - mx)) ** 2 for x, y in zip(xs, ys))
    return b, t90(n - 2) * math.sqrt(res / (n - 2) / sxx)


def drop_cache(f, start, length):
    try:
        os.posix_fadvise(f.fileno(), start, length, os.POSIX_FADV_DONTNEED)
    except (OSError, AttributeError):
        pass


# ---------------------------------------------------------------- inputs

def sim(attempt):
    """The directory holding an attempt's events.jsonl (attempt/simulation, or the attempt itself)."""
    return attempt / 'simulation' if (attempt / 'simulation' / 'events.jsonl').exists() else attempt

def attempts_of(args):
    """Ordered attempt directories, and the default state file."""
    paths = [Path(p).resolve() for p in args.paths]
    if len(paths) == 1 and (paths[0] / 'run.json').exists():
        run = json.loads((paths[0] / 'run.json').read_text())
        return [Path(a) for a in run['attempts']], paths[0] / 'report' / 'state.json'
    if args.lineage:
        if len(paths) != 1:
            sys.exit('--lineage takes one attempt directory')
        chain, a = [], str(paths[0])
        while a and a not in chain:
            chain.append(a)
            try:
                a = json.loads((Path(a) / 'process.json').read_text()).get('parent_attempt')
            except (OSError, ValueError):
                break
        paths = [Path(a) for a in reversed(chain)]
    paths = [p for p in paths if (sim(p) / 'events.jsonl').exists() and (sim(p) / 'events.jsonl').stat().st_size]
    if not paths:
        sys.exit('no attempt with events found')
    return paths, None


class Model:
    def __init__(self, attempt):
        config_path = attempt / 'config.json'
        if config_path.exists():
            config = json.loads(config_path.read_text())
        else:  # an output directory: the manifest records the resolved configuration
            config = json.loads((sim(attempt) / 'manifest.json').read_text())['resolved_config']
        path = Path(config['inputs']['manufacturing_path'])
        if not path.is_absolute():
            path = (config_path.parent / path).resolve()
        raw = path.read_bytes()
        self.sha = hashlib.sha256(raw).hexdigest()
        m = json.loads(raw)['model']
        self.area = {g['id']: g.get('area') or '?' for g in m['tool_groups']}
        self.tools = {g['id']: g['source_count'] for g in m['tool_groups']}
        self.tools.update(config.get('generation', {}).get('counts', {}))
        # The topology the run used is authoritative for tool counts (generation defaults differ).
        try:
            artifact = json.loads((sim(attempt) / 'manifest.json').read_text())['topology_artifact']['path']
            counts = collections.Counter(e['tool_group_id'] for e in json.loads(Path(artifact).read_text())['model']['equipment']
                                         if e['kind'] == 'ordinary')
            if counts:
                self.tools = {g: counts.get(g, 0) for g in self.tools}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self.step = {}          # (route, source step) -> (route fifth, tool group)
        for r in m['routes']:
            n = len(r['steps'])
            for i, s in enumerate(r['steps']):
                self.step[(r['id'], s['source_step_id'])] = (min(4, int(5 * i / n)), s['tool_group_id'])
        self.stream = {x['id']: (x['product_id'], x['route_id']) for x in m['releases']}
        self.counter_pm = {x['id']: x for x in m['maintenance']
                           if x.get('type') == 'counter' and x.get('scope') == 'toolgroup'}


def first_sequence(attempt):
    with open(sim(attempt) / 'events.jsonl', 'rb') as f:
        return json.loads(f.readline())['event_sequence']


def committed_sequence(attempt):
    """Last event sequence saved in the attempt's newest checkpoint (None if it has none)."""
    latest = sim(attempt) / 'checkpoints' / 'latest.json'
    try:
        name = json.loads(latest.read_text())['manifest']
        return json.loads((latest.parent / name).read_text())['last_committed_sequence']
    except (OSError, ValueError, KeyError):
        return None


# ---------------------------------------------------------------- accumulation

def new_state(model_sha):
    return {'schema': STATE_SCHEMA, 'model_sha256': model_sha, 'read': {}, 'last_seq': -1, 'now_us': 0,
            'lots': {}, 'busy_end': {}, 'setup_end': {}, 'open_down': {}, 'recent_down': {},
            'buckets': {}, 'products': {}, 'samples': [], 'next_sample_us': None,
            'long_down': [], 'pm_tools': {}}


def add_span(state, group, kind, start, end):
    """Add [start, end) us of a tool group's time to its per-day buckets."""
    b = state['buckets'].setdefault(group, {})
    while start < end:
        day = int(start // DAY)
        stop = min(end, (day + 1) * DAY)
        row = b.setdefault(str(day), {})
        row[kind] = row.get(kind, 0) + (stop - start)
        start = stop


def minus_down(state, tool, start, end):
    """[start, end) without the tool's recorded downtimes (a breakdown can interrupt processing)."""
    parts = [(start, end)]
    for d0, d1 in state['recent_down'].get(tool, []):
        parts = [q for s, e in parts for q in ((s, min(e, d0)), (max(s, d1), e)) if q[1] > q[0]]
    return parts


def count(state, group, day, kind, value=1):
    row = state['buckets'].setdefault(group, {}).setdefault(str(day), {})
    row[kind] = row.get(kind, 0) + value


def product_day(state, product, day):
    return state['products'].setdefault(product, {}).setdefault(str(day), {'released': 0, 'exited': 0, 'ct': []})


def sample(state, model, t_us):
    c = collections.Counter()
    sections, areas, queues = collections.Counter(), collections.Counter(), collections.Counter()
    for product, route, admitted, step, status, entered in state['lots'].values():
        c['wip'] += 1
        if step is None:
            c['released'] += 1
            continue
        fifth, group = model.step.get((route, step), (None, None))
        c['waiting' if status == 'w' else 'processing'] += 1
        if fifth is not None:
            sections[f'{product} {fifth * 20}-{fifth * 20 + 20}%'] += 1
        if status == 'w' and group:
            queues[group] += 1
            areas[model.area.get(group, '?')] += 1
    state['samples'].append([t_us / DAY, c['wip'], c['waiting'], c['processing'], dict(sections), dict(areas), dict(queues)])


def lot_info(model, lot_id, payload=None):
    if payload and payload.get('product_id'):
        return payload['product_id'], payload['route_id']
    return model.stream.get(lot_id.split(':')[0], ('?', '?'))


def apply(state, model, e):
    t, et, p = e['simulation_time_us'], e['event_type'], e['payload']
    if state['next_sample_us'] is None:
        state['next_sample_us'] = math.ceil(t / SAMPLE) * SAMPLE
    while t >= state['next_sample_us']:
        sample(state, model, state['next_sample_us'])
        state['next_sample_us'] += SAMPLE
    state['now_us'] = t
    day = int(t // DAY)
    lots = state['lots']
    if et == 'lot_admitted':
        product, route = lot_info(model, p['lot_id'], p)
        lots[p['lot_id']] = [product, route, p['admitted_at_us'], None, 'w', None]
        product_day(state, product, day)['released'] += 1
    elif et == 'visit_entered':
        lot = lots.get(p['lot_id'])
        if lot is None:  # admitted before the first attempt read
            product, route = lot_info(model, p['lot_id'])
            lot = lots[p['lot_id']] = [product, route, None, None, 'w', None]
        lot[3], lot[4], lot[5] = p['source_step_id'], 'w', p['entered_at_us']
    elif et == 'processing_started':
        lot = lots.get(p['lot_id'])
        group = p['tool_id'].split(':tool:')[0]
        count(state, group, day, 'starts')
        if p.get('entered_at_us') is not None:
            count(state, group, day, 'wait_us', p['processing_started_at_us'] - p['entered_at_us'])
            count(state, group, day, 'waits')
        if lot is not None:
            lot[3], lot[4] = p['source_step_id'], 'p'
    elif et == 'unload_completed':
        tool = p['tool_id']
        start = max(p['processing_started_at_us'], state['busy_end'].get(tool, 0))
        for s, e in minus_down(state, tool, start, p['unloaded_at_us']):
            add_span(state, tool.split(':tool:')[0], 'processing_us', s, e)
        state['busy_end'][tool] = max(state['busy_end'].get(tool, 0), p['unloaded_at_us'])
    elif et == 'lot_exited':
        lot = lots.pop(p['lot_id'], None)
        product = lot[0] if lot else lot_info(model, p['lot_id'])[0]
        row = product_day(state, product, day)
        row['exited'] += 1
        if lot and lot[2] is not None:
            row['ct'].append(round((p['exited_at_us'] - lot[2]) / DAY, 3))
    elif et == 'maintenance_started':
        state['open_down'][p['id']] = [p['tool_id'], p['kind'], p['definition_id'], p['started_at_us']]
        if p['kind'] == 'pm' and p['definition_id'] in model.counter_pm:
            tools = state['pm_tools'].setdefault(p['definition_id'], [])
            if p['tool_id'] not in tools:
                tools.append(p['tool_id'])
    elif et == 'maintenance_recovered':
        tool, kind, definition, start = state['open_down'].pop(p['id'], [p['tool_id'], p['kind'], p['definition_id'], p['started_at_us']])
        end = p.get('recovered_at_us') or t
        add_span(state, tool.split(':tool:')[0], kind + '_us', start, end)
        # Keep a tool's downtimes of the last 3 days to exclude them from processing and setup.
        recent = [d for d in state['recent_down'].get(tool, []) if d[1] > t - 3 * DAY]
        state['recent_down'][tool] = recent + [[start, end]]
        if end - start >= LONG_DOWN:
            state['long_down'].append([tool, kind, definition, start / DAY, end / DAY])
    elif et == 'service_completed' and p.get('kind') == 'setup' and ':tool:' in p.get('owner_id', ''):
        owner = p['owner_id']
        tool = owner.split(':')[1] + ':tool:' + owner.split(':tool:')[1].split(':')[0]
        start = max(p['started_at_us'], state['setup_end'].get(tool, 0))
        for s, e in minus_down(state, tool, start, p['completed_at_us']):
            add_span(state, tool.split(':tool:')[0], 'setup_us', s, e)
        state['setup_end'][tool] = max(state['setup_end'].get(tool, 0), p['completed_at_us'])


def read(state, model, attempt, limit):
    """Apply the attempt's events after the saved offset up to sequence `limit` (None: to the end).
    Returns the byte offset after the last applied line."""
    key = str(attempt)
    offset = state['read'].get(key, 0)
    path = sim(attempt) / 'events.jsonl'
    with open(path, 'rb') as f:
        f.seek(offset)
        dropped = offset
        for line in f:
            if not line.endswith(b'\n'):
                break  # the writer's unfinished last line
            if limit is not None and b'"event_sequence":' in line:
                seq = int(line.split(b'"event_sequence":', 1)[1].split(b',', 1)[0])
                if seq > limit:
                    break
            offset += len(line)
            if any(n in line for n in NEEDLES):
                e = json.loads(line)
                if e['event_type'] in ('service_completed',) and e['payload'].get('kind') != 'setup':
                    continue
                apply(state, model, e)
                state['last_seq'] = e['event_sequence']
            if offset - dropped >= 1 << 28:
                drop_cache(f, dropped, offset - dropped)
                dropped = offset
        drop_cache(f, dropped, 0)
    state['read'][key] = offset
    return offset


def close_open(state, model):
    """Count downtimes still open at the end of the data (on a copy used only for the report)."""
    end = state['now_us']
    for tool, kind, definition, start in state['open_down'].values():
        add_span(state, tool.split(':tool:')[0], kind + '_us', start, end)
        if end - start >= LONG_DOWN:
            state['long_down'].append([tool, kind, definition, start / DAY, None])


def accumulate(attempts, model, state_path, rescan):
    state = None
    if state_path and state_path.exists() and not rescan:
        state = json.loads(state_path.read_text())
        if state.get('schema') != STATE_SCHEMA or state.get('model_sha256') != model.sha:
            state = None
    firsts = [first_sequence(a) for a in attempts]
    if state is not None:
        # A resume from a checkpoint older than what the state already counted: start over.
        for i, a in enumerate(attempts[1:], 1):
            if str(a) not in state['read'] and firsts[i] <= state['last_seq']:
                state = None
                break
    if state is None:
        state = new_state(model.sha)
    for i, a in enumerate(attempts[:-1]):
        read(state, model, a, firsts[i + 1] - 1)
    last = attempts[-1]
    committed = committed_sequence(last)
    if committed is not None and committed > state['last_seq']:
        read(state, model, last, committed)
    if state_path:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(state, separators=(',', ':')))
        os.replace(tmp, state_path)
    view = copy.deepcopy(state)
    read(view, model, last, None)  # the uncommitted tail, for this report only
    close_open(view, model)
    return view


# ---------------------------------------------------------------- report

def group_rows(state, model, d0, d1, span_days=None):
    rows = []
    span_days = span_days or d1 - d0
    for group, n in sorted(model.tools.items()):
        b = state['buckets'].get(group, {})
        tot = collections.Counter()
        for d in range(d0, d1):
            for k, v in b.get(str(d), {}).items():
                tot[k] += v
        cap = n * span_days * DAY
        if not cap:
            continue
        share = {k: tot[k + '_us'] / cap for k in ('processing', 'setup', 'pm', 'breakdown', 'injected')}
        load = sum(share.values())
        q = [s[6].get(group, 0) for s in state['samples'] if d0 <= s[0] < d1]
        rows.append({'group': group, 'area': model.area.get(group, '?'), 'tools': n,
                     'processing': share['processing'], 'setup': share['setup'], 'pm': share['pm'],
                     'breakdown': share['breakdown'] + share['injected'], 'load': load, 'idle': max(0.0, 1 - load),
                     'starts_per_day': tot['starts'] / span_days,
                     'mean_queue': sum(q) / len(q) if q else 0.0, 'final_queue': q[-1] if q else 0,
                     'mean_wait_h': tot['wait_us'] / tot['waits'] / 3600e6 if tot['waits'] else 0.0})
    return sorted(rows, key=lambda r: -r['load'])


def pct(x):
    return f'{100 * x:5.1f}'


def report(state, model, args, attempts):
    out = []
    w = out.append
    end = state['now_us'] / DAY
    d1 = int(end)
    d0 = max(0, d1 - int(args.window))
    span = d1 - d0
    if d1 <= d0:  # shorter than a day: report the partial first day
        d0, d1, span = 0, 1, end
        if not span:
            sys.exit('no simulated time in the events yet')
    samples = [s for s in state['samples'] if d0 <= s[0] < d1]
    w(f'# Run report — simulated day {end:.2f}')
    w(f'Window: days {d0}-{min(d1, end):g} ({span:g} days); {len(attempts)} attempt(s), newest {attempts[-1].name}\n')

    # Throughput
    w('## Throughput')
    w('| Product | released/day | exited/day | exit/release | cycle time mean / p50 / p90 (d) |')
    w('| --- | --- | --- | --- | --- |')
    for product in sorted(state['products']):
        days = state['products'][product]
        rel = sum(days.get(str(d), {}).get('released', 0) for d in range(d0, d1))
        ex = sum(days.get(str(d), {}).get('exited', 0) for d in range(d0, d1))
        cts = sorted(c for d in range(d0, d1) for c in days.get(str(d), {}).get('ct', []))
        ct = (f'{sum(cts) / len(cts):.1f} / {cts[len(cts) // 2]:.1f} / {cts[min(len(cts) - 1, int(0.9 * len(cts)))]:.1f}'
              if cts else '—')
        w(f"| {product} | {rel / span:.1f} | {ex / span:.1f} | {f'{ex / rel:.2f}' if rel else '—'} | {ct} |")
    if samples:
        w(f'\nWIP {samples[0][1]} → {samples[-1][1]} lots (waiting {samples[-1][2]}, processing {samples[-1][3]})\n')

    # Trends
    w('## Trends (slope per simulated day, 90% CI)')
    series = {'WIP (lots)': [(s[0], s[1]) for s in samples],
              'waiting lots': [(s[0], s[2]) for s in samples],
              'processing lots': [(s[0], s[3]) for s in samples]}
    for product in sorted(state['products']):
        pts = []
        for d in range(d0, d1):
            cts = state['products'][product].get(str(d), {}).get('ct', [])
            if cts:
                pts.append((d + 0.5, sum(cts) / len(cts)))
        series[f'{product} cycle time (d)'] = pts
    trends = {}
    for name, pts in series.items():
        s, hw = fit([p[0] for p in pts], [p[1] for p in pts])
        mean = sum(p[1] for p in pts) / len(pts) if pts else float('nan')
        trends[name] = (s, hw, mean, pts)
        if not pts:
            w(f'- {name}: no data')
        elif math.isnan(s):
            w(f'- {name}: mean {mean:.1f}, slope — (too few points)')
        else:
            w(f'- {name}: mean {mean:.1f}, slope {s:+.2f} (CI {s - hw:+.2f} to {s + hw:+.2f})')
    w('')

    # Workload
    rows = group_rows(state, model, d0, d1, span)
    chosen = rows[:args.top] + [r for r in rows[args.top:] if r['group'] in args.groups]
    w(f'## Tool-group workload (share of tool time, %; top {args.top} by load of {len(rows)} groups)')
    w('| Group | Area | Tools | Processing | Setup | PM | Breakdown | Load | Idle | Starts/day | Queue mean / end | Wait h |')
    w('| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |')
    for r in chosen:
        w(f"| {r['group']} | {r['area']} | {r['tools']} | {pct(r['processing'])} | {pct(r['setup'])} | {pct(r['pm'])} | "
          f"{pct(r['breakdown'])} | {pct(r['load'])} | {pct(r['idle'])} | {r['starts_per_day']:.1f} | "
          f"{r['mean_queue']:.1f} / {r['final_queue']} | {r['mean_wait_h']:.1f} |")
    if args.compare:
        a0, a1 = int(args.compare[0]), int(args.compare[1])
        before = {r['group']: r for r in group_rows(state, model, a0, a1)}
        w(f'\nCompared with days {a0}-{a1} (load and mean queue):')
        for r in chosen:
            b = before.get(r['group'])
            if b:
                w(f"- {r['group']}: load {pct(b['load'])} → {pct(r['load'])}, queue {b['mean_queue']:.1f} → {r['mean_queue']:.1f}")
    w('')

    # Where the WIP is
    w('## Where the WIP is (mean lots over the window)')
    if samples:
        for title, idx in (('Route section', 4), ('Waiting by area', 5)):
            tot = collections.Counter()
            for s in samples:
                tot.update(s[idx])
            w(f'- {title}: ' + ', '.join(f'{k} {v / len(samples):.0f}' for k, v in sorted(tot.items(), key=lambda x: -x[1])))
    w('')

    # Long downtimes
    longs = [x for x in state['long_down'] if (x[4] or end) > d0]
    per = collections.Counter(x[0].split(':tool:')[0] for x in longs)
    w(f'## Downtimes of 12 h or longer in the window: {len(longs)}')
    w('- most per group: ' + ', '.join(f'{g} {n}' for g, n in per.most_common(10)))
    w('- longest:')
    for tool, kind, definition, s0, e0 in sorted(longs, key=lambda x: (x[4] or end) - x[3])[-10:][::-1]:
        w(f"- {tool}: {kind} {definition}, day {s0:.2f} → {'open' if e0 is None else f'{e0:.2f}'} "
          f"({24 * ((e0 or end) - s0):.0f} h)")
    w('')

    if args.steady_state:
        steady(state, model, rows, trends, samples, d0, d1, w)
    return '\n'.join(out), rows


def steady(state, model, rows, trends, samples, d0, d1, w):
    """Opt-in steady-state criteria (the thresholds are this project's choice, not a standard)."""
    span = d1 - d0
    verdict = {}
    w('## Steady-state assessment')
    s, hw, mean, pts = trends['WIP (lots)']
    halves = [p[1] for p in pts]
    h = len(halves) // 2
    m1 = sum(halves[:h]) / h if h else float('nan')
    m2 = sum(halves[h:]) / (len(halves) - h) if halves else float('nan')
    ok = len(pts) >= 4 and abs(s) <= 3 and s - hw <= 0 <= s + hw and abs(m2 - m1) / m1 < 0.01
    verdict['1 WIP trend'] = ok
    w(f'- 1 WIP trend: slope {s:+.1f}/day (CI {s - hw:+.1f} to {s + hw:+.1f}), half-window means {m1:.0f} → {m2:.0f} — '
      f'{"PASS" if ok else "FAIL"} (|slope| ≤ 3, CI contains 0, halves within 1%)')
    ok = True
    for product in sorted(state['products']):
        rel = sum(state['products'][product].get(str(d), {}).get('released', 0) for d in range(d0, d1))
        ex = sum(state['products'][product].get(str(d), {}).get('exited', 0) for d in range(d0, d1))
        ok &= bool(rel) and abs(ex / rel - 1) <= 0.03
    verdict['2 flow balance'] = ok
    w(f'- 2 flow balance: {"PASS" if ok else "FAIL"} (exits within ±3% of releases for every product)')
    ok = True
    for name, (s, hw, mean, pts) in trends.items():
        if name.endswith('cycle time (d)'):
            ok &= len(pts) >= 3 and (abs(s) * span < 0.03 * mean or s - hw <= 0 <= s + hw)
    verdict['3 cycle time'] = ok
    w(f'- 3 cycle time: {"PASS" if ok else "FAIL"} (change over the window < 3% of mean, or CI contains 0)')
    key = [r['group'] for r in rows[:7]]
    ok = True
    for name, idx in (('waiting', 2), ('processing', 3)):
        pts = [(x[0], x[idx]) for x in samples]
        s, hw = fit([p[0] for p in pts], [p[1] for p in pts])
        mean = sum(p[1] for p in pts) / len(pts) if pts else 0
        ok &= abs(s) * span <= max(0.05 * mean, 3) or s - hw <= 0 <= s + hw
    for g in key:
        pts = [(x[0], x[6].get(g, 0)) for x in samples]
        s, hw = fit([p[0] for p in pts], [p[1] for p in pts])
        mean = sum(p[1] for p in pts) / len(pts) if pts else 0
        ok &= abs(s) * span <= max(0.05 * mean, 3) or s - hw <= 0 <= s + hw
    verdict['4 internals'] = ok
    w(f'- 4 internals: {"PASS" if ok else "FAIL"} (waiting, processing and queues of the 7 most loaded groups change '
      f'≤ max(5% of mean, 3 lots) over the window, or CI contains 0)')
    pending = []
    for pid, m in model.counter_pm.items():
        hours = float(m['duration']['mean_seconds']) / 3600
        n, done = model.tools.get(m['scope_id'], 0), len(state['pm_tools'].get(pid, []))
        if hours >= 24 and done < n and m['scope_id'] in key:
            pending.append(f"{pid} ({done}/{n} tools)")
    verdict['5 first PM cycle'] = not pending
    w(f'- 5 first long PM cycle on the loaded groups: {"PASS" if not pending else "OPEN: " + ", ".join(pending)}')
    ok = True
    for r in rows[:7]:
        limit = 0.90 if r['tools'] >= 10 else 0.85 if r['tools'] >= 5 else 0.80
        ok &= r['load'] <= limit
    verdict['6 bottleneck budget'] = ok
    w(f'- 6 bottleneck budget: {"PASS" if ok else "FAIL"} (load of the 7 most loaded groups ≤ 90% / 85% / 80% '
      f'for ≥ 10 / 5-9 / ≤ 4 tools)')
    w(f'- Overall: {"STEADY" if all(verdict.values()) else "NOT STEADY"} '
      f'({sum(verdict.values())}/{len(verdict)} criteria pass)\n')


def write_csv(state, rows, directory):
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / 'groups.csv', 'w', newline='') as f:
        out = csv.DictWriter(f, fieldnames=list(rows[0]))
        out.writeheader()
        out.writerows(rows)
    with open(directory / 'throughput.csv', 'w', newline='') as f:
        out = csv.writer(f)
        out.writerow(['day', 'product', 'released', 'exited', 'ct_mean_d', 'ct_p50_d', 'ct_p90_d'])
        for product, days in sorted(state['products'].items()):
            for d in sorted(days, key=int):
                cts = sorted(days[d]['ct'])
                out.writerow([d, product, days[d]['released'], days[d]['exited'],
                              f'{sum(cts) / len(cts):.3f}' if cts else '', f'{cts[len(cts) // 2]:.3f}' if cts else '',
                              f'{cts[min(len(cts) - 1, int(0.9 * len(cts)))]:.3f}' if cts else ''])
    sections = sorted({k for s in state['samples'] for k in s[4]})
    areas = sorted({k for s in state['samples'] for k in s[5]})
    with open(directory / 'samples.csv', 'w', newline='') as f:
        out = csv.writer(f)
        out.writerow(['day', 'wip', 'waiting', 'processing'] + [f'section {k}' for k in sections] + [f'waiting {k}' for k in areas])
        for s in state['samples']:
            out.writerow([f'{s[0]:.2f}', s[1], s[2], s[3]] + [s[4].get(k, 0) for k in sections] + [s[5].get(k, 0) for k in areas])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('paths', nargs='+')
    ap.add_argument('--lineage', action='store_true')
    ap.add_argument('--window', type=float, default=7)
    ap.add_argument('--top', type=int, default=15)
    ap.add_argument('--groups', type=lambda s: s.split(','), default=[])
    ap.add_argument('--compare', nargs=2, type=float)
    ap.add_argument('--csv', type=Path)
    ap.add_argument('--out', type=Path)
    ap.add_argument('--state', type=Path)
    ap.add_argument('--rescan', action='store_true')
    ap.add_argument('--steady-state', action='store_true')
    args = ap.parse_args()
    attempts, default_state = attempts_of(args)
    model = Model(attempts[-1])
    state = accumulate(attempts, model, args.state or default_state, args.rescan)
    text, rows = report(state, model, args, attempts)
    print(text)
    if args.out:
        args.out.write_text(text + '\n')
    if args.csv:
        write_csv(state, rows, args.csv)


if __name__ == '__main__':
    main()
