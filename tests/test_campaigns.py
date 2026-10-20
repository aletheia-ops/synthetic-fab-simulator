"""Real finite custody and visits exercise setup campaign safety."""
from copy import deepcopy
from fractions import Fraction

from test_route_rules import step
from test_transfers import factory as transfer_factory
from test_manufacturing_facade import place


def factory(count=7, racks=8, mode=None):
    from fab_sim.campaigns import SetupCampaigns
    setup = {'id': 'B', 'when': 'need', 'fallback': None}
    first = step(1, setup=setup)
    if mode:
        first.update(processing_unit=mode, cascade_interval_us=5)
    t, m, s, c, lots, vehicles, events = transfer_factory([
        first, step(2, setup={**setup, 'id': 'A'})], rack_count=2 if mode == 'lot' else 1)
    m.setups['t'].current = 'A'
    m.setups['t'].transitions = ({'current': None, 'new': 'B', 'duration_us': 4,
                                'minimum_runs': 7},
                               {'current': None, 'new': 'A', 'duration_us': 4,
                                'minimum_runs': 7})
    for sid in m.tools['t']['rack_slots']:
        c.slots.pop(sid)
    m.tools['t']['rack_slots'] = []
    for i in range(racks):
        sid = f'rack:{i}'
        c.slots[sid] = {'id': sid, 'kind': 'rack', 'equipment_id': 't',
                        'carrier_id': None, 'reservation': None}
        m.tools['t']['rack_slots'].append(sid)
    lots.clear()
    for i in range(count):
        lid, sid = f'lot:{i}', f'entry:{i}'
        lots[lid] = {'route_id': 'r', 'product_id': 'p', 'priority': 10,
            'wafer_count': 25, 'carrier_id': f'carrier:{i}', 'admitted_at_us': 0}
        c.slots[sid] = {'id': sid, 'kind': 'entry', 'carrier_id': None, 'reservation': None}
        t.docks[sid] = {**t.docks['entry'], 'id': sid}
        t.model['docks'].append(t.docks[sid])
        place(c, f'carrier:{i}', sid)
        m.enter_visit(lid, '1')
    def cost(carrier, tid):
        origin = c.locations[carrier]
        if origin in m.tools[tid]['rack_slots']:
            return Fraction(0)
        path = t._path(t._pose(origin), t._pose('port'))
        return path['seconds'] if path else None
    campaigns = SetupCampaigns(m, t, cost, lambda k, p: events.append((k, deepcopy(p))))
    return campaigns, t, m, s, c, lots, vehicles, events


def test_partial_new_setup_does_not_switch():
    campaigns, t, m, s, c, lots, vehicles, events = factory(3)
    before = deepcopy(m.streams.__dict__)
    assert campaigns.dispatch() is False
    assert not campaigns.records and m.setups['t'].current == 'A'
    assert not any(slot['reservation'] for slot in c.slots.values())
    assert m.streams.__dict__ == before


def test_complete_campaign_stages_before_setup():
    campaigns, t, m, s, c, lots, vehicles, events = factory()
    assert campaigns.dispatch() is True
    record = campaigns.records[campaigns.active_by_tool['t']]
    assert record['status'] == 'staging' and len(record['members']) == 7
    assert len({member['rack_slot_id'] for member in record['members']}) == 7
    assert m.setups['t'].current == 'A' and not m.executions
    assert all(c.slots[x['rack_slot_id']]['reservation'] == record['id'] for x in record['members'])
    assert all(not campaigns.can_commit(x['visit_id'], 't') for x in record['members'])
    assert campaigns.dispatch() is False


def test_locked_setup_does_not_admit_incompatible_visit():
    campaigns, t, m, s, c, lots, vehicles, events = factory(3)
    m.setups['t'].minimum_runs = 7
    m.setups['t'].completed_lots = 3
    vid = lots['lot:0']['current_visit_id']
    did = t.request('carrier:0', vid, ('port',))
    s.settle(0)
    assert t.demands[did]['rack_slot_id'] is None
    assert not any(c.slots[sid]['reservation'] for sid in m.tools['t']['rack_slots'])


