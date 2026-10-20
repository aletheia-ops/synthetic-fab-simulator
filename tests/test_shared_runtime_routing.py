"""Exact shared static routing, including caller isolation and bounded reuse."""
from fractions import Fraction
import pytest
from test_routing import ring
from fab_sim.routing import free_flow_costs_to,free_flow_path
from fab_sim.routing_index import FreeFlowCosts
from fab_sim.derived_cache import DerivedCache


def test_reverse_rows_reuse_across_offsets_and_match_independent_routing():
    model=ring();model['rails'].append(dict(model['rails'][1],id='parallel'))
    model['rails'].append(dict(model['rails'][0],id='island',from_node='x',to_node='y'))
    model['rails'][0]['speed_sections']=[{'start_um':0,'end_um':400,'curve':False},
                                        {'start_um':400,'end_um':1000,'curve':True}]
    router=FreeFlowCosts(model,'v')
    origins={str(i):{'rail_id':rid,'offset_um':off} for i,(rid,off) in enumerate(
        [('a',0),('a',100),('a',1000),('b',300),('parallel',700),('island',200)])}
    for offset in (0,100,400,900,1000):
        end={'rail_id':'a','offset_um':offset}
        assert router.costs_to(origins,end)==free_flow_costs_to(model,origins,end,'v')
    assert router.stats['reverse_searches']==1
    assert router.costs_to({}, {'rail_id':'a','offset_um':300})=={}


def test_shared_paths_preserve_ties_sections_and_caller_isolation():
    model=ring();model['rails'].append(dict(model['rails'][1],id='c'))
    model['rails'][0]['speed_sections']=[{'start_um':0,'end_um':500,'curve':False},
                                        {'start_um':500,'end_um':1000,'curve':True}]
    cache=DerivedCache(100000);first=FreeFlowCosts(model,'v',cache=cache)
    second=FreeFlowCosts(model,'v',cache=cache)
    start={'rail_id':'a','offset_um':800};end={'rail_id':'a','offset_um':100}
    expected=free_flow_path(model,start,end,'v')
    result=first.path(start,end);assert result==expected
    result['sublinks'][0]['end_um']=-1
    assert second.path(start,end)==expected
    assert first.stats['path_searches']==1 and second.stats['path_searches']==0
    assert first.path(start,start)=={'seconds':Fraction(0),'sublinks':[]}


def test_reverse_and_path_cache_respect_tiny_budget_and_invalid_poses():
    model=ring();cache=DerivedCache(1);router=FreeFlowCosts(model,'v',cache=cache)
    start={'rail_id':'a','offset_um':800};end={'rail_id':'a','offset_um':100}
    assert router.path(start,end)==free_flow_path(model,start,end,'v')
    assert router.path(start,end)==free_flow_path(model,start,end,'v')
    assert router.stats['path_searches']==2 and cache.stats()['entries']==0
    for offset in (True,-1,1001,1.5):
        with pytest.raises(ValueError):router.costs_to({}, {'rail_id':'a','offset_um':offset})
        with pytest.raises(ValueError):router.path(start,{'rail_id':'a','offset_um':offset})


def test_shared_runtime_consumers_use_one_router_and_restore_cold(tmp_path):
    from test_runner import fixture
    from fab_sim.runner import build_runtime,load_config
    from fab_sim.runtime_state import capture_runtime,restore_runtime,runtime_digest
    config=load_config(fixture(tmp_path));owner=build_runtime(config,lambda *args:None,initialize=True)
    owner.scheduler.settle(0);t=owner.transfers;traffic=owner.traffic
    router=t._router()
    assert traffic._router(t.vehicle_type) is router
    carrier=next(iter(owner.manufacturing.lots.values()))['carrier_id']
    owner.flow.delivery_cost(carrier,next(iter(owner.manufacturing.tools)))
    assert owner.flow._delivery_router is router
    before=runtime_digest(owner);state=capture_runtime(owner)
    clone=build_runtime(config,lambda *args:None,initialize=False)
    restore_runtime(clone,state,clone.actions)
    assert not clone.traffic._idle_routers
    assert clone.transfers._router() is clone.traffic._router(t.vehicle_type)
    assert clone.transfers._router() is not router
    assert runtime_digest(clone)==before
