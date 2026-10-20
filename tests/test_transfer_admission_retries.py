"""Owned admission retry dependencies remain authoritative under direct mutation."""
import sys
import pytest
from fab_sim.transfers import Transfers
from fab_sim.resource_changes import ResourceChange
from test_transport_resources import blocked_pickup


def admission_calls(action):
    calls = 0
    code = Transfers._admission_valid.__code__
    def profile(frame, event, arg):
        nonlocal calls
        if event == 'call' and frame.f_code is code:
            calls += 1
    previous = sys.getprofile()
    try:
        sys.setprofile(profile)
        action()
    finally:
        sys.setprofile(previous)
    return calls


def retry(t, s):
    t.resource_changed(ResourceChange(slot_ids=frozenset({'rack'})))
    s.settle(s.now_us)


def test_unchanged_blocked_pickup_reuses_owned_admission():
    _, t, m, s, c, lots, v, events, did = blocked_pickup()
    retry(t, s)
    assert admission_calls(lambda: retry(t, s)) == 0
    assert t.demands[did]['status'] == 'pickup_wait'


@pytest.mark.parametrize('mutation', ['visit', 'execution', 'group', 'dedication'])
def test_direct_canonical_admission_mutation_invalidates_retry(mutation):
    _, t, m, s, c, lots, v, events, did = blocked_pickup()
    retry(t, s)
    r = t.demands[did]
    visit = m.rules.visits[r['visit_id']]
    if mutation == 'visit':
        visit['status'] = 'processing'
    elif mutation == 'execution':
        visit['execution_id'] = 'foreign'
    elif mutation == 'group':
        m.tools[t.docks[r['destination_id']]['equipment_id']]['tool_group_id'] = 'foreign'
    else:
        lot = m.lots[visit['lot_id']]
        route = m.rules.routes[lot['route_id']]
        route['dedication_components'].append({'id':'retry-test', 'source_step_ids':[visit['source_step_id']]})
        lot['dedication']['retry-test'] = 'foreign'
    assert admission_calls(lambda: retry(t, s)) > 0
    assert t.demands[did]['status'] == 'cancelled'


@pytest.mark.parametrize('override', ['instance', 'class'])
def test_custom_native_validator_is_always_rechecked(monkeypatch, override):
    _, t, m, s, c, lots, v, events, did = blocked_pickup()
    retry(t, s)
    original = Transfers._target_valid
    seen = []
    def target(self, r, sid):
        seen.append(sid)
        return original(self, r, sid)
    if override == 'class':
        monkeypatch.setattr(Transfers, '_target_valid', target)
    else:
        monkeypatch.setattr(t, '_target_valid', target.__get__(t, Transfers))
    retry(t, s)
    retry(t, s)
    assert len(seen) == 2


def test_custom_dependency_mapping_bypasses_reuse():
    _, t, m, s, c, lots, v, events, did = blocked_pickup()
    retry(t, s)
    m.lots = type('CustomLots', (dict,), {})(m.lots)
    assert admission_calls(lambda: retry(t, s)) > 0
    assert admission_calls(lambda: retry(t, s)) > 0


@pytest.mark.parametrize('mutation', ['member', 'arrived', 'assembly', 'owner'])
def test_batch_owned_membership_mutation_invalidates_signature(mutation):
    _, t, m, s, c, lots, v, events, did = blocked_pickup()
    r = t.demands[did]
    visit = m.rules.visits[r['visit_id']]
    step = m._step(visit)
    step['batch_min'] = 1
    visit['assembly_id'] = r['rack_owner']
    member = {k:r[k] for k in ('carrier_id','visit_id','rack_slot_id')}
    member['arrived'] = False
    assembly = {'status':'assembling',
        'tool_id':t.docks[r['destination_id']]['equipment_id'], 'members':[member]}
    m.batches.assemblies[r['rack_owner']] = assembly
    assert t._retry_admission_valid(r)
    assert admission_calls(lambda: t._retry_admission_valid(r)) == 0
    if mutation == 'member':
        member['carrier_id'] = 'foreign'
    elif mutation == 'arrived':
        member['arrived'] = True
    elif mutation == 'assembly':
        assembly['status'] = 'finished'
    else:
        visit['assembly_id'] = 'foreign'
    assert admission_calls(lambda: t._retry_admission_valid(r)) == 1
    assert not t._retry_admission_valid(r)


@pytest.mark.parametrize('mutation', ['owner', 'arrived', 'rack', 'tool'])
def test_campaign_owned_membership_mutation_invalidates_signature(mutation):
    from test_campaigns import factory
    campaigns, t, m, s, c, lots, v, events = factory()
    assert campaigns.dispatch()
    did = t.request('carrier:0', lots['lot:0']['current_visit_id'], ('port',))
    t.assign()
    r = t.demands[did]
    assert t._retry_admission_valid(r)
    assert admission_calls(lambda: t._retry_admission_valid(r)) == 0
    owner = campaigns.visit_owners[r['visit_id']]
    campaign = campaigns.records[owner]
    member = next(x for x in campaign['members'] if x['visit_id'] == r['visit_id'])
    if mutation == 'owner':
        del campaigns.visit_owners[r['visit_id']]
    elif mutation == 'arrived':
        member['arrived'] = True
    elif mutation == 'rack':
        member['rack_slot_id'] = 'foreign'
    else:
        campaign['tool_id'] = 'foreign'
    assert admission_calls(lambda: t._retry_admission_valid(r)) == 1
    assert not t._retry_admission_valid(r)