def test_pending_transfer_adoption_preserves_age():
    campaigns, t, m, s, c, lots, vehicles, events = factory()
    vehicles['v1']['available'] = False
    did = t.request('carrier:0', lots['lot:0']['current_visit_id'], ('port',))
    s.settle(0)
    age = t.demands[did]['original_request_time_us']
    assert campaigns.dispatch()
    assert t.demands[did]['status'] == 'pending'
    assert t.demands[did]['original_request_time_us'] == age
    assert lots['lot:0']['current_visit_id'] in campaigns.visit_owners


def test_insufficient_staging_space_never_partially_reserves_campaign():
    campaigns, t, m, s, c, lots, vehicles, events = factory(racks=6)
    assert not campaigns.dispatch()
    assert not campaigns.visit_owners and not campaigns.records
    assert not any(slot['reservation'] for slot in c.slots.values())


def deliver_members(state):
    from test_transfers import arrive
    campaigns, t, m, s, c, lots, vehicles, events = state
    record = campaigns.records[campaigns.active_by_tool['t']]
    for index, member in enumerate(record['members']):
        did = t.request(member['carrier_id'], member['visit_id'], ('port',))
        s.settle(s.now_us)
        assert t.demands[did]['status'] == 'assigned'
        arrive(t, vehicles, did, 'pickup')
        s.settle(s.now_us + 10)
        arrive(t, vehicles, did, 'drop')
        s.settle(s.now_us + 10)
        assert c.locations[member['carrier_id']] == 'port'
        s.settle(s.now_us + 1_000_000)
        assert c.locations[member['carrier_id']] == member['rack_slot_id']
        assert member['arrived']
        if index < 6:
            assert not m.executions and m.setups['t'].current == 'A'
    return record


def test_physical_arrivals_gate_setup_and_seven_successful_credits():
    from test_manufacturing_facade import drain
    state = factory()
    campaigns, t, m, s, c, lots, vehicles, events = state
    assert campaigns.dispatch()
    record = deliver_members(state)
    assert record['status'] == 'serving' and m.setups['t'].current == 'A'
    drain(s)
    assert record['status'] == 'completed' and all(x['completed'] for x in record['members'])
    assert m.setups['t'].current == 'B' and m.setups['t'].completed_lots == 7
    assert not campaigns.visit_owners and campaigns.drained('t')
    kinds = [kind for kind, _ in events]
    assert kinds.count('carrier_exchange_completed') == 7
    assert kinds.count('setup_campaign_ready') == kinds.count('setup_campaign_completed') == 1


def test_two_tools_cannot_share_campaign_members_or_slots():
    campaigns, t, m, s, c, lots, vehicles, events = factory()
    m.tools['u'] = {**m.tools['t'], 'id': 'u', 'rack_slots': []}
    m.setups['u'] = deepcopy(m.setups['t'])
    m.causes['u'] = set()
    for i in range(8):
        sid = f'u-rack:{i}'
        c.slots[sid] = {**c.slots['rack:0'], 'id': sid, 'equipment_id': 'u'}
        m.tools['u']['rack_slots'].append(sid)
    campaigns.managed_tools.add('u')
    campaigns.delivery_cost = lambda carrier, tid: Fraction(1)
    assert campaigns.dispatch()
    assert list(campaigns.active_by_tool) == ['t']
    assert len(campaigns.visit_owners) == 7
    assert all(c.slots[sid]['reservation'] is None for sid in m.tools['u']['rack_slots'])


def test_rank_applies_only_to_complete_feasible_campaigns():
    campaigns, t, m, s, c, lots, vehicles, events = factory(10)
    m.setups['t'].transitions += ({'current': None, 'new': 'C', 'duration_us': 1, 'minimum_runs': 7},)
    # Three higher-priority visits form a distinct route/setup; B still has seven.
    m.rules.routes['hot'] = deepcopy(m.rules.routes['r'])
    m.rules.steps['hot'] = deepcopy(m.rules.steps['r'])
    m.rules.steps['hot']['1']['setup']['id'] = 'C'
    for i in range(7, 10):
        lots[f'lot:{i}'].update(route_id='hot', priority=30)
        m.rules.visits[lots[f'lot:{i}']['current_visit_id']]['route_id'] = 'hot'
    assert campaigns.dispatch()
    record = campaigns.records[campaigns.active_by_tool['t']]
    assert record['setup_id'] == 'B'
    assert {x['carrier_id'] for x in record['members']} == {f'carrier:{i}' for i in range(7)}


