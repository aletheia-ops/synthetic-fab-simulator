from copy import deepcopy
import os
from pathlib import Path
import pytest


def small():
    return {'nodes': [{'id': 'x', 'x_um': 0, 'y_um': 0, 'zcu_id': None, 'zcu_type': 'NONE'},
                      {'id': 'y', 'x_um': 10000, 'y_um': 0, 'zcu_id': None, 'zcu_type': 'NONE'}],
            'rails': [{'id': 'a', 'from_node': 'x', 'to_node': 'y', 'length_um': 10000,
                       'bay_id': 'bay', 'curve': False, 'speed_multiplier': '1'},
                      {'id': 'b', 'from_node': 'y', 'to_node': 'x', 'length_um': 10000,
                       'bay_id': 'bay', 'curve': False, 'speed_multiplier': '1'}],
            'zcus': [], 'docks': [{'id': 'd', 'rail_id': 'a', 'offset_um': 1}],
            'vehicle_types': [{'id': 'v', 'length_um': 784, 'gap_um': 125, 'speed_um_s': '1500'}],
            'vehicles': [], 'policy': {'holding_pitch_um': 2000, 'raw_dock_clamp': True}}


def compile_model(model=None):
    from fab_sim.topology import compile_traffic
    return compile_traffic(deepcopy(model or small()))


def test_endpoint_clamp_and_coincident_docks_share_exclusion():
    model = small(); model['docks'].append({'id': 'd2', 'rail_id': 'a', 'offset_um': 0})
    out = compile_model(model)
    assert out['traffic']['common_half_um'] == 455
    assert [d['offset_um'] for d in out['docks']] == [455, 455]
    assert out['docks'][0]['source_offset_um'] == 1
    assert out['traffic']['coincident_docks'] == [['d', 'd2']]
    assert all(abs(h['offset_um']-455) >= 910 for h in out['holding_points'] if h['rail_id']=='a')


def test_short_dock_rail_and_holding_pitch_reject():
    model = small(); model['rails'][0]['length_um'] = 900
    with pytest.raises(ValueError, match='short'): compile_model(model)
    model = small(); model['policy']['holding_pitch_um'] = 909
    with pytest.raises(ValueError, match='pitch'): compile_model(model)


def test_zcu_stops_at_first_reset_and_rejects_escaping_branch():
    model = small(); model['zcus'] = [{'id': 'z'}]
    model['nodes'][0].update(zcu_id='z', zcu_type='STOP')
    model['nodes'][1].update(zcu_id='z', zcu_type='RESET')
    model['docks'] = []
    out = compile_model(model)
    assert out['traffic']['passages'] == [{'id': 'z', 'rail_ids': ['a'], 'node_ids': ['x', 'y']}]
    assert all(h['rail_id'] != 'a' for h in out['holding_points'])
    model['nodes'][1]['zcu_id'] = None
    model['nodes'][1]['zcu_type'] = 'NONE'
    with pytest.raises(ValueError, match='leaves ZCU'): compile_model(model)


def test_missing_zcu_reset_and_service_in_passage_reject():
    model = small(); model['zcus'] = [{'id': 'z'}]
    for n in model['nodes']: n.update(zcu_id='z', zcu_type='NONE')
    model['nodes'][0]['zcu_type']='STOP'; model['docks']=[]
    with pytest.raises(ValueError, match='RESET'): compile_model(model)
    model['nodes'][1]['zcu_type']='RESET'; model['docks']=small()['docks']
    with pytest.raises(ValueError, match='passage'): compile_model(model)


def test_source_idle_pose_becomes_holding_or_transient_service_pose():
    model = small(); model['vehicles']=[{'id':'v1','type_id':'v','rail_id':'a','offset_um':455}]
    out = compile_model(model)
    assert out['vehicles'][0]['initial_pose_kind']=='transient_service'
    model['vehicles'][0]['offset_um']=1500
    out = compile_model(model)
    assert out['vehicles'][0]['initial_pose_kind']=='holding'
    assert any(h['id']=='source-holding:v1' for h in out['holding_points'])


def test_excessive_or_endpoint_initial_fleet_reject():
    model = small(); model['vehicles']=[{'id':'v1','type_id':'v','rail_id':'a','offset_um':1500},
                                       {'id':'v2','type_id':'v','rail_id':'a','offset_um':2000}]
    with pytest.raises(ValueError, match='overlap'): compile_model(model)
    model['vehicles']=model['vehicles'][:1]; model['vehicles'][0]['offset_um']=454
    with pytest.raises(ValueError, match='envelope'): compile_model(model)


def test_transient_fleet_requires_enough_nonoverlapping_holding_space():
    model=small(); model['rails'][0]['length_um']=model['rails'][1]['length_um']=910
    model['docks']=[{'id':'a-dock','rail_id':'a','offset_um':455},
                    {'id':'b-dock','rail_id':'b','offset_um':455}]
    model['vehicles']=[{'id':'v1','type_id':'v','rail_id':'a','offset_um':455}]
    with pytest.raises(ValueError,match='holding capacity'): compile_model(model)


