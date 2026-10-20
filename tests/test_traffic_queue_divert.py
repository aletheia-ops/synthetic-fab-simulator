"""Persistent-queue divert: a vehicle stuck behind waiting vehicles takes a free branch at a diverge."""
from copy import deepcopy
from types import SimpleNamespace
from fab_sim.topology import compile_traffic
from fab_sim.traffic_queue_divert import WAIT_US, POLICY
from test_traffic import factory


def queue(*, alternative=True, stuck_ahead=True):
    rails = [('a', 'x', 'y'), ('b', 'y', 'z'), ('e', 'z', 'x')]
    if alternative: rails += [('c', 'y', 'w'), ('d', 'w', 'z')]
    raw = {'nodes': [{'id': n, 'zcu_id': None, 'zcu_type': 'NONE'} for n in ('x', 'y', 'z', 'w')[:4 if alternative else 3]],
      'rails': [{'id': rid, 'from_node': a, 'to_node': b, 'length_um': 1000, 'bay_id': 'bay', 'curve': False,
                 'speed_multiplier': '1'} for rid, a, b in rails],
      'zcus': [], 'docks': [{'id': 'goal1', 'rail_id': 'e', 'offset_um': 900}, {'id': 'goal2', 'rail_id': 'e', 'offset_um': 500}],
      'vehicle_types': [{'id': 'type', 'length_um': 200, 'gap_um': 0, 'speed_um_s': '1000'}],
      'vehicles': [{'id': 'v1', 'type_id': 'type', 'rail_id': 'a', 'offset_um': 900},
                   {'id': 'v2', 'type_id': 'type', 'rail_id': 'b', 'offset_um': 100},
                   {'id': 'p', 'type_id': 'type', 'rail_id': 'b', 'offset_um': 400}],
      'policy': {'holding_pitch_um': 1000, 'raw_dock_clamp': False, 'curve_cap_um_s': '1000'}}
    m = compile_traffic(raw)
    t, s, v, e = factory(m); t.manage_idle = False; t.queue_divert = True
    movements = {}; demands = {}; slots = {}; locations = {}
    for i in ((1, 2) if stuck_ahead else (1,)):
        vid = f'v{i}'; did = f'd{i}'; mid = did + ':drop'; goal = f'goal{i}'; carrier = f'carrier{i}'; slot = f'vehicle{i}'
        path = t._router('type').path(v[vid], t.docks[goal])
        t.request(vid, goal, mid, path=path); v[vid].update(assignment_id=did, carrier_slot_id=slot)
        movements[mid] = {'id': mid, 'demand_id': did, 'vehicle_id': vid, 'phase': 'drop', 'path': deepcopy(path),
            'status': 'moving', 'cancelled': False, 'target_pose': dict(t.requests[mid]['target_pose'])}
        demands[did] = {'id': did, 'status': 'picked_up', 'carrier_id': carrier, 'vehicle_id': vid,
            'destination_id': goal, 'rack_slot_id': None, 'loaded_path': deepcopy(path)}
        slots[slot] = {'carrier_id': carrier, 'reservation': did}; locations[carrier] = slot
        slots[goal] = {'carrier_id': None, 'reservation': did}
    t.transfers = SimpleNamespace(movements=movements, demands=demands,
        custody=SimpleNamespace(slots=slots, locations=locations), dirty=lambda: None)
    until(s, 0)
    return t, s, v, e


def until(s, at_us):
    while s.next_time_us is not None and s.next_time_us < at_us: s.settle(s.next_time_us)
    s.settle(at_us)


def diverts(e): return [p for k, p in e if k == 'traffic_route_amended' and p['policy'] == POLICY]


def test_stuck_vehicle_takes_free_branch_after_wait_and_keeps_destination():
    t, s, v, e = queue()
    r = t.requests['d1:drop']
    assert r['blockers'] and not t.hops.get('v1')
    until(s, WAIT_US - 1); t.dirty(); until(s, WAIT_US - 1)
    assert not diverts(e)
    until(s, WAIT_US); t.dirty(); until(s, WAIT_US)
    [p] = diverts(e)
    assert p['vehicle_id'] == 'v1' and p['destination_pose_id'] == 'goal1' and p['waited_us'] == WAIT_US
    assert {q['rail_id'] for q in r['remaining']} >= {'c', 'd'} and 'b' not in {q['rail_id'] for q in r['remaining']}
    assert t.hops['v1'] and t.transfers.movements['d1:drop']['path']['sublinks'][-1]['rail_id'] == 'e'


def test_no_divert_without_alternative_or_behind_a_vehicle_that_is_not_waiting():
    for kwargs in ({'alternative': False}, {'stuck_ahead': False}):
        t, s, v, e = queue(**kwargs)
        until(s, 3 * WAIT_US); t.dirty(); until(s, 3 * WAIT_US)
        assert not diverts(e), kwargs


def test_failed_attempt_is_not_repeated_within_the_same_wait_window():
    from fab_sim.probes import Probes
    t, s, v, e = queue(alternative=False); p = Probes(); t.transfers.probes = p
    until(s, WAIT_US); t.dirty(); until(s, WAIT_US); t.dirty(); until(s, WAIT_US + 1)
    assert t.requests['d1:drop']['queue_divert_key'] == [0, 1]
