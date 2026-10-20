"""Busy-vehicle dispatch cap and super-hot expedite."""
import pytest
from types import SimpleNamespace
from fab_sim.transfers import Transfers


def stub(cap, busy, super_hot=False):
    t = Transfers.__new__(Transfers)
    t.busy_vehicle_cap = cap; t._cap_engaged = None; events = []
    t.vehicles = {f'v{i}': {'assignment_id': f'd{i}' if i < busy else None} for i in range(5)}
    t.manufacturing = SimpleNamespace(lots={'L': {'super_hot': super_hot}})
    t.emit = lambda kind, payload: events.append((kind, payload))
    return t, events


def test_cap_holds_at_the_cap_and_reports_only_changes_after_the_first_evaluation():
    t, events = stub(3, 2)
    # The first evaluation in a process records the state silently (a resume must
    # not report a change that did not happen).
    assert not t._cap_hold({'lot_id': 'L'}) and events == []
    t.vehicles['v2']['assignment_id'] = 'd2'
    assert t._cap_hold({'lot_id': 'L'}) and t._cap_hold({'lot_id': 'L'})
    assert events == [('dispatch_cap_changed', {'engaged': True, 'busy_vehicles': 3, 'cap': 3})]
    t.vehicles['v0']['assignment_id'] = None
    assert not t._cap_hold({'lot_id': 'L'})
    assert events[-1] == ('dispatch_cap_changed', {'engaged': False, 'busy_vehicles': 2, 'cap': 3})


def test_cap_never_holds_super_hot():
    t, events = stub(1, 5, super_hot=True)
    assert not t._cap_hold({'lot_id': 'L'}) and events == []



def test_super_hot_expedite_lets_a_super_hot_lot_start_a_batch_below_the_minimum():
    from test_batch import factory
    b, r, c, tools, s, streams, lots, events = factory(n=1, minimum=75, maximum=100)
    assert b.commit() is None  # one ordinary LOT waits for partners (minimum 3 LOTs)
    b.super_hot_expedite = True
    assert b.commit() is None  # the setting alone changes nothing for ordinary LOTs
    lots['lot:0']['super_hot'] = True
    aid = b.commit()
    assert aid is not None and [m['lot_id'] for m in b.assemblies[aid]['members']] == ['lot:0']


def _peer_scenario():
    # switch_on_empty: tool t has recipe A; lot:1 waits at the A step, so t may not
    # switch to B unless another available tool of the group holds A. Peer p holds
    # A but is down; it has no docks, so demands for t never name it.
    from copy import deepcopy
    from test_campaigns import switch_factory
    from fab_sim.manufacturing import SetupState
    campaigns, t, m, s, c, lots, vehicles, events = switch_factory(2)
    b = lots['lot:0']['current_visit_id']
    m.rules.visits[lots['lot:1']['current_visit_id']]['source_step_id'] = '2'
    m.tools['p'] = {**deepcopy(m.tools['t']), 'id': 'p', 'rack_slots': [], 'available': False}
    m.setups['p'] = SetupState('A', m.setups['t'].transitions)
    m.causes['p'] = set()
    m._wait_generation += 1
    for sid, slot in c.slots.items():
        if slot.get('kind') == 'track_buffer': slot['reservation'] = 'held'
    did = t.request('carrier:0', b, ('port',))
    return campaigns, t, m, did, b


def test_negative_cache_is_invalidated_when_a_peer_tool_recovers():
    campaigns, t, m, did, b = _peer_scenario()
    assert not campaigns.allows_admission(b, 't')
    t.assign()
    r = t.demands[did]
    assert r['status'] == 'pending' and did in t._neg.entries
    # The peer recovers (maintenance): its availability opens t's recipe switch.
    m.tools['p']['available'] = True
    m.tool_changed('p')
    t.note_event('maintenance_recovered', {'tool_id': 'p'})
    assert campaigns.allows_admission(b, 't')
    assert t._choices(r, c_origin(t, r), with_paths=False)
    hits = t._neg.hits
    t.assign()
    assert t._neg.hits == hits  # the stale entry was invalidated, not reused


def c_origin(t, r):
    return t.custody.locations[r['carrier_id']]
