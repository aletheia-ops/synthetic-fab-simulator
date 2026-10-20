"""Guarded same-rail advances preserve assigned transfer ownership and clocks."""
from copy import deepcopy
from types import SimpleNamespace
from fractions import Fraction
import pytest
from test_traffic import factory, model, drain

def assigned():
    t,s,v,e=factory(model([{'id':'v1','type_id':'type','rail_id':'a','offset_um':500},
                          {'id':'v2','type_id':'type','rail_id':'a','offset_um':1100}]))
    t.manage_idle=False
    path=t._router('type').path(v['v1'],t.docks['dock'])
    t.request('v1','dock','d:drop',path=path)
    v['v1'].update(assignment_id='d',carrier_slot_id='vehicle1')
    custody=SimpleNamespace(slots={'vehicle1':{'carrier_id':'carrier','reservation':'d'},
                                  'dock':{'carrier_id':None,'reservation':'d'}},locations={'carrier':'vehicle1'})
    movement={'id':'d:drop','demand_id':'d','vehicle_id':'v1','phase':'drop','path':deepcopy(path),
              'status':'moving','cancelled':False,'target_pose':dict(t.requests['d:drop']['target_pose'])}
    demand={'id':'d','status':'picked_up','carrier_id':'carrier','vehicle_id':'v1',
            'destination_id':'dock','rack_slot_id':None,'loaded_path':deepcopy(path)}
    t.transfers=SimpleNamespace(movements={'d:drop':movement},demands={'d':demand},custody=custody,dirty=lambda:None)
    return t,s,v,e

def test_assigned_blocked_hop_advances_to_guarded_temporary_stop_and_retains_route():
    t,s,v,e=assigned();r=t.requests['d:drop'];route=deepcopy(r['remaining']);custody=deepcopy(t.transfers.custody.slots)
    s.settle(0)
    assert 'v1' in t.hops
    assert t.hops['v1']['target_pose']=={'rail_id':'a','offset_um':900}
    assert t.hops['v1']['claims']=={'a':[(400,1000)]}
    assert t.hops['v1']['tail']==[{**route[0],'start_um':900}]+route[1:]
    assert r['target_pose']=={'rail_id':'a','offset_um':5000} and t.transfers.custody.slots==custody
    s.settle(400000)
    assert v['v1']['offset_um']==900 and r['status']=='pending'
    assert r['cumulative_seconds']==Fraction(2,5) and not t.hops
    # Physical permission change wakes the remaining exact route.
    v['v2']['offset_um']=7000;t.occupancy['v2']=t._envelope(v['v2'],'type');t.claims_changed();drain(s)
    assert r['status']=='complete' and s.now_us==4500000
    assert t.transfers.movements['d:drop']['path']['sublinks']==route
    assert t.transfers.custody.slots==custody

@pytest.mark.parametrize('problem',['cancelled','custody','reservation','movement','oht_fault','rail_fault','unassigned','no_gap','target_claim','moving_sweep'])
def test_temporary_stop_never_bypasses_owner_fault_or_body_permissions(problem):
    t,s,v,e=assigned();r=t.requests['d:drop']
    if problem=='cancelled':r['cancel_requested']=True
    elif problem=='custody':t.transfers.custody.locations['carrier']='foreign'
    elif problem=='reservation':t.transfers.custody.slots['dock']['reservation']='foreign'
    elif problem=='movement':t.transfers.movements[r['id']]['status']='completed'
    elif problem=='oht_fault':t.causes['oht']['v1'].add('fault')
    elif problem=='rail_fault':t.causes['rail']['a'].add('fault')
    elif problem=='unassigned':v['v1']['assignment_id']=None
    elif problem=='no_gap':v['v2']['offset_um']=700;t.occupancy['v2']=t._envelope(v['v2'],'type');t.claims_changed()
    elif problem=='target_claim':t.targets['reserved']={'a':[(600,800)]};t.claims_changed()
    else:t.hops['foreign']={'claims':{'a':[(600,800)]}};t.claims_changed()
    s.settle(0)
    assert 'v1' not in t.hops and r['status']=='pending'

def test_cancel_temporary_hop_retains_sweep_and_normal_safe_arrival():
    t,s,v,e=assigned();s.settle(0);s.settle(200000)
    t.transfers.demands['d']['status']='cancelled';t.cancel('d:drop')
    assert t.hops['v1']['claims']=={'a':[(400,1000)]}
    s.settle(400000)
    assert v['v1']['offset_um']==900 and t.requests['d:drop']['status']=='cancelled'
    assert not t.hops and not t.resources

def test_temporary_stop_contact_does_not_retry_on_clock_only_changes():
    from fab_sim.probes import Probes
    t,s,v,e=assigned();t.transfers.probes=Probes();s.settle(0);s.settle(400000)
    attempts=t.transfers.probes.work_counts.get('traffic_guarded_stop_checks',0)
    starts=sum(k=='traffic_hop_started' for k,p in e)
    for tick in (500000,600000,700000):s.settle(tick);t.dirty();s.settle(tick)
    assert sum(k=='traffic_hop_started' for k,p in e)==starts==1
    assert t.transfers.probes.work_counts.get('traffic_guarded_stop_checks',0)==attempts

def test_guarded_stop_rejects_stale_demand_vehicle_ownership():
    t,s,v,e=assigned();t.transfers.demands['d']['vehicle_id']='v2';s.settle(0)
    assert 'v1' not in t.hops