def test_mixed_types_use_largest_gap_and_reject_any_negative_gap():
    model=small(); model['vehicle_types'].append({'id':'v2','length_um':100,'gap_um':200,'speed_um_s':'900'})
    assert compile_model(model)['traffic']['common_half_um']==492
    model['vehicle_types'][0]['gap_um']=-1
    with pytest.raises(ValueError,match='gap'): compile_model(model)


@pytest.fixture(scope='module')
def actual():
    smat=os.environ.get('FAB_SMAT_WORKBOOK'); smt=os.environ.get('FAB_HVLM_WORKBOOK')
    if not smat or not smt: pytest.skip('supplied SMT and SMAT workbooks required')
    from fab_sim.sources.smt import prepare_manufacturing
    return Path(smat), prepare_manufacturing(Path(smt))


@pytest.fixture(scope='module')
def prepared(actual):
    from fab_sim.sources.smat import prepare_topology
    return prepare_topology(actual[0], actual[1], {'stocker_capacity':8})


def test_complete_source_alias_metadata_and_geometry(prepared,actual):
    from fab_sim.sources.smat import validate_topology
    out=validate_topology(prepared,actual[1]); m=out['model']
    assert len(m['nodes'])==2858 and len(m['rails'])==3424 and len(m['docks'])==22120
    assert len(m['equipment'])==1116 and len(m['vehicles'])==500
    assert {e['tool_group_id'] for e in m['equipment'] if e['kind']=='ordinary'}==set(actual[1]['model']['required_tool_group_ids'])
    assert out['provenance']['aliases']=={'DefMEt_FE_118':'DefMet_FE_118'}
    assert m['vehicle_types'][0]['length_um']==784000 and m['vehicle_types'][0]['gap_um']==125000
    assert m['vehicle_types'][0]['speed_um_s']=='1500000'
    assert m['traffic']['common_half_um']==454500
    assert m['policy']['curve_cap_um_s']=='1000000'
    assert m['policy']['pickup_us']==m['policy']['drop_us']==10000000
    assert sum(d['source_offset_um']!=d['offset_um'] for d in m['docks'])==46
    assert all(v['initial_pose_kind']=='transient_service' for v in m['vehicles'])
    assert all(e['rack_capacity']>=1 for e in m['equipment'])


def test_stocker_capacity_must_be_explicit(actual):
    from fab_sim.sources.smat import prepare_topology
    with pytest.raises(ValueError, match='stocker_capacity'): prepare_topology(*actual,{})


def test_model_or_requirements_edit_invalidates_binding(prepared,actual):
    from fab_sim.sources.smat import validate_topology
    a=deepcopy(prepared); a['model']['docks'][0]['offset_um']+=1
    with pytest.raises(ValueError,match='identity'): validate_topology(a,actual[1])
    m=deepcopy(actual[1]); m['model']['routes'][0]['steps'][0]['processing']['mean_seconds']='9'
    with pytest.raises(ValueError): validate_topology(prepared,m)


@pytest.mark.parametrize('sheet,cell,value,match',[
    ('Rail','F2',2,'multiplier'),
    ('Rail','D2','missing-node','reference'),
    ('Port','F2',-1,'range'),
    ('PortType','C2','UNKNOWN','role'),
    ('Equipment','B2','unknown-group','tool group'),
    ('Vehicle','E2',0,'envelope'),
])
def test_invalid_physical_source_rejects(actual,tmp_path,sheet,cell,value,match):
    from openpyxl import load_workbook
    from fab_sim.sources.smat import prepare_topology
    w=load_workbook(actual[0]); w[sheet][cell]=value
    p=tmp_path/'invalid.xlsx'; w.save(p); w.close()
    with pytest.raises(ValueError,match=match): prepare_topology(p,actual[1],{'stocker_capacity':8})


def test_alias_joins_original_manufacturing_id_without_rewriting_routes(prepared):
    matching=[e for e in prepared['model']['equipment'] if e['tool_group_id']=='DefMEt_FE_118']
    assert matching and all(e['physical_reference_group_id']=='DefMet_FE_118' for e in matching)


def test_source_dimensions_are_preserved_and_overrides_are_explicit(actual):
    from fab_sim.sources.smat import prepare_topology
    group='DefMEt_FE_118'
    profile={'stocker_capacity':8,'footprints':{group:{'width_mm':2000,'height_mm':1000}}}
    out=prepare_topology(actual[0],actual[1],profile)
    assert all((e['width_um'],e['height_um'])==(2000000,1000000) for e in out['model']['equipment'] if e['tool_group_id']==group)
    assert out['provenance']['overrides']==profile
    assert out['provenance']['source_tables']['Equipment']['rows']
