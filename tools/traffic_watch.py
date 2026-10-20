"""Watch a run for traffic breakdown by working-vehicle blocked time, not record counts.
Per 30 sim-min window: deliveries and blocked seconds of vehicles on pickup/drop tasks.
Prints one status line per window, 'BREAKDOWN day D' when blocked s/delivery > LIMIT in two
consecutive completed windows, 'RECOVERED day D' at the first window back under LIMIT (the
watch continues), and 'READY day D' (and exits) past TARGET_DAY.

Usage: python tools/traffic_watch.py SOURCE TARGET_DAY [LIMIT_S] [--follow]
  SOURCE: a run directory (run.json: its newest attempt), an attempt directory, a simulation
  directory, or a current_live.json written by ops/fabctl (its current attempt).
  Without --follow: one pass over the log as it is now. With --follow: keep reading new lines
  every 2 minutes until TARGET_DAY; when the newest attempt changes (resume or recovery) the new
  attempt's oht.jsonl is read from its start and the window state is reset.
  LIMIT_S (default 60) is the working-blocked seconds per delivery that counts as a breakdown.
  Cost: well under 1% of one core and a few MB of memory; consumed file ranges are dropped from
  the page cache."""
import collections, json, os, sys, time

W = 1800e6


def drop_cache(f, start, length):
    """Drop clean cached pages of the range already consumed (dirty tail pages stay)."""
    try:
        os.posix_fadvise(f.fileno(), start, length, os.POSIX_FADV_DONTNEED)
    except (OSError, AttributeError):
        pass


def resolve(source):
    """oht.jsonl of the newest attempt behind SOURCE (None if it cannot be read yet)."""
    try:
        if os.path.isfile(source):
            with open(source) as f:
                source = json.load(f)['attempt']
        elif os.path.exists(os.path.join(source, 'run.json')):
            with open(os.path.join(source, 'run.json')) as f:
                attempts = json.load(f)['attempts']
            if not attempts:
                return None
            source = attempts[-1]
        for candidate in (os.path.join(source, 'simulation', 'oht.jsonl'), os.path.join(source, 'oht.jsonl')):
            if os.path.exists(candidate):
                return candidate
    except (OSError, ValueError, KeyError):
        pass
    return None


def main():
    args = [a for a in sys.argv[1:] if a != '--follow']
    if len(args) < 2:
        sys.exit(__doc__)
    live = '--follow' in sys.argv[1:]
    source, target = args[0], float(args[1])
    limit = float(args[2]) if len(args) > 2 else 60
    path = None
    while True:
        new = resolve(source)
        if new != path:
            if path is not None:
                print(f'ATTEMPT {new}', flush=True)
            path, pos, last, over = new, 0, 0, 0
            blk, deliv, bsec, done = {}, collections.Counter(), collections.Counter(), set()
        if path and os.path.exists(path):
            with open(path, 'rb') as f:
                f.seek(pos); dropped = pos
                for line in f:
                    if not line.endswith(b'\n'):
                        break
                    pos += len(line)
                    if pos - dropped >= 1 << 28:
                        drop_cache(f, dropped, pos - dropped); dropped = pos
                    e = json.loads(line); ts = e['simulation_time_us']; last = ts
                    p = e['payload']; v = p.get('vehicle_id'); k = e['event_type']
                    if v in blk and k != 'oht_blocked':
                        s0 = blk.pop(v); bsec[int(s0 // W)] += (ts - s0) / 1e6
                    if k == 'oht_blocked' and p.get('task') in ('pickup', 'drop') and v not in blk:
                        blk[v] = ts
                    elif k == 'oht_unload_completed':
                        deliv[int(ts // W)] += 1
                drop_cache(f, dropped, pos - dropped)
            for w in sorted(x for x in deliv if (x + 1) * W < last and x not in done):
                done.add(w); per = bsec[w] / max(deliv[w], 1)
                print(f'day {w * W / 86400e6:7.3f} deliveries {deliv[w]:4d} working-blocked {per:6.1f} s/delivery', flush=True)
                if per > limit:
                    over += 1
                    if over == 2:
                        print(f'BREAKDOWN day {w * W / 86400e6:.3f}', flush=True)
                else:
                    if over >= 2:
                        print(f'RECOVERED day {w * W / 86400e6:.3f}', flush=True)
                    over = 0
            if last >= target * 86400e6:
                print(f'READY day {last / 86400e6:.3f}', flush=True); sys.exit(0)
            if not live:
                sys.exit(0)  # offline: one pass over a finished log
        time.sleep(120)


if __name__ == '__main__':
    main()
