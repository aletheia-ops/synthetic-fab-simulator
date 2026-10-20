import sys
import pytest
from collections import Counter
from test_clearance import lease_fixture
from fab_sim.clearance import ClearancePool
from fab_sim.manufacturing import RouteRules


def profile_calls(action):
    codes={ClearancePool._cohort.__code__: 'cohort', RouteRules.eligible_tools.__code__: 'eligible'}
    counts=Counter()
    def count(frame,event,arg):
        if event=='call' and frame.f_code in codes:counts[codes[frame.f_code]]+=1
    previous=sys.getprofile()
    try:
        sys.setprofile(count)
        action()
    finally:sys.setprofile(previous)
    return counts


def test_blocked_cohort_retries_until_capacity_frees():
    pool,t,m,s,c,lots=lease_fixture()
    t._router()
    c.reserve('protected:1','foreign')
    for _ in range(10):
        assert not pool.dispatch()
        assert pool.waits['t']['required_capacity']==2
    c.release('protected:1','foreign')
    assert pool.dispatch()
    assert len(next(iter(pool.records.values()))['members'])==2


def mutate(case,pool,t,m,c,lots):
    lot=lots['low'];vid=lot['current_visit_id'];visit=m.rules.visits[vid]
    demand=t.demands[t.active[lot['carrier_id']]]
    if case=='admission':lot['admitted_at_us']=None
    elif case=='visit':lot['current_visit_id']=None
    elif case=='carrier':lot['carrier_id']='new-carrier'
    elif case=='location':c.locations[lot['carrier_id']]='elsewhere'
    elif case=='claim':m.claims[lot['carrier_id']]={}
    elif case=='reservation':c.slots['rack']['reservation']='foreign-rack'
    elif case=='execution':visit['execution_id']='execution'
    elif case=='status':visit['status']='processing'
    elif case=='assembly':visit['assembly_id']='assembly'
    elif case=='age':visit['entered_at_us']=99;demand['original_request_time_us']=99
    elif case=='demand_status':demand['status']='in_transit'
    elif case=='exchange':demand['output_exchange_id']='exchange'
    elif case=='setup':m.setups['t'].current='B'
    elif case=='credits':m.setups['t'].minimum_runs=2;m.setups['t'].completed_lots=0
    elif case=='transition':m.setups['t'].transitions[0]['minimum_runs']=1
    elif case=='step':m.rules.steps['r']['2']['setup']['id']='C'
    elif case=='components':
        m.rules.routes['r']['dedication_components']=[{'id':'ded','source_step_ids':['2']}]
        lot['dedication']['ded']='missing-tool'
    elif case=='catalogue':m.tools.clear();m.tools['t']={'id':'t','tool_group_id':'A','rack_slots':['rack','rack-extra:1']};m.tools['new']={'tool_group_id':'A','rack_slots':[]}
    else:raise AssertionError(case)


@pytest.mark.parametrize('case',[
    'admission','visit','carrier','location','claim','reservation','execution','status',
    'assembly','age','demand_status','exchange','setup','credits','transition','step',
    'components','catalogue'])
def test_direct_local_mutations_match_fresh_native_cohort(case):
    pool,t,m,s,c,lots=lease_fixture();t._router();c.reserve('protected:1','foreign')
    assert not pool.dispatch()
    mutate(case,pool,t,m,c,lots)
    cold,ct,cm,cs,cc,clots=lease_fixture();ct._router();cc.reserve('protected:1','foreign')
    mutate(case,cold,ct,cm,cc,clots)
    pool.dispatch();cold.dispatch()
    assert pool.waits==cold.waits
    assert pool.records==cold.records
    assert c.slots==cc.slots


def test_unknown_carrier_lookup_keeps_each_native_retry_callback():
    pool,t,m,s,c,lots=lease_fixture();t._router();c.reserve('protected:1','foreign')
    calls=[]
    def lookup(carrier):
        calls.append(carrier)
        return next((lid,lot) for lid,lot in lots.items() if lot['carrier_id']==carrier)
    m.carrier_lot_lookup=lookup
    for _ in range(3):assert not pool.dispatch()
    assert len(calls)==6


