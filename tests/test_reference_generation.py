from copy import deepcopy
import pytest
from test_generation import inputs


def reference_inputs():
    from fab_sim.generation import generate_topology
    from fab_sim.inputs import bind_validation
    from fab_sim.sources.smat import ADAPTER, VALIDATOR, traffic_requirements
    m,p=inputs()
    reference=generate_topology(m,p,{'fleet_count':1,'improvement_limit':1})
    reference['provenance']['adapter_version']=ADAPTER
    return m,bind_validation(reference,traffic_requirements(m),VALIDATOR)


def test_reference_policy_preserves_network_infrastructure_and_motion():
    from fab_sim.generation import generate_topology
    m,p=reference_inputs();original=deepcopy(p)
    out=generate_topology(m,p,{'layout_policy':'smat_reference_slots_v1','fleet_count':2,'improvement_limit':1})
    assert p==original
    for key in ('nodes','rails','bays','zcus','vehicle_types'):
        assert out['model'][key]==p['model'][key]
    assert len(out['model']['vehicles'])==2
    assert out['provenance']['layout_policy']=='smat_reference_slots_v1'
    assert out['provenance']['rail_adaptations']==[]
    assert out['model']['policy']['pickup_us']==10_000_000
    assert out['provenance']['placement']['final_objective']
    assert out['provenance']['placement']['improvement_limit']==1
    ids={e['id'] for e in out['model']['equipment']}
    assert all(d['equipment_id'] is None or d['equipment_id'] in ids for d in out['model']['docks'])


def test_explicit_default_layout_policy_matches_omission():
    from fab_sim.generation import generate_topology
    m,p=inputs()
    a=generate_topology(m,p,{'improvement_limit':1})
    b=generate_topology(m,p,{'improvement_limit':1,'layout_policy':'single_spine_slots_v1'})
    assert a==b


@pytest.mark.parametrize('extra,match',[
    ({'geometry':{}},'geometry'),({'clearance_mm':True},'clearance'),
    ({'layout_policy':'unknown'},'policy'),({'fleet_count':100000},'fleet')])
def test_reference_policy_rejects_incompatible_settings(extra,match):
    from fab_sim.generation import generate_topology
    m,p=reference_inputs()
    with pytest.raises(ValueError,match=match):
        generate_topology(m,p,{'layout_policy':'smat_reference_slots_v1','fleet_count':1,'improvement_limit':1}|extra)


def test_reference_cache_normalizes_settings_and_binds_policy(tmp_path,monkeypatch):
    from fab_sim import topology_store as store
    m,p=reference_inputs()
    settings={'layout_policy':'smat_reference_slots_v1','fleet_count':1,'improvement_limit':1}
    a,first=store.generated(m,p,settings,tmp_path)
    def forbidden(*args):raise AssertionError('unexpected regeneration')
    monkeypatch.setattr(store,'generate_topology',forbidden)
    b,second=store.generated(m,p,settings|{'clearance_mm':'500.0','pickup_seconds':'10.0'},tmp_path)
    assert a==b and second['cache_hit']
    assert first['generation_key']==second['generation_key']
    assert 'reference_layout.py' in store._request(m,p,settings)['generator_code']
    with pytest.raises(ValueError,match='clearance'):
        store.generated(m,p,settings|{'clearance_mm':True},tmp_path)
