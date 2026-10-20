"""Area-clustered, flow- and balance-aware assignment of tools to SMAT bay columns.

Layout policy smat_area_flow_v1 (docs/layout.md). The SMAT
geometry (rails, bays, columns, buffers) is kept; tool placement is derived for
the requested equipment, not inherited from the source (Dataset 4) partitions:
  - Tools are assigned to bay columns, limited only by each column's free
    segments (exact first-fit of bodies with clearance) and width; no per-column
    tool count and no source area labels.
  - Lexicographic score: (area mixing = tools outside their column's and bay's
    majority area, flow-weighted loaded travel between columns + balance weight
    * travel0 * S / S0, with S the sum of squared expected bay loads).
  - From the reference packing (feasible), parallel rounds score all single-tool
    moves and two-tool swaps; improving candidates are applied in order, each
    re-scored on the current layout, until no candidate improves.
Each area's physical extent is the result of where its tools land.
"""
from collections import defaultdict, Counter
from fractions import Fraction


_SEARCH = None


def _scan(chunk):
    """Worker: score a chunk of candidates against the forked search state."""
    st = _SEARCH; out = []
    for cand in chunk:
        result = st.score(cand)
        if result is not None and (result[0] < 0 or (result[0] == 0 and result[1] < -1e-9)):
            out.append((result[0], result[1], cand))
    return out


