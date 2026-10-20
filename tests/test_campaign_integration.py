from copy import deepcopy
import pytest
from test_campaigns import factory, deliver_members
from test_maintenance import pm, run_until


def maintenance(state, definitions=(), injected=()):
    from fab_sim.maintenance import Maintenance
    campaigns, t, m, s, c, lots, vehicles, events = state
    owner = Maintenance(m, lambda k, p: events.append((k, deepcopy(p))),
                        exchange_handlers=t.exchange.handlers)
    owner.initialize({'model': {'equipment': [{'id': 't', 'kind': 'ordinary', 'tool_group_id': 'A'}]}},
        {'model': {'tool_groups': [{'id': 'A', 'area': 'area'}],
                   'maintenance': list(definitions), 'breakdowns': []}},
        injected_faults=list(injected))
    return owner


@pytest.mark.parametrize('mode', [None, 'lot', 'wafer'])
def test_pending_pm_drains_staged_campaign_before_start(mode):
    state = factory(mode=mode)
    campaigns, t, m, s, c, lots, vehicles, events = state
    campaigns.dispatch()
    owner = maintenance(state, [pm(first=0, interval=100_000_000)])
    s.settle(0)
    record = campaigns.describe()[0]
    cause = next(iter(owner.causes.values()))
    assert cause['status'] == 'pending'
    assert not owner._drained('t')
    deliver_members(state)
    while campaigns.active_by_tool:
        s.settle(s.next_time_us)
    while cause['status'] == 'pending':
        s.settle(s.next_time_us)
    assert cause['status'] == 'active'
    assert m.setups['t'].completed_lots == 7
    assert cause['started_at_us'] >= max(m.rules.visits[x['visit_id']]['unloaded_at_us'] for x in record['members'])


def test_fault_during_staging_retains_members_and_resumes():
    state = factory()
    campaigns, t, m, s, c, lots, vehicles, events = state
    campaigns.dispatch()
    before = campaigns.describe()
    owner = maintenance(state, injected=[{'id': 'fault', 'target_id': 't',
        'target_type': 'equipment', 'onset_us': 0, 'duration_us': 5}])
    s.settle(0)
    assert campaigns.describe() == before
    assert not m.executions and m.causes['t']
    s.settle(5)
    assert campaigns.describe() == before and not m.causes['t']
    deliver_members(state)
    while campaigns.active_by_tool:
        s.settle(s.next_time_us)
    assert m.setups['t'].completed_lots == 7


def test_cancel_before_pickup_retains_campaign_slot_and_replacement_age():
    state = factory()
    campaigns, t, m, s, c, lots, vehicles, events = state
    campaigns.dispatch()
    member = campaigns.describe()[0]['members'][0]
    did = t.request(member['carrier_id'], member['visit_id'], ('port',))
    s.settle(0)
    t.cancel(did)
    assert c.slots[member['rack_slot_id']]['reservation'] == campaigns.visit_owners[member['visit_id']]
    assert c.locations[member['carrier_id']] == 'entry:0'
    again = t.request(member['carrier_id'], member['visit_id'], ('port',))
    s.settle(0)
    assert t.demands[again]['status'] == 'assigned'
    assert t.demands[again]['rack_slot_id'] == member['rack_slot_id']


def test_same_tool_locked_successor_clears_through_real_exchange_and_transport():
    from test_transfers import arrive
    from test_manufacturing_facade import drain
    state = factory()
    campaigns, t, m, s, c, lots, vehicles, events = state
    campaigns.dispatch()
    deliver_members(state)
    # After three credits, the first LOT's A successor is incompatible with B.
    while m.setups['t'].completed_lots < 3:
        s.settle(s.next_time_us)
    member = next(x for x in campaigns.describe()[0]['members'] if x['completed'])
    vid = lots[m.rules.visits[member['visit_id']]['lot_id']]['current_visit_id']
    did = t.request(member['carrier_id'], vid, ('port',))
    s.settle(s.now_us)
    assert t.demands[did]['status'] == 'pending'
    assert c.locations[member['carrier_id']] == member['rack_slot_id']
    # Output exchange waits for unloading, then owns a positive physical phase.
    while c.locations[member['carrier_id']] != 'port':
        s.settle(s.next_time_us)
    assert t.demands[did]['destination_id'] == 'buffer'
    assert c.locations[member['carrier_id']] == 'port'
    arrive(t, vehicles, did, 'pickup')
    while t.demands[did]['status'] != 'picked_up':
        s.settle(s.next_time_us)
    arrive(t, vehicles, did, 'drop')
    while c.locations[member['carrier_id']] != 'buffer':
        s.settle(s.next_time_us)
    assert c.slots[member['rack_slot_id']]['carrier_id'] is None
    assert m.setups['t'].current == 'B'


