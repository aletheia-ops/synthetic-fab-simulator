from fractions import Fraction
import pytest
from test_routing import ring
from fab_sim.routing_index import FreeFlowCosts
from fab_sim.routing import free_flow_costs


def cycle(count):
    model=ring();model['rails']=[dict(model['rails'][0],id=str(i),from_node=str(i),to_node=str((i+1)%count)) for i in range(count)]
    return model


def test_index_matches_directed_costs_sections_ties_and_unreachable():
    model=ring();model['rails'][0]['speed_sections']=[{'start_um':0,'end_um':400,'curve':False},
        {'start_um':400,'end_um':1000,'curve':True}]
    model['rails'].append(dict(model['rails'][1],id='parallel'))
    model['rails'].append(dict(model['rails'][0],id='island',from_node='x',to_node='y'))
    router=FreeFlowCosts(model,'v')
    destinations={str(i):{'rail_id':rail,'offset_um':offset} for i,(rail,offset) in enumerate(
        [('a',0),('a',100),('a',700),('a',1000),('b',0),('b',500),('b',2000),('parallel',100),('island',100)])}
    for rail in ('a','b','island'):
        for offset in (0,100,900):
            start={'rail_id':rail,'offset_um':offset}
            assert router.costs(start,destinations)==free_flow_costs(model,start,destinations,'v')


def test_more_than_32_origins_do_not_thrash_and_offsets_share_rows():
    model=cycle(65);router=FreeFlowCosts(model,'v')
    destination={'target':{'rail_id':'0','offset_um':500}}
    for _ in range(2):
        for i in range(64):
            for offset in (100,200):
                start={'rail_id':str(i),'offset_um':offset}
                assert router.costs(start,destination)==free_flow_costs(model,start,destination,'v')
    stats=router.cache_info()
    assert stats['searches']==64 and stats['hits']==192 and stats['evictions']==0
    assert stats['projected_destinations']==256


def test_retained_row_cache_has_hard_entry_budget_and_exact_evicted_results():
    model=cycle(5);router=FreeFlowCosts(model,'v',max_cached_entries=10)
    destination={'target':{'rail_id':'0','offset_um':500}}
    for i in range(5):
        start={'rail_id':str(i),'offset_um':100}
        assert router.costs(start,destination)==free_flow_costs(model,start,destination,'v')
        assert router.cache_info()['cached_entries']<=10
    assert router.cache_info()['evictions']==3
    start={'rail_id':'0','offset_um':100}
    assert router.costs(start,destination)==free_flow_costs(model,start,destination,'v')
    assert router.cache_info()['searches']==6


@pytest.mark.parametrize('budget',[True,0,-1,1.5])
def test_invalid_cache_budget_rejected(budget):
    with pytest.raises(ValueError):FreeFlowCosts(ring(),'v',max_cached_entries=budget)


@pytest.mark.parametrize('offset',[True,-1,1001,1.5])
def test_invalid_pose_rejected(offset):
    router=FreeFlowCosts(ring(),'v')
    with pytest.raises(ValueError):router.costs({'rail_id':'a','offset_um':offset},{})


def test_index_snapshots_physical_inputs_and_new_index_observes_changes():
    model=ring();router=FreeFlowCosts(model,'v')
    start={'rail_id':'a','offset_um':0};targets={'target':{'rail_id':'a','offset_um':900}}
    original=router.costs(start,targets)
    model['vehicle_types'][0]['speed_um_s']='300'
    assert router.costs(start,targets)==original
    assert FreeFlowCosts(model,'v').costs(start,targets)==free_flow_costs(model,start,targets,'v')
    assert FreeFlowCosts(model,'v').costs(start,targets)!=original


def test_checkpoint_rebuilds_warm_delivery_index_without_changing_state(tmp_path):
    from test_runner import fixture
    from fab_sim.runner import build_runtime,load_config
    from fab_sim.runtime_state import capture_runtime,restore_runtime,runtime_digest
    config=load_config(fixture(tmp_path));owner=build_runtime(config,lambda *args:None,initialize=True)
    owner.scheduler.settle(0)
    carrier=next(iter(owner.manufacturing.lots.values()))['carrier_id']
    tool=next(iter(owner.manufacturing.tools))
    before=runtime_digest(owner);expected=owner.flow.delivery_cost(carrier,tool)
    assert owner.flow._delivery_router is not None
    assert runtime_digest(owner)==before
    state=capture_runtime(owner)
    clone=build_runtime(config,lambda *args:None,initialize=False)
    restore_runtime(clone,state,clone.actions)
    assert clone.flow._delivery_router is None
    assert clone.flow.delivery_cost(carrier,tool)==expected
    assert clone.flow._delivery_router is not owner.flow._delivery_router
    assert runtime_digest(clone)==before


def test_oversized_row_is_not_retained_and_empty_query_does_not_search():
    router=FreeFlowCosts(cycle(5),'v',max_cached_entries=3)
    start={'rail_id':'0','offset_um':100}
    assert router.costs(start,{})=={}
    assert router.cache_info()['searches']==0
    targets={'target':{'rail_id':'1','offset_um':500}}
    assert router.costs(start,targets)==router.costs(start,targets)
    assert router.cache_info()['searches']==2
    assert router.cache_info()['cached_entries']==0


def test_partial_cost_cache_eviction_and_zero_remain_exact():
    from fab_sim.derived_cache import DerivedCache
    model=ring();storage=DerivedCache(1);router=FreeFlowCosts(model,'v',cache=storage)
    start={'rail_id':'a','offset_um':100};targets={'same':dict(start),'later':{'rail_id':'a','offset_um':900}}
    for _ in range(2):assert router.costs(start,targets)==free_flow_costs(model,start,targets,'v')
    assert router.costs(start,targets)['same']==Fraction(0)
    assert storage.stats()['retained_bytes']<=1