class _Search:
    def __init__(self, sites, instances, graph, costs, clearance_um, tolerance, tool_load, balance_weight, spans, initial):
        columns = defaultdict(list)
        for s in sites:
            columns[(s['bay_id'], s['x_um'])].append(s)
        self.keys = keys = sorted(columns)
        tol = Fraction(tolerance)
        # Exact packer feasibility: first-fit of bodies (tallest first) into the
        # column's free segments, with clearance between bodies in a segment.
        self.segments = {k: spans[k] for k in columns}
        self.width = {k: max(s['width_um'] for s in members) * (1 + tol) for k, members in columns.items()}
        rep = {k: sorted(members, key=lambda s: (s['y_um'], s['id']))[len(members) // 2]['id'] for k, members in columns.items()}
        self.C = {(a, b): costs[(rep[a], rep[b])] for a in keys for b in keys}
        self.bay = {k: k[0] for k in keys}
        self.inst = {e['id']: e for e in instances}
        self.group_of = {e['id']: e['tool_group_id'] for e in instances}
        self.area_of = {e['id']: e['area'] for e in instances}
        self.n_group = Counter(self.group_of.values())
        self.clearance = clearance_um; self.tool_load = tool_load
        self.edges = [(a, b, float(Fraction(w))) for (a, b), w in sorted(graph.items())
                      if a != b and a in self.n_group and b in self.n_group]
        self.edges_of = defaultdict(list)
        for edge in self.edges:
            self.edges_of[edge[0]].append(edge); self.edges_of[edge[1]].append(edge)
        self.count = defaultdict(Counter); self.col_area = defaultdict(Counter); self.bay_area = defaultdict(Counter)
        self.members = defaultdict(set); self.placed = {}; self.at = defaultdict(set); self.bay_load = Counter()
        for e in instances:
            if e['width_um'] > self.width[initial[e['id']]]:
                raise ValueError(f"area placement: initial column too narrow for {e['id']}")
            self.put(e['id'], initial[e['id']])
        if not all(self.packs(k) for k in keys):
            raise ValueError('area placement: initial assignment does not pack')
        self.terms = {x: self.edge_term(x) for x in self.edges}
        self.travel0 = sum(self.terms.values()); self.s0 = sum(v * v for v in self.bay_load.values())
        self.lam = float(Fraction(balance_weight)) * self.travel0 / self.s0 if self.s0 and balance_weight else 0.0
        self.mix0 = self.mixing(keys)

    def put(self, eid, k):
        g = self.group_of[eid]; a = self.area_of[eid]; b = self.bay[k]
        self.count[g][k] += 1; self.col_area[k][a] += 1; self.bay_area[b][a] += 1
        self.members[k].add(eid); self.bay_load[b] += self.tool_load[g]
        self.placed[eid] = k; self.at[(g, k)].add(eid)

    def take(self, eid):
        k = self.placed.pop(eid); g = self.group_of[eid]; a = self.area_of[eid]; b = self.bay[k]
        self.count[g][k] -= 1; self.col_area[k][a] -= 1; self.bay_area[b][a] -= 1
        self.members[k].discard(eid); self.bay_load[b] -= self.tool_load[g]
        self.at[(g, k)].discard(eid)
        return k

    def packs(self, k):
        used = [0] * len(self.segments[k]); jobs = [0] * len(self.segments[k])
        for eid in sorted(self.members[k], key=lambda e: (-self.inst[e]['height_um'], self.group_of[e], e)):
            h = self.inst[eid]['height_um']
            for i, cap in enumerate(self.segments[k]):
                extra = h + h % 2 + (self.clearance if jobs[i] else 0)
                if used[i] + extra <= cap:
                    used[i] += extra; jobs[i] += 1; break
            else:
                return False
        return True

    def mixing(self, cols):
        # Minority tools: tools not in their column's (and bay's) majority area.
        bays = {self.bay[k] for k in cols}
        return (sum(sum(self.col_area[k].values()) - max(self.col_area[k].values(), default=0) for k in cols)
                + sum(sum(self.bay_area[b].values()) - max(self.bay_area[b].values(), default=0) for b in bays))

    def edge_term(self, edge):
        a, b, w = edge; C = self.C
        dest = [d for d, n in self.count[b].items() if n]
        if not dest:
            return 0.0
        return w / self.n_group[a] * sum(n * min(C[(c, d)] for d in dest) for c, n in self.count[a].items() if n)

    def _ops(self, cand):
        if cand[0] == 'move':
            _, g, src, k = cand
            if not self.count[g][src]:return None
            eid = min(self.at[(g, src)])
            if self.inst[eid]['width_um'] > self.width[k]:return None
            return (g,), (src, k), (lambda: (self.take(eid), self.put(eid, k))), (lambda: (self.take(eid), self.put(eid, src)))
        _, g, src, h, k = cand
        if not self.count[g][src] or not self.count[h][k]:return None
        eid = min(self.at[(g, src)]); other = min(self.at[(h, k)])
        def apply():
            self.take(eid); self.take(other); self.put(eid, k); self.put(other, src)
        def undo():
            self.take(eid); self.take(other); self.put(eid, src); self.put(other, k)
        return (g, h), (src, k), apply, undo

    def score(self, cand, keep=False):
        ops = self._ops(cand)
        if ops is None:return None
        groups, cols, apply, undo = ops
        affected = {x for g in groups for x in self.edges_of[g]}
        before = sum(self.terms[x] for x in affected)
        bays = {self.bay[k] for k in cols}
        sq_before = sum(self.bay_load[b] ** 2 for b in bays); mix_before = self.mixing(cols)
        apply()
        if not all(self.packs(k) for k in cols):
            undo();return None
        after = {x: self.edge_term(x) for x in affected}
        sq_after = sum(self.bay_load[b] ** 2 for b in bays); mix_after = self.mixing(cols)
        result = (mix_after - mix_before, sum(after.values()) - before + self.lam * (sq_after - sq_before))
        if keep and (result[0] < 0 or (result[0] == 0 and result[1] < -1e-9)):
            self.terms.update(after);return result
        undo()
        return result if not keep else None

    def candidates(self):
        units = sorted((g, k) for g in self.count for k, n in self.count[g].items() if n)
        out = [('move', g, src, k) for g, src in units for k in self.keys if k != src]
        out += [('swap', g, src, h, k) for i, (g, src) in enumerate(units) for h, k in units[i + 1:]
                if h != g and k != src]
        return out


def assign_columns(sites, instances, graph, costs, *, clearance_um, tolerance,
                   tool_load, balance_weight, spans, initial, max_rounds=500, workers=None):
    """Instance id -> column key (bay_id, x_um), plus a search history.

    Columns are limited only by free span (times 1 + tolerance); no per-column
    tool count and no source area labels. From a feasible start, rounds of
    parallel candidate scoring (moves of one tool, swaps of two tools of
    different groups) are followed by sequential application of improving
    candidates, each re-scored on the current layout. Lexicographic score:
    (area mixing = sum over columns and bays of distinct areas - 1,
     flow-weighted travel + balance). Each area's extent is the result."""
    import multiprocessing as mp, os
    global _SEARCH
    st = _Search(sites, instances, graph, costs, clearance_um, tolerance, tool_load, balance_weight, spans, initial)
    history = {'initial_travel': st.travel0, 'initial_bay_squares': st.s0, 'initial_mixing': st.mix0,
               'applied': 0, 'rounds': 0}
    workers = workers or max(1, min(24, (os.cpu_count() or 2) - 4))
    ctx = mp.get_context('fork')
    for rnd in range(max_rounds):
        history['rounds'] = rnd + 1
        cands = st.candidates()
        _SEARCH = st
        size = max(1, len(cands) // (workers * 8))
        chunks = [cands[i:i + size] for i in range(0, len(cands), size)]
        with ctx.Pool(workers) as pool:
            found = [x for part in pool.imap_unordered(_scan, chunks) for x in part]
        if not found:
            break
        applied = 0
        for _, _, cand in sorted(found, key=lambda x: (x[0], x[1], x[2])):
            if st.score(cand, keep=True) is not None:
                applied += 1
        history['applied'] += applied
        import sys, time
        print(f'area placement round {rnd + 1}: candidates {len(cands)} improving {len(found)} applied {applied} '
              f'mixing {st.mixing(st.keys)} travel {sum(st.terms.values()):.4g} at {time.strftime("%H:%M:%S")}',
              file=sys.stderr, flush=True)
        if not applied:
            break
    _SEARCH = None
    history.update(final_travel=sum(st.terms.values()), final_bay_squares=sum(v * v for v in st.bay_load.values()),
                   final_mixing=st.mixing(st.keys), initial_mixing_metric='minority_tools_v1', balance_weight=str(balance_weight), tolerance=str(tolerance),
                   termination='local_optimum' if rnd + 1 < max_rounds else 'round_limit')
    return dict(st.placed), history


def balance_sections(sites, instances, graph, costs, *, clearance_um, tolerance, tool_load, spans,
                     columns_for, window_um, max_swaps=40):
    """Whole-bay permutation that lowers the peak handling load along the spines.

    Bays keep their tool sets intact (every column's tools move together to the
    same-position column of the other bay), so areas stay whole; only which bay
    position a tool set occupies changes. Only bays with identical column
    geometry are exchanged, and every exchange must still pack. Section load:
    the sum of expected bay loads (tool_load) of all bays whose centre lies in an
    x-window of window_um (bays on both sides of a spine share it). Greedy,
    deterministic: each step applies the exchange that most lowers the peak
    window (then the sum of squared windows), until none does or max_swaps."""
    st = _Search(sites, instances, graph, costs, clearance_um, tolerance, tool_load, 0, spans, columns_for)
    cols = defaultdict(list)
    for k in st.keys:cols[k[0]].append(k)
    for b in cols:cols[b].sort(key=lambda k: k[1])
    lo = {b: min(k[1] for k in ks) for b, ks in cols.items()}
    centre = {b: (lo[b] + max(k[1] for k in ks)) / 2 for b, ks in cols.items()}
    shape = {b: tuple((k[1] - lo[b], round(st.width[k])) for k in ks) for b, ks in cols.items()}
    xs = range(int(min(centre.values())) - window_um, int(max(centre.values())) + 1, max(1, window_um // 10))
    def windows():
        load = st.bay_load
        return sorted((sum(load[b] for b in cols if x <= centre[b] < x + window_um) for x in xs), reverse=True)
    def key():
        w = windows();return (w[0], sum(v * v for v in w))
    def exchange(p, q):
        moved = []
        for kp, kq in zip(cols[p], cols[q]):
            for eid in sorted(st.members[kp]):moved.append((eid, kq))
            for eid in sorted(st.members[kq]):moved.append((eid, kp))
        for eid, _ in moved:st.take(eid)
        for eid, k in moved:st.put(eid, k)
        return all(st.inst[e]['width_um'] <= st.width[k] for e, k in moved) and all(st.packs(k) for k in cols[p] + cols[q])
    start = key(); current = start; swaps = []
    pairs = [(p, q) for p in sorted(cols) for q in sorted(cols) if p < q and shape[p] == shape[q]]
    while len(swaps) < max_swaps:
        best = None
        for p, q in pairs:
            ok = exchange(p, q)
            if ok:
                k = key()
                if k < current and (best is None or k < best[0]):best = (k, p, q)
            exchange(p, q)
        if best is None:break
        current, p, q = best; exchange(p, q); swaps.append([p, q])
    st.terms = {x: st.edge_term(x) for x in st.edges}
    history = {'window_um': window_um, 'swaps': swaps, 'initial_peak_load': start[0], 'final_peak_load': current[0],
               'mean_window_load': sum(st.bay_load.values()) * window_um / max(1, (max(centre.values()) - min(centre.values()) + window_um)),
               'final_travel': sum(st.terms.values()), 'compatible_pairs': len(pairs)}
    return dict(st.placed), history
