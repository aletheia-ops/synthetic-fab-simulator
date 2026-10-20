"""Service overlap keeps exact boundaries without scanning unrelated docks."""
import pytest
from test_traffic import factory, model
from fab_sim.geometry import overlaps
from fab_sim.probes import domain_digest, state_projection


def test_idle_parking_does_not_enumerate_all_docks_after_preparation():
    class Docks(dict):
        def values(self):
            raise AssertionError('idle parking enumerated all service docks')
    m=model();m['docks'].extend({'id':f'other:{i}','rail_id':'b','offset_um':5000} for i in range(1000))
    t,s,v,e=factory(m);t.manage_idle=True;t.docks=Docks(t.docks)
    assert t._plan_idle() is False
    assert not t.active and not e


@pytest.mark.parametrize('offset', [4799,4800,4801,4999,5000,5199,5200,5201])
def test_indexed_service_overlap_retains_open_interval_boundaries(offset):
    t,s,v,e=factory();occupied={'a':[(offset-100,offset+100)]}
    expected=overlaps(occupied['a'][0],(4900,5100))
    assert t._at_service(occupied) is expected


def test_indexed_service_overlap_matches_reference_for_multiple_rails_and_duplicate_offsets():
    m=model();m['docks'].extend([
        {'id':'duplicate','rail_id':'a','offset_um':5000},
        {'id':'second','rail_id':'a','offset_um':7000},
        {'id':'reverse','rail_id':'b','offset_um':2000}])
    t,s,v,e=factory(m);half=m['traffic']['common_half_um']
    for rid in ('a','b','no-docks'):
        for offset in range(100,9900,13):
            occupied={rid:[(offset-100,offset+100)]}
            expected=any(d['rail_id'] in occupied and overlaps(occupied[d['rail_id']][0],
                (d['offset_um']-half,d['offset_um']+half)) for d in t.docks.values())
            assert t._at_service(occupied) is expected


def test_derived_dock_offsets_do_not_enter_physical_digest():
    t,s,v,e=factory();before=domain_digest(state_projection(t))
    t._dock_offsets_by_rail={'derived':[1,2,3]}
    assert domain_digest(state_projection(t))==before
    t.occupancy['v1']['a']=[(401,601)]
    assert domain_digest(state_projection(t))!=before