def test_wrong_arrival_and_duplicate_completion_fail_explicitly():
    import pytest
    state = factory()
    campaigns, t, m, s, c, lots, vehicles, events = state
    campaigns.dispatch()
    record = campaigns.records[campaigns.active_by_tool['t']]
    member = record['members'][0]
    with pytest.raises(RuntimeError, match='ownership'):
        campaigns.arrived(member['visit_id'], member['rack_slot_id'])
    deliver_members(state)
    while not member['completed']:
        s.settle(s.next_time_us)
    assert member['completed']
    with pytest.raises(RuntimeError, match='ownership'):
        campaigns.completed(member['visit_id'], 't')


def test_lost_campaign_membership_invalidates_pickup_without_moving_carrier():
    from test_transfers import arrive
    state = factory()
    campaigns, t, m, s, c, lots, vehicles, events = state
    campaigns.dispatch()
    member = campaigns.describe()[0]['members'][0]
    did = t.request(member['carrier_id'], member['visit_id'], ('port',))
    s.settle(0)
    del campaigns.visit_owners[member['visit_id']]
    arrive(t, vehicles, did, 'pickup')
    assert t.demands[did]['status'] == 'cancelled'
    assert c.locations[member['carrier_id']] == 'entry:0'
    assert c.slots[member['rack_slot_id']]['reservation'].startswith('setup-campaign:')


def test_existing_assigned_nonmember_admission_drains_before_campaign_commit():
    campaigns, t, m, s, c, lots, vehicles, events = factory(8)
    vid = lots['lot:7']['current_visit_id']
    m.rules.routes['old'] = deepcopy(m.rules.routes['r'])
    m.rules.steps['old'] = deepcopy(m.rules.steps['r'])
    m.rules.steps['old']['1']['setup']['id'] = 'A'
    m.rules.visits[vid]['route_id'] = 'old'
    lots['lot:7']['route_id'] = 'old'
    did = t.request('carrier:7', vid, ('port',))
    s.settle(0)
    assert t.demands[did]['status'] == 'assigned'
    assert not campaigns.dispatch()
    assert not campaigns.records


def switch_factory(count=3):
    campaigns, t, m, s, c, lots, vehicles, events = factory(count)
    campaigns.policy = 'switch_on_empty'
    return campaigns, t, m, s, c, lots, vehicles, events


def test_switch_on_empty_admits_new_recipe_without_staging_a_campaign():
    # Only recipe-B LOTs wait; the tool has nothing for its installed recipe A.
    campaigns, t, m, s, c, lots, vehicles, events = switch_factory(1)
    vid = lots['lot:0']['current_visit_id']
    assert campaigns.dispatch() is False and not campaigns.records
    assert campaigns.allows_admission(vid, 't') and campaigns.can_commit(vid, 't')


def test_switch_on_empty_keeps_installed_recipe_while_its_work_remains():
    campaigns, t, m, s, c, lots, vehicles, events = switch_factory(2)
    b = lots['lot:0']['current_visit_id']
    assert campaigns.allows_admission(b, 't')
    # lot:1 moves to step 2, which uses the installed recipe A.
    a_visit = m.rules.visits[lots['lot:1']['current_visit_id']]
    a_visit['source_step_id'] = '2'
    m._wait_generation += 1  # a real visit change publishes a resource change
    assert not campaigns.allows_admission(b, 't')


def test_switch_on_empty_respects_minimum_runs_after_a_change():
    campaigns, t, m, s, c, lots, vehicles, events = switch_factory(1)
    vid = lots['lot:0']['current_visit_id']
    m.setups['t'].minimum_runs = 7
    m.setups['t'].completed_lots = 3
    assert not campaigns.allows_admission(vid, 't')
    m.setups['t'].completed_lots = 7
    assert campaigns.allows_admission(vid, 't')


def test_staged_campaign_remains_the_default_policy():
    campaigns, *_ = factory(3)
    assert campaigns.policy == 'staged_campaign'


def test_switch_on_empty_admission_stays_valid_once_bound_to_the_tool():
    # A recipe-B LOT on the tool's rack keeps its admission even after
    # installed-recipe (A) work appears in the group.
    campaigns, t, m, s, c, lots, vehicles, events = switch_factory(2)
    b = lots['lot:0']['current_visit_id']
    c.locations.pop('carrier:0', None)
    for slot in c.slots.values():
        if slot['carrier_id'] == 'carrier:0':slot['carrier_id'] = None
    place(c, 'carrier:0', m.tools['t']['rack_slots'][0])
    m.rules.visits[lots['lot:1']['current_visit_id']]['source_step_id'] = '2'
    assert campaigns.allows_admission(b, 't')


