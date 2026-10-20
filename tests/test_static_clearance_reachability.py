import pytest
from test_routing import ring
from test_clearance_eligibility_cost import fixture
from fab_sim.routing_index import FreeFlowCosts
from fab_sim.derived_cache import DerivedCache


def test_exact_reachability_handles_backward_offsets_and_disconnected_rails():
    model=ring()
    model['rails'].append(dict(model['rails'][0],id='island',from_node='x',to_node='y'))
    router=FreeFlowCosts(model,'v')
    assert router.reachable({'rail_id':'a','offset_um':900},{'rail_id':'a','offset_um':100})
    assert router.reachable({'rail_id':'island','offset_um':100},{'rail_id':'island','offset_um':900})
    assert not router.reachable({'rail_id':'island','offset_um':900},{'rail_id':'island','offset_um':100})
    assert not router.reachable({'rail_id':'a','offset_um':0},{'rail_id':'island','offset_um':0})
    assert router.stats['path_searches']==0


def test_repeated_reachability_uses_bounded_cache_including_false_answers(monkeypatch):
    model=ring()
    model['rails'].append(dict(model['rails'][0],id='island',from_node='x',to_node='y'))
    router=FreeFlowCosts(model,'v')
    start={'rail_id':'a','offset_um':0};end={'rail_id':'island','offset_um':0}
    assert not router.reachable(start,end)
    monkeypatch.setattr(router,'_row',lambda node:pytest.fail('unchanged geometry searched again'))
    assert not router.reachable(start,end)
    for offset in (True,-1,1001,1.5):
        with pytest.raises(ValueError):router.reachable(start,{'rail_id':'a','offset_um':offset})


def test_tiny_budget_keeps_reachability_correct_without_retaining_entries():
    cache=DerivedCache(1);router=FreeFlowCosts(ring(),'v',cache=cache)
    for _ in range(2):assert router.reachable({'rail_id':'a','offset_um':900},{'rail_id':'a','offset_um':100})
    assert cache.stats()['entries']==0


def test_clearance_repeats_live_checks_without_reconstructing_unchanged_routes():
    pool,t,m,c,lots,member=fixture()
    assert pool._compatible(member,'protected:0')
    assert pool._compatible(member,'protected:0')
    assert t._router().stats['path_searches']==0
    m.rules.routes['r']['dedication_components']=[{'id':'ded','source_step_ids':['2']}]
    lots['low']['dedication']['ded']='other'
    t.docks['other:port']['role']='OUT'
    assert not pool._compatible(member,'protected:0')
    t.docks['other:port']['role']='BOTH'
    assert pool._compatible(member,'protected:0')


def test_custom_path_override_still_controls_clearance(monkeypatch):
    pool,t,m,c,lots,member=fixture()
    assert pool._compatible(member,'protected:0')
    monkeypatch.setattr(t,'_path',lambda a,b:None)
    assert not pool._compatible(member,'protected:0')


def test_custom_router_path_override_still_controls_clearance(monkeypatch):
    pool,t,m,c,lots,member=fixture()
    assert pool._compatible(member,'protected:0')
    def unavailable(self,start,end):raise ValueError('unreachable directed destination')
    monkeypatch.setattr(FreeFlowCosts,'path',unavailable)
    assert not pool._compatible(member,'protected:0')


def test_transfers_shaped_extension_without_reachability_keeps_path_contract():
    from types import SimpleNamespace
    pool,t,m,c,lots,member=fixture()
    pool.transfers=SimpleNamespace(docks=t.docks,_pose=t._pose,_target_valid=t._target_valid,_path=t._path)
    assert pool._compatible(member,'protected:0')
    pool.transfers._path=lambda a,b:None
    assert not pool._compatible(member,'protected:0')


def test_class_level_transfer_path_override_controls_clearance(monkeypatch):
    from fab_sim.transfers import Transfers
    pool,t,m,c,lots,member=fixture()
    assert pool._compatible(member,'protected:0')
    monkeypatch.setattr(Transfers,'_path',lambda self,a,b:None)
    assert not pool._compatible(member,'protected:0')
