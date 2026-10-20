from copy import deepcopy
import pytest
from fab_sim.probes import Probes, state_projection
from test_traffic import factory, model, short_links

def blocked():
    t,s,v,e=factory(model([{'id':'v1','type_id':'type','rail_id':'a','offset_um':500},
                           {'id':'v2','type_id':'type','rail_id':'a','offset_um':1100}]))
    t.probes=Probes();t.request('v1','dock','one');s.settle(0)
    assert t.requests['one']['status']=='pending'
    return t,s,v,e

def test_unchanged_denial_skips_geometry_and_permission_recheck(monkeypatch):
    t,s,v,e=blocked();before=deepcopy(e)
    calls=[];original=t._blockers
    def check(*a,**kw):calls.append(1);return original(*a,**kw)
    monkeypatch.setattr(t,'_blockers',check)
    for _ in range(4):t.dirty();s.settle(s.now_us)
    assert not calls
    assert t.probes.work_counts['traffic_denied_checks_reused']==4
    assert e==before and not t.hops

@pytest.mark.parametrize('kind',['occupancy','target','hop','fault'])
def test_relevant_permission_change_rechecks_and_keeps_fresh_wait_evidence(kind):
    t,s,v,e=blocked()
    if kind=='occupancy':t.occupancy['v2']={'a':[(7000,7200)]};t.claims_changed()
    elif kind=='target':t.targets['foreign']={'a':[(800,900)]};t.claims_changed()
    elif kind=='hop':t.hops['foreign']={'claims':{'a':[(800,900)]}};t.claims_changed()
    else:t.pause('a','cause',target_type='rail')
    t.dirty();s.settle(s.now_us)
    r=t.requests['one']
    if kind=='occupancy':assert r['status']=='moving'
    elif kind=='fault':assert {'kind':'fault','id':'rail:a','owner':'cause'} in r['blockers']
    else:assert {'kind':'rail','id':'a','owner':'foreign'} in r['blockers']


def test_unrelated_rail_claim_does_not_invalidate_denial():
    t,s,v,e=blocked();t.occupancy['other']={'b':[(200,400)]};t.claims_changed()
    t.dirty();s.settle(s.now_us)
    assert t.probes.work_counts['traffic_denied_checks_reused']==1


def test_cached_denial_never_bypasses_request_ownership():
    t,s,v,e=blocked();v['v1']['movement_id']='foreign'
    with pytest.raises(RuntimeError,match='ownership'):t.dispatch()


def test_cancelled_request_drops_derived_retry_record():
    t,s,v,e=blocked();t.cancel('one')
    assert not t._denied_hops
    t.dirty();s.settle(s.now_us)
    assert not t.hops


def test_relevant_passage_change_refreshes_wait_evidence():
    t,s,v,e=factory(short_links());t.probes=Probes()
    t.resources['zcu:z']='foreign';t.request('v1','dock','one');s.settle(0)
    t.dirty();s.settle(0)
    assert t.probes.work_counts['traffic_denied_checks_reused']==1
    del t.resources['zcu:z'];t.dirty();s.settle(0)
    assert t.requests['one']['status']=='moving' and t.resources['zcu:z']=='v1'


def test_route_change_invalidates_denial():
    t,s,v,e=blocked();r=t.requests['one'];r['remaining'][0]['end_um']=600
    r['target_pose']={'rail_id':'a','offset_um':600}
    t.dirty();s.settle(0)
    assert r['status']=='moving' and t.hops['v1']['target_pose']['offset_um']==600


def test_retry_cache_is_outside_physical_projection():
    t,s,v,e=blocked();before=state_projection(t)
    t._denied_hops.clear()
    assert state_projection(t)==before


def test_wait_callback_cancellation_does_not_retain_terminal_retry_record():
    t,s,v,e=factory(model([{'id':'v1','type_id':'type','rail_id':'a','offset_um':500},
                           {'id':'v2','type_id':'type','rail_id':'a','offset_um':1100}]))
    original=t.emit
    def emit(kind,payload):
        original(kind,payload)
        if kind=='traffic_wait_changed':t.cancel(payload['request_id'])
    t.emit=emit;t.request('v1','dock','one');s.settle(0)
    assert t.requests['one']['status']=='cancelled' and not t._denied_hops


def test_external_wait_evidence_change_is_corrected_by_fresh_check():
    t,s,v,e=blocked();expected=deepcopy(t.requests['one']['blockers'])
    t.requests['one']['blockers']=[];t.dirty();s.settle(0)
    assert t.requests['one']['blockers']==expected
    assert t.probes.work_counts['traffic_denied_checks_reused']==0
    assert e[-1][0]=='traffic_wait_changed'


def test_native_capture_and_repeated_restore_discard_warm_denial_cache(tmp_path):
    from test_runner import fixture
    from fab_sim.runner import build_runtime,load_config
    from fab_sim.runtime_state import capture_runtime,restore_runtime,runtime_digest
    config=load_config(fixture(tmp_path));o=build_runtime(config,lambda *args:None,initialize=False)
    t=o.traffic;vid=next(iter(t.vehicles));v=t.vehicles[vid]
    goal=next(hid for hid,h in t.holdings.items() if (h['rail_id'],h['offset_um'])!=(v['rail_id'],v['offset_um']))
    for rail in t.rails:t.pause(rail,'test',target_type='rail')
    t.request(vid,goal,'manual');o.scheduler.settle(0)
    assert t._denied_hops
    state=capture_runtime(o);digest=runtime_digest(o)
    clone=build_runtime(config,lambda *args:None,initialize=False)
    restore_runtime(clone,state,clone.actions)
    assert not clone.traffic._denied_hops and runtime_digest(clone)==digest
    clone.traffic.dirty();clone.scheduler.settle(0)
    assert clone.traffic._denied_hops and runtime_digest(clone)==digest
    restore_runtime(clone,state,clone.actions)
    assert not clone.traffic._denied_hops and runtime_digest(clone)==digest