def test_switch_on_empty_does_not_commit_a_change_while_installed_work_is_bound():
    # Recipe-B LOT on the rack, an installed-recipe (A) LOT also on the rack:
    # admission of B stays valid, but the change waits for the A work.
    campaigns, t, m, s, c, lots, vehicles, events = switch_factory(2)
    for i in (0, 1):
        c.locations.pop(f'carrier:{i}', None)
        for slot in c.slots.values():
            if slot['carrier_id'] == f'carrier:{i}':slot['carrier_id'] = None
        place(c, f'carrier:{i}', m.tools['t']['rack_slots'][i])
    b = lots['lot:0']['current_visit_id']
    m.rules.visits[lots['lot:1']['current_visit_id']]['source_step_id'] = '2'
    assert campaigns.allows_admission(b, 't')
    assert not campaigns.can_commit(b, 't')


def test_switch_on_empty_bound_lot_waits_out_a_recipe_lock_instead_of_failing():
    # The tool changed recipe after this LOT was bound: it stays admitted (no
    # transfer failure) and is committed only once the minimum runs are done.
    campaigns, t, m, s, c, lots, vehicles, events = switch_factory(1)
    b = lots['lot:0']['current_visit_id']
    c.locations.pop('carrier:0', None)
    for slot in c.slots.values():
        if slot['carrier_id'] == 'carrier:0':slot['carrier_id'] = None
    place(c, 'carrier:0', m.tools['t']['rack_slots'][0])
    m.setups['t'].current = 'C';m.setups['t'].minimum_runs = 7;m.setups['t'].completed_lots = 2
    assert campaigns.allows_admission(b, 't')
    assert not campaigns.can_commit(b, 't')
    m.setups['t'].completed_lots = 7
    assert campaigns.can_commit(b, 't')


def group_factory(policy):
    """Two LOTs (lot:0 wants recipe B, lot:1 recipe A) and a peer tool t2 holding A."""
    campaigns, t, m, s, c, lots, vehicles, events = factory(2)
    campaigns.policy = policy
    a_visit = m.rules.visits[lots['lot:1']['current_visit_id']]
    a_visit['source_step_id'] = '2'
    m.tools['t2'] = {**deepcopy(m.tools['t']), 'id': 't2', 'rack_slots': ['rack:t2']}
    c.slots['rack:t2'] = {'id': 'rack:t2', 'kind': 'rack', 'equipment_id': 't2', 'carrier_id': None, 'reservation': None}
    m.setups['t2'] = deepcopy(m.setups['t']); m.causes['t2'] = set()
    m.resource_index = None
    m._wait_generation += 1
    return campaigns, m, c, lots, a_visit


def test_group_least_setup_keeps_the_recipe_while_same_recipe_work_waits_in_the_group():
    # switch_on_empty lets t switch because peer t2 also holds A; the source rule
    # (least setup over the group queue) keeps A while an A LOT waits unassigned.
    for policy, allowed in (('switch_on_empty', True), ('group_least_setup', False)):
        campaigns, m, c, lots, a_visit = group_factory(policy)
        b = lots['lot:0']['current_visit_id']
        assert campaigns.allows_admission(b, 't') is allowed, policy


def test_group_least_setup_lets_a_higher_priority_lot_trigger_a_setup():
    campaigns, m, c, lots, a_visit = group_factory('group_least_setup')
    b = lots['lot:0']['current_visit_id']
    m.rules.visits[b]['priority'] = 20; m._wait_generation += 1
    assert campaigns.allows_admission(b, 't')


def test_group_least_setup_counts_work_bound_to_a_down_peer():
    campaigns, m, c, lots, a_visit = group_factory('group_least_setup')
    b = lots['lot:0']['current_visit_id']
    carrier = lots['lot:1']['carrier_id']
    origin = c.locations[carrier]
    assert c.reserve_many(['rack:t2'], 'test', origins={origin: carrier})
    c.move(carrier, origin, 'rack:t2', 'test')
    m._wait_generation += 1
    assert campaigns.allows_admission(b, 't')          # served by its up peer
    m.causes['t2'].add('pm:t2'); m._wait_generation += 1
    assert not campaigns.allows_admission(b, 't')      # the peer is down: still group work