def test_retired_demand_drops_derived_retry_entry():
    _, t, m, s, c, lots, v, events, did = blocked_pickup()
    retry(t, s)
    assert did in t._admission_retries
    t.cancel(did)
    assert did not in t._admission_retries


def test_bool_integer_dependencies_do_not_alias():
    _, t, m, s, c, lots, v, events, did = blocked_pickup()
    r = t.demands[did]
    m.rules.visits[r['visit_id']]['execution_id'] = False
    assert t._retry_admission_valid(r)
    m.rules.visits[r['visit_id']]['execution_id'] = 0
    assert admission_calls(lambda: t._retry_admission_valid(r)) == 1


def test_unknown_equality_value_always_falls_back():
    _, t, m, s, c, lots, v, events, did = blocked_pickup()
    retry(t, s)
    class Status:
        def __eq__(self, other):
            return other == 'pending'
    m.rules.visits[t.demands[did]['visit_id']]['status'] = Status()
    assert admission_calls(lambda: retry(t, s)) > 0
    assert admission_calls(lambda: retry(t, s)) > 0


def test_overflow_clearance_contract_is_never_reused():
    _, t, m, s, c, lots, v, events, did = blocked_pickup()
    r = t.demands[did]
    r['overflow'] = True
    assert admission_calls(lambda: t._retry_admission_valid(r)) == 1
    assert admission_calls(lambda: t._retry_admission_valid(r)) == 1
    assert did not in t._admission_retries


def test_pickup_commit_revalidates_even_after_positive_retry_reuse():
    _, t, m, s, c, lots, v, events, did = blocked_pickup()
    r = t.demands[did]
    assert t._retry_admission_valid(r)
    assert admission_calls(lambda: t._retry_admission_valid(r)) == 0
    origin = c.locations[r['carrier_id']]
    t._start_handling(r, 'pickup')
    m.rules.visits[r['visit_id']]['execution_id'] = 'foreign'
    assert admission_calls(lambda: t._handled(r, 'pickup')) == 1
    assert r['status'] == 'cancelled'
    assert c.locations[r['carrier_id']] == origin


@pytest.mark.parametrize('mutation', ['current', 'transitions', 'minimum_runs',
    'completed_lots', 'setup', 'active_tool'])
def test_managed_setup_state_direct_mutation_rebuilds_retry(mutation):
    from test_campaigns import factory
    campaigns, t, m, s, c, lots, v, events = factory(3)
    state = m.setups['t']
    state.transitions = ({'current':None, 'new':'B', 'duration_us':4, 'minimum_runs':0},)
    did = t.request('carrier:0', lots['lot:0']['current_visit_id'], ('port',))
    t.assign()
    r = t.demands[did]
    assert t._retry_admission_valid(r)
    assert admission_calls(lambda: t._retry_admission_valid(r)) == 0
    if mutation == 'current':
        state.current = 'B'
    elif mutation == 'transitions':
        state.transitions[0]['minimum_runs'] = 7
    elif mutation == 'minimum_runs':
        state.minimum_runs = 7
    elif mutation == 'completed_lots':
        state.completed_lots = 7
    elif mutation == 'setup':
        m._step(m.rules.visits[r['visit_id']])['setup']['when'] = 'always'
    else:
        campaigns.active_by_tool['t'] = 'foreign'
    assert admission_calls(lambda: t._retry_admission_valid(r)) == 1
    assert t._retry_admission_valid(r) == t._admission_valid(r)


def test_different_rule_lot_mapping_never_reuses_manufacturing_dependencies():
    from copy import deepcopy
    _, t, m, s, c, lots, v, events, did = blocked_pickup()
    r = t.demands[did]
    assert t._retry_admission_valid(r)
    m.rules.lots = deepcopy(m.rules.lots)
    assert admission_calls(lambda: t._retry_admission_valid(r)) == 1
    assert admission_calls(lambda: t._retry_admission_valid(r)) == 1


def test_setup_state_method_extension_never_reuses_native_signature(monkeypatch):
    from fab_sim.manufacturing import SetupState
    from test_campaigns import factory
    campaigns, t, m, s, c, lots, v, events = factory(3)
    state = m.setups['t']
    state.transitions = ({'current':None, 'new':'B', 'duration_us':4, 'minimum_runs':0},)
    did = t.request('carrier:0', lots['lot:0']['current_visit_id'], ('port',))
    t.assign()
    r = t.demands[did]
    assert t._retry_admission_valid(r)
    original = SetupState.can_use
    monkeypatch.setattr(SetupState, 'can_use', lambda self, step: original(self, step))
    assert admission_calls(lambda: t._retry_admission_valid(r)) == 1
    assert admission_calls(lambda: t._retry_admission_valid(r)) == 1


def test_retry_caches_are_nonphysical_and_cleared_on_restore(tmp_path):
    from test_runner import fixture
    from fab_sim.runner import build_runtime, load_config
    from fab_sim.runtime_state import capture_runtime, restore_runtime, runtime_digest
    from fab_sim.checkpoint_format import encode_value, decode_value
    config = load_config(fixture(tmp_path))
    owners = build_runtime(config, lambda *args: None, initialize=True)
    owners.scheduler.settle(owners.scheduler.next_time_us)
    digest = runtime_digest(owners)
    owners.transfers._admission_retries['ghost'] = ('stale', True)
    assert runtime_digest(owners) == digest
    state = decode_value(encode_value(capture_runtime(owners)))
    clone = build_runtime(config, lambda *args: None, initialize=False)
    clone.transfers._admission_retries['ghost'] = ('stale', True)
    restore_runtime(clone, state, clone.actions)
    assert clone.transfers._admission_retries == {}
    assert runtime_digest(clone) == digest
