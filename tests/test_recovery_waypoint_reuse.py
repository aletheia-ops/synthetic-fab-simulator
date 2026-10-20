from copy import deepcopy
import pytest
from test_traffic_cycle_recovery import cycle
from fab_sim.probes import Probes


def faulted_cycle(monkeypatch):
    t,s,v,e=cycle();s.now_us=1;t.causes['rail']['c'].add('fault')
    probes=Probes();t.transfers.probes=probes
    router=t._router('type');calls=[];original=router.path
    def counted(*args,**kwargs):
        calls.append(1);return original(*args,**kwargs)
    monkeypatch.setattr(router,'path',counted)
    assert not t._recover_cycles()
    assert calls
    return t,s,v,e,probes,calls


@pytest.mark.parametrize('change',['occupancy','sweep','target','resource','fault'])
def test_unrelated_changes_reuse_failed_waypoint_paths(change,monkeypatch):
    t,s,v,e,p,calls=faulted_cycle(monkeypatch);before=len(calls)
    # Changes outside the attempted route must not restart its rejected geometry.
    if change=='occupancy':t.occupancy['remote']={'remote-rail':[(100,200)]};t.claims_changed()
    elif change=='sweep':t.hops['remote']={'claims':{'remote-rail':[(100,200)]}};t.claims_changed()
    elif change=='target':t.targets['remote']={'remote-rail':[(100,200)]};t.claims_changed()
    elif change=='resource':t.resources['remote-passage']='remote'
    else:t.causes['rail']['remote-rail']={'remote-fault'}
    assert not t._recover_cycles()
    assert len(calls)==before
    assert p.work_counts['traffic_cycle_waypoint_rejections_reused']>0
    assert not any(k=='traffic_route_amended' for k,_ in e)


def test_relevant_fault_recovery_wakes_cached_rejection(monkeypatch):
    t,s,v,e,p,calls=faulted_cycle(monkeypatch);before=len(calls)
    t.causes['rail']['c'].clear()
    assert t._recover_cycles()
    assert len(calls)>before
    assert len([k for k,_ in e if k=='traffic_route_amended'])==1


@pytest.mark.parametrize('problem',['custody','reservation','assignment','movement','fault'])
def test_warm_waypoint_rejection_never_skips_owner_validation(problem,monkeypatch):
    t,s,v,e,p,calls=faulted_cycle(monkeypatch)
    if problem=='custody':t.transfers.custody.locations['carrier1']='foreign'
    elif problem=='reservation':t.transfers.custody.slots['goal1']['reservation']='foreign'
    elif problem=='assignment':v['v1']['assignment_id']='foreign'
    elif problem=='movement':t.transfers.movements['d1:drop']['status']='completed'
    else:t.causes['oht']['v1'].add('fault')
    t.causes['rail']['c'].clear()
    assert not t._recover_cycles()
    assert not any(k=='traffic_route_amended' for k,_ in e)


def test_unchanged_first_hop_rejection_reused_and_new_blocker_then_release_rechecked(monkeypatch):
    t,s,v,e=cycle(parked=True);s.now_us=1;p=Probes();t.transfers.probes=p
    router=t._router('type');calls=[];original=router.path
    def counted(*args,**kwargs):
        calls.append(1);return original(*args,**kwargs)
    monkeypatch.setattr(router,'path',counted)
    assert not t._recover_cycles();before=len(calls)
    t.occupancy['remote']={'remote-rail':[(100,200)]};t.claims_changed()
    assert not t._recover_cycles()
    assert len(calls)==before
    assert p.work_counts['traffic_cycle_waypoint_rejections_reused']>0
    # A new owner with the same occupied interval is a relevant dependency.
    t.occupancy['replacement']=t.occupancy.pop('parked');t.claims_changed()
    assert not t._recover_cycles();assert len(calls)>before
    t.occupancy.pop('replacement');t.claims_changed()
    assert t._recover_cycles()
    assert any(k=='traffic_route_amended' for k,_ in e)


def test_rejection_cache_is_bounded_and_never_enters_checkpoint(tmp_path):
    from fab_sim.traffic_cycle_recovery import _remember_rejection
    from test_runner import fixture
    from fab_sim.runner import build_runtime,load_config
    from fab_sim.runtime_state import capture_runtime,restore_runtime,runtime_digest
    owners=build_runtime(load_config(fixture(tmp_path)),lambda *args:None,initialize=True)
    owners.scheduler.settle(0);t=owners.traffic
    state=capture_runtime(owners);digest=runtime_digest(owners)
    rail=next(iter(t.rails));claims=t._claim_index()
    for i in range(300):
        _remember_rejection(t,('missing-owner',str(i)),{'kind':'fault','rails':(rail,)},claims)
    assert len(t._recovery_waypoint_rejections)==256
    assert ('missing-owner','0') not in t._recovery_waypoint_rejections
    assert capture_runtime(owners)==state and runtime_digest(owners)==digest
    restore_runtime(owners,state,owners.actions)
    assert not t._recovery_waypoint_rejections
    assert runtime_digest(owners)==digest


def test_relevant_junction_owner_change_rechecks_and_release_allows_recovery(monkeypatch):
    t,s,v,e=cycle(parked=True);s.now_us=1
    router=t._router('type');calls=[];original=router.path
    def counted(*args,**kwargs):
        calls.append(1);return original(*args,**kwargs)
    monkeypatch.setattr(router,'path',counted)
    assert not t._recover_cycles();before=len(calls)
    t.resources['junction:y']='foreign'
    assert not t._recover_cycles();assert len(calls)>before
    t.occupancy.pop('parked');t.claims_changed()
    assert not t._recover_cycles()
    t.resources.pop('junction:y')
    assert t._recover_cycles()
    assert len([k for k,_ in e if k=='traffic_route_amended'])==1


@pytest.mark.parametrize('change',['origin','remaining'])
def test_mutable_route_evidence_does_not_alias_warm_rejection(change,monkeypatch):
    t,s,v,e,p,calls=faulted_cycle(monkeypatch);before=len(calls)
    r=t.requests['d1:drop']
    if change=='origin':
        v['v1']['offset_um']+=1
        t.occupancy['v1']=t._envelope(v['v1'],v['v1']['type_id']);t.claims_changed()
    else:r['remaining'][0]['start_um']+=1
    assert not t._recover_cycles()
    assert len(calls)>before
    assert not any(k=='traffic_route_amended' for k,_ in e)


def test_changed_request_target_rechecks_live_movement_ownership(monkeypatch):
    t,s,v,e,p,calls=faulted_cycle(monkeypatch)
    t.requests['d1:drop']['target_pose']['offset_um']-=1
    t.causes['rail']['c'].clear()
    assert not t._recover_cycles()
    assert not any(k=='traffic_route_amended' for k,_ in e)
