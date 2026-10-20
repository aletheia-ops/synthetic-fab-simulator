from copy import deepcopy
from test_traffic import factory,model
from fab_sim.routing import free_flow_costs


def test_new_offsets_reuse_node_row_and_match_exact_reference():
    t,s,vehicles,e=factory();v=deepcopy(vehicles['v1'])
    for offset in (500,1000,1500,2000,500):
        v['offset_um']=offset
        want=tuple(sorted(free_flow_costs(t.model,v,t.holdings,v['type_id']).items(),key=lambda item:(item[1],item[0])))
        assert t._idle_destinations(v)==want
    info=t._idle_routers['type'].cache_info()
    assert info['cached_entries']<=info['max_cached_entries']


def test_router_is_lazy_and_vehicle_types_do_not_share_speed_rows():
    m=model();m['vehicle_types'].append({**m['vehicle_types'][0],'id':'slow','speed_um_s':'500'})
    t,s,vehicles,e=factory(m);v=deepcopy(vehicles['v1'])
    assert t._idle_routers=={}
    fast=t._idle_destinations(v);v['type_id']='slow';slow=t._idle_destinations(v)
    assert [cost*2 for _,cost in fast]==[cost for _,cost in slow]
    assert set(t._idle_routers)=={'type','slow'}


def test_restore_drops_warm_idle_router_and_rebuilds_exact_rankings(tmp_path):
    from test_runner import fixture
    from fab_sim.runner import build_runtime,load_config
    from fab_sim.runtime_state import capture_runtime,restore_runtime,runtime_digest
    config=load_config(fixture(tmp_path));owners=build_runtime(config,lambda *a:None,initialize=True)
    owners.scheduler.settle(owners.scheduler.next_time_us)
    t=owners.traffic;v=next(iter(t.vehicles.values()));digest=runtime_digest(owners)
    expected=t._idle_destinations(v)
    assert t._idle_routers and runtime_digest(owners)==digest
    state=capture_runtime(owners)
    clone=build_runtime(config,lambda *a:None,initialize=False)
    clone.traffic._idle_destinations(next(iter(clone.traffic.vehicles.values())))
    assert clone.traffic._idle_routers
    restore_runtime(clone,state,clone.actions)
    assert clone.traffic._idle_routers=={}
    assert runtime_digest(clone)==digest
    assert clone.traffic._idle_destinations(v)==expected
