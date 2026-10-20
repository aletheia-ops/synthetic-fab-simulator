from copy import deepcopy
from test_routing import ring
from test_traffic import factory
from fab_sim.routing_index import FreeFlowCosts


def test_exit_order_matches_pose_sort_and_shared_exit_rails():
    from fab_sim.derived_cache import DerivedCache
    model=ring();model['rails'].append(dict(model['rails'][0],id='parallel'))
    model['rails'].append(dict(model['rails'][0],id='island',from_node='x',to_node='y'))
    holdings={str(n):{'rail_id':rail,'offset_um':offset}
              for n,(rail,offset) in enumerate((('a',0),('a',100),('a',900),('b',500),('parallel',100),('island',100)))}
    router=FreeFlowCosts(model,'v',cache=DerivedCache(65536))
    for rail in ('a','parallel','island'):
        for offset in (0,100,500,900,1000):
            start={'rail_id':rail,'offset_um':offset}
            assert tuple(router.idle_order(start,holdings))==tuple(sorted(router.costs(start,holdings).items(),key=lambda x:(x[1],x[0])))


def test_changed_target_geometry_does_not_reuse_stale_rank():
    router=FreeFlowCosts(ring(),'v')
    targets={'target':{'rail_id':'a','offset_um':100}}
    start={'rail_id':'a','offset_um':0}
    assert tuple(router.idle_order(start,targets))==tuple(router.costs(start,targets).items())
    targets['target']['offset_um']=900
    assert tuple(router.idle_order(start,targets))==tuple(router.costs(start,targets).items())


def test_idle_orders_evict_without_changing_results():
    from fab_sim.derived_cache import DerivedCache
    c=DerivedCache(1024);router=FreeFlowCosts(ring(),'v',cache=c)
    start={'rail_id':'a','offset_um':50}
    for n in range(100):
        targets={str(n):{'rail_id':'b','offset_um':n}}
        assert tuple(router.idle_order(start,targets))==tuple(router.costs(start,targets).items())
        assert c.stats()['retained_bytes']<=1024
