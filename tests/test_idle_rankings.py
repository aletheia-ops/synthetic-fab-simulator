"""Free-flow rankings may be reused; physical claims are checked each time."""
from copy import deepcopy
from test_traffic import factory,model
from fab_sim.routing import free_flow_costs
from fab_sim.probes import domain_digest,state_projection


def test_idle_ranking_reuses_same_pose_search_and_recomputes_after_pose_change(monkeypatch):
    from fab_sim.routing_index import FreeFlowCosts
    t,s,vehicles,e=factory();v=vehicles['v1'];calls=[]
    original=FreeFlowCosts.idle_order
    def counted(*args,**kwargs):
        calls.append(1)
        return original(*args,**kwargs)
    monkeypatch.setattr(FreeFlowCosts,'idle_order',counted)
    want=tuple(sorted(free_flow_costs(t.model,v,t.holdings,v['type_id']).items(),key=lambda item:(item[1],item[0])))
    assert t._idle_destinations(v)==want
    assert t._idle_destinations(v)==want and len(calls)==1
    v['offset_um']=1500
    expected=tuple(sorted(free_flow_costs(t.model,v,t.holdings,v['type_id']).items(),key=lambda item:(item[1],item[0])))
    assert t._idle_destinations(v)==expected and len(calls)==2


def test_idle_rankings_separate_vehicle_types_and_bound_retained_poses():
    m=model();m['vehicle_types'].append({**m['vehicle_types'][0],'id':'slow','speed_um_s':'500'})
    t,s,vehicles,e=factory(m);v=deepcopy(vehicles['v1'])
    fast=t._idle_destinations(v);v['type_id']='slow';slow=t._idle_destinations(v)
    assert [hid for hid,cost in fast]==[hid for hid,cost in slow]
    assert [cost*2 for hid,cost in fast]==[cost for hid,cost in slow]
    for offset in range(100,300):
        v['offset_um']=offset;t._idle_destinations(v)
    assert len(t._idle_rankings)<=128


def test_idle_rankings_leave_physical_digest_unchanged():
    t,s,v,e=factory();before=domain_digest(state_projection(t))
    t._idle_destinations(v['v1'])
    assert domain_digest(state_projection(t))==before


def test_idle_cache_does_not_reuse_occupied_target_permission():
    t,s,vehicles,e=factory();t.manage_idle=True
    vehicles['v1']['offset_um']=5000;t.occupancy['v1']={'a':[(4900,5100)]}
    first=next((hid,cost) for hid,cost in t._idle_destinations(vehicles['v1']) if cost>0)
    hid,cost=first
    t.occupancy['blocker']=t._envelope(t.holdings[hid],'type')
    t._plan_idle()
    assert not t.active or t.requests[t.active['v1']]['destination_pose_id']!=hid