def test_native_method_extensions_invalidate_positive_cohort(monkeypatch):
    pool,t,m,s,c,lots=lease_fixture();t._router();c.reserve('protected:1','foreign')
    assert not pool.dispatch()
    original=RouteRules.eligible_tools;calls=[]
    def eligible(self,vid,tools):
        calls.append(vid)
        return original(self,vid,tools)
    monkeypatch.setattr(RouteRules,'eligible_tools',eligible)
    for _ in range(3):assert not pool.dispatch()
    assert len(calls)>=6


def test_finished_assembly_members_and_departures_refresh_cohort():
    pool,t,m,s,c,lots=lease_fixture();t._router();c.reserve('protected:1','foreign')
    m.tools['t']['assembly_id']='finished'
    assembly={'status':'finished','members':[{'carrier_id':lot['carrier_id'],'departed':False} for lot in lots.values()]}
    m.batches.assemblies['finished']=assembly
    assert not pool.dispatch()
    assert pool.waits['t']['kind']=='finished_batch'
    assembly['members'][0]['departed']=True
    assert pool.dispatch()
    assert len(next(iter(pool.records.values()))['members'])==1


def test_custom_assignment_member_mutation_cannot_poison_retry_cohort(monkeypatch):
    pool,t,m,s,c,lots=lease_fixture();t._router();c.reserve('protected:1','foreign')
    assert not pool.dispatch()
    seen=[]
    def assignment(members,slots):
        seen.append(members[0]['age'])
        members[0]['age']=999
        return None
    monkeypatch.setattr(pool,'_first_assignment',assignment)
    for _ in range(3):assert not pool.dispatch()
    assert seen==[seen[0]]*3


def test_native_flow_carrier_lookup_reuses_current_local_mapping():
    from fab_sim.flow import Flow
    pool,t,m,s,c,lots=lease_fixture();t._router();c.reserve('protected:1','foreign')
    flow=object.__new__(Flow);flow.lots=lots
    flow._lot_by_carrier={lot['carrier_id']:lid for lid,lot in lots.items()}
    m.carrier_lot_lookup=flow._carrier_lot
    for _ in range(3):assert pool.dispatch() is False
    flow._lot_by_carrier.pop('clow')
    pool.dispatch()
    assert pool.dispatch() is False


def test_changed_empty_eligibility_preserves_skipped_step_lookup():
    pool,t,m,s,c,lots=lease_fixture();t._router();c.reserve('protected:1','foreign')
    assert not pool.dispatch()
    m.rules.routes['r']['dedication_components']=[{'id':'ded','source_step_ids':['2']}]
    for lot in lots.values():lot['dedication']['ded']='missing-tool'
    m.rules.steps.clear()
    assert not pool.dispatch()
    assert pool.waits=={}


def test_cached_global_contenders_recheck_age_and_shared_capacity():
    from fab_sim.flow import Flow
    from fab_sim.manufacturing import SetupState
    from test_manufacturing_facade import place
    pool,t,m,s,c,lots=lease_fixture();t._router()
    m.tools['u']={'id':'u','tool_group_id':'A','rack_slots':['u:0','u:1']}
    m.setups['u']=SetupState('A',m.setups['t'].transitions)
    for i in range(2):
        sid=f'u:{i}';lid=f'other:{i}';carrier=lid+':carrier'
        c.slots[sid]={'id':sid,'kind':'rack','equipment_id':'u','carrier_id':None,'reservation':None}
        lots[lid]={'route_id':'r','priority':10,'wafer_count':25,'carrier_id':carrier,'admitted_at_us':0}
        m.enter_visit(lid,'2');place(c,carrier,sid)
    c.slots['u:port']={'id':'u:port','kind':'port','equipment_id':'u','carrier_id':None,'reservation':None}
    t.docks['u:port']={'id':'u:port','equipment_id':'u','kind':'port','role':'BOTH','rail_id':'a','offset_um':450}
    flow=object.__new__(Flow);flow.lots=lots
    flow._lot_by_carrier={lot['carrier_id']:lid for lid,lot in lots.items()}
    m.carrier_lot_lookup=flow._carrier_lot
    c.reserve('protected:1','foreign')
    assert not pool.dispatch()
    assert not pool.dispatch()
    for lid in ('other:0','other:1'):m.rules.visits[lots[lid]['current_visit_id']]['entered_at_us']=-1
    c.release('protected:1','foreign')
    assert pool.dispatch()
    record=next(iter(pool.records.values()))
    assert record['tool_id']=='u' and len(record['members'])==2
    assert pool.waits['t']['available_capacity']==0
    assert pool.waits['t']['conflicting_leases']==[record['id']]