def test_dedication_excludes_bound_visits_without_blocking_feasible_campaign():
    state = factory(8)
    campaigns, t, m, s, c, lots, vehicles, events = state
    m.rules.routes['r']['dedication_components'] = [{'id': 'bound', 'source_step_ids': ['1']}]
    lots['lot:7']['dedication']['bound'] = 'other-tool'
    assert campaigns.dispatch()
    assert lots['lot:7']['current_visit_id'] not in campaigns.visit_owners
    assert len(campaigns.visit_owners) == 7
    assert lots['lot:7']['dedication'] == {'bound': 'other-tool'}


def test_finite_batch_tail_does_not_stop_other_campaign(tmp_path):
    import json
    from test_runner import fixture
    from test_route_rules import step
    from fab_sim.runner import run
    setup = {'id': 'B', 'when': 'need', 'fallback': None}
    path = fixture(tmp_path, model_updates={
        'required_tool_group_ids': ['A', 'B'],
        'tool_groups': [{'id': 'A', 'area': 'a', 'batching': False, 'cascading': False,
            'initial_setup': 'A', 'loading_us': 2, 'unloading_us': 3},
            {'id': 'B', 'area': 'a', 'batching': True, 'cascading': False,
             'initial_setup': None, 'loading_us': 2, 'unloading_us': 3}],
        'setup_transitions': [{'current': None, 'new': 'B', 'duration_us': 4, 'minimum_runs': 7}],
        'routes': [{'id': 'r', 'product_id': 'p', 'steps': [step(1, setup=setup)], 'dedication_components': []},
            {'id': 'q', 'product_id': 'q', 'steps': [step(1, tool_group_id='B', batch_min=50, batch_max=50,
                processing_unit='batch', sampling_probability='0.99')], 'dedication_components': []}],
        'releases': [{'id': product, 'product_id': product, 'route_id': route,
            'priority': 1, 'super_hot': False, 'wafer_count': 25, 'first_at_us': 0,
            'interval_seconds': '1', 'lots_per_release': 1, 'due_allowance_us': 100000000}
            for product, route in [('p', 'r'), ('q', 'q')]]},
        inputs={'manufacturing_path': 'manufacturing.json', 'physical_reference_path': 'reference.json'},
        generation={'entry_capacity': 8, 'rack_capacities': {'A': 8}, 'counts': {'B': 48}, 'improvement_limit': 1},
        workload={'count': 8, 'product_weights': {'p': '7', 'q': '1'}},
        execution={'horizon_us': 10_000_000_000, 'master_seed': 7})
    result = run(path)
    rows = [json.loads(row) for row in (tmp_path/'result/events.jsonl').read_text().splitlines()]
    assert result['exited_count'] == 7 and result['outcome'] == 'Incomplete'
    assert sum(row['event_type'] == 'setup_campaign_completed' for row in rows) == 1
    assert any(row['event_type'] == 'lot_exited' for row in rows)


def test_default_producer_can_accumulate_full_downstream_batch(tmp_path):
    from test_runner import fixture
    from test_route_rules import step
    from fab_sim.runner import run
    path = fixture(tmp_path, model_updates={
        'required_tool_group_ids': ['A', 'B'],
        'tool_groups': [{'id': 'A', 'area': 'a', 'batching': False, 'cascading': False,
            'initial_setup': None, 'loading_us': 2, 'unloading_us': 3},
            {'id': 'B', 'area': 'a', 'batching': True, 'cascading': False,
             'initial_setup': None, 'loading_us': 2, 'unloading_us': 3}],
        'routes': [{'id': 'r', 'product_id': 'p', 'steps': [step(1), step(2, tool_group_id='B',
            processing_unit='batch', batch_min=75, batch_max=75)], 'dedication_components': []}]},
        workload={'count': 3}, inputs={'manufacturing_path': 'manufacturing.json', 'physical_reference_path': 'reference.json'},
        generation={'entry_capacity': 3, 'counts': {'B': 16}, 'improvement_limit': 1},
        execution={'horizon_us': 10_000_000_000, 'master_seed': 7})
    result = run(path)
    assert result['outcome'] == 'Completed' and result['exited_count'] == 3
