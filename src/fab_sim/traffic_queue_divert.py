"""Persistent-queue divert (declared project policy, docs/settings.md).

An assigned vehicle that has waited at least WAIT_US behind vehicles that are
themselves waiting (a stuck queue, not a vehicle loading at a port) takes a free
alternative branch at a diverge on its next hop, then continues to the same
destination. This keeps vehicles from joining queues that close wait-for rings
in the SMAT mesh (bays link to neighbour bays and to both interbays).

Each wait period is examined once per WAIT_US; the attempt key is stored on the
request (checkpointed), so resumed runs make the same decisions. A movement is
amended at most MAX_AMENDMENTS times (shared with cycle recovery) and a detour
may add at most MAX_EXTRA_S seconds of free-flow travel.
"""
from .geometry import swept_intervals

POLICY = 'persistent-queue-divert-v1'
WAIT_US = 60_000_000
MAX_AMENDMENTS = 8
MAX_EXTRA_S = 900
WAYPOINT_SEARCH_RAILS = 24


def _successors(t):
    graph = getattr(t, '_queue_divert_graph', None)
    if graph is None:
        out = {}
        for rid, rail in t.rails.items():
            out.setdefault(rail['from_node'], []).append(rid)
        graph = t._queue_divert_graph = {node: tuple(sorted(rids)) for node, rids in out.items()}
    return graph


def _waypoint(t, rail_id):
    """First holding on rail_id or, following single successors, beyond it."""
    graph = _successors(t); rid = rail_id
    for _ in range(WAYPOINT_SEARCH_RAILS):
        hs = t.holding_by_rail.get(rid)
        if hs: return hs[0][1]
        nxt = graph.get(t.rails[rid]['to_node'], ())
        if len(nxt) != 1: return None
        rid = nxt[0]
    return None


def _stuck(t, r, now):
    if not r['blockers'] or now - r['waiting_since_us'] < WAIT_US: return False
    for b in r['blockers']:
        if b['kind'] != 'rail': return False
        other = t.requests.get(t.active.get(b['owner']))
        if b['owner'] in t.hops or other is None or other['status'] != 'pending' or not other['blockers']:
            return False
    return True


def divert_queues(t):
    """Deadline check: divert stuck assigned vehicles at a diverge. True if any route changed."""
    from .traffic_cycle_recovery import _movement, amend_route
    now = t.scheduler.now_us; probes = getattr(t.transfers, 'probes', None) or getattr(t, 'probes', None)
    stuck = sorted((vid, rid) for vid, rid in t.active.items()
                   if t.requests[rid]['status'] == 'pending' and _stuck(t, t.requests[rid], now))
    if not stuck: return False
    graph = _successors(t); changed = False; claims = None
    for vid, rid in stuck:
        r = t.requests[rid]
        if r['status'] != 'pending' or not _stuck(t, r, now): continue
        key = [r['waiting_since_us'], (now - r['waiting_since_us']) // WAIT_US]
        if r.get('queue_divert_key') == key: continue
        if len(r.get('route_recovery', {}).get('recovery_poses', [])) >= MAX_AMENDMENTS: continue
        bound = _movement(t, r, allow_amended=True)
        if bound is None: continue
        r['queue_divert_key'] = key
        v = t.vehicles[vid]
        first, _, _ = t._next_hop(r)
        # Diverges inside the blocked first hop: (index of the part after the node, node).
        forks = [(i + 1, t.rails[p['rail_id']]['to_node']) for i, p in enumerate(first[:-1])
                 if p['end_um'] == t.rails[p['rail_id']]['length_um']
                 and len(graph.get(t.rails[p['rail_id']]['to_node'], ())) > 1]
        if not forks and first and first[-1]['end_um'] == t.rails[first[-1]['rail_id']]['length_um']:
            node = t.rails[first[-1]['rail_id']]['to_node']
            if len(graph.get(node, ())) > 1: forks = [(len(first), node)]
        if not forks: continue
        if probes: probes.count_work('traffic_queue_divert_searches')
        if claims is None: claims = t._claim_index()
        router = t._router(v['type_id']); old_s = t._duration(r['remaining'], v['type_id'])
        done = False
        for index, node in forks:
            taken = r['remaining'][index]['rail_id'] if index < len(r['remaining']) else None
            for alt in graph[node]:
                if alt == taken or t.causes['rail'][alt]: continue
                hid = _waypoint(t, alt)
                if hid is None: continue
                waypoint = t.holdings[hid]
                try:
                    outward = router.path(v, waypoint); onward = router.path(waypoint, r['target_pose'])
                except ValueError: continue
                if alt not in {p['rail_id'] for p in outward['sublinks']}: continue
                new = outward['sublinks'] + onward['sublinks']
                if new == r['remaining'] or any(t.causes['rail'][p['rail_id']] for p in new): continue
                new_s = t._duration(new, v['type_id'])
                if new_s - old_s > MAX_EXTRA_S: continue
                hop, _, target = t._next_hop({'remaining': new, 'target_pose': r['target_pose']})
                if not hop: continue
                try: t._safe(target, v['type_id'])
                except ValueError: continue
                sweep = swept_intervals(t.model, hop, t.halves[v['type_id']])
                if t._blockers(vid, sweep, t._resources(hop, sweep), claim_index=claims): continue
                prior = r.get('route_recovery', {}).get('recovery_poses', [])
                evidence = {'policy': POLICY, 'request_id': r['id'], 'vehicle_id': vid,
                    'blocked_by': [dict(b) for b in r['blockers']], 'waypoint_id': hid, 'amended_at_us': now,
                    'waited_us': now - r['waiting_since_us'],
                    'destination_pose_id': r['destination_pose_id'], 'traveled_seconds': r['cumulative_seconds'],
                    'recovery_poses': [dict(p) for p in prior] + [{k: v[k] for k in ('rail_id', 'offset_um')}],
                    'previous_remaining_seconds': old_s, 'new_remaining_seconds': new_s}
                amend_route(t, r, bound, new, evidence)
                t._denied_hops.pop(r['id'], None)
                if probes: probes.count_work('traffic_queue_diverts')
                changed = done = True; claims = None
                break
            if done: break
    return changed
