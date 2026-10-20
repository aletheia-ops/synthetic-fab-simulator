from copy import deepcopy
from fractions import Fraction
import os
from pathlib import Path
import pytest
from fab_sim.inputs import bind_validation
from fab_sim.sources.smt import ADAPTER, VALIDATOR
from fab_sim.sources.smat import ADAPTER as PHYSICAL_ADAPTER, VALIDATOR as PHYSICAL_VALIDATOR, traffic_requirements


def inputs():
    steps=[{'source_step_id':'1','kind':'processing','tool_group_id':'A','sampling_probability':'1','rework':None,'processing_unit':'lot','batch_max':None},
           {'source_step_id':'2','kind':'transport_abstraction','tool_group_id':'Delay_32','replacement':{'next_executable_step_id':'3'}},
           {'source_step_id':'3','kind':'processing','tool_group_id':'B','sampling_probability':'0.5','rework':{'probability':'0.2','target_step_id':'2'},'processing_unit':'lot','batch_max':None},
           {'source_step_id':'4','kind':'processing','tool_group_id':'A','sampling_probability':'1','rework':None,'processing_unit':'lot','batch_max':None}]
    m={'schema_version':1,'model':{'required_tool_group_ids':['A','B'],
        'tool_groups':[{'id':g,'batching':False,'cascading':False} for g in ('A','B')],
        'routes':[{'id':'r','product_id':'p','steps':steps}],
        'releases':[{'product_id':'p','lots_per_release':1,'interval_seconds':'60'}]},
       'provenance':{'adapter_version':ADAPTER}}
    m=bind_validation(m,{'tool_groups':['A','B']},VALIDATOR)
    p={'schema_version':1,'model':{'equipment':[{'id':g,'tool_group_id':g,'kind':'ordinary','width_um':2000000,'height_um':2000000} for g in ('A','B')],
       'vehicle_types':[{'id':'OHTV0','length_um':784000,'gap_um':125000,'speed_um_s':'1500000','carrier_capacity':1}]},
       'provenance':{'adapter_version':PHYSICAL_ADAPTER,'source_sha256':'fixture'}}
    p=bind_validation(p,traffic_requirements(m),PHYSICAL_VALIDATOR)
    return m,p


def generate(settings=None):
    from fab_sim.generation import generate_topology
    return generate_topology(*inputs(),settings or {'seed':1,'improvement_limit':2})


def test_nominal_graph_skips_delay_and_resolves_rework_target():
    from fab_sim.generation import transition_graph
    graph=transition_graph(inputs()[0],{'p':'1'})
    assert graph=={('A','B'):Fraction(1),('B','A'):Fraction(1),('B','B'):Fraction(1,10)}


def test_minimum_required_template_dimensions_ports_and_storage():
    out=generate(); m=out['model']
    ordinary=[e for e in m['equipment'] if e['kind']=='ordinary']
    assert len(ordinary)==2 and {e['tool_group_id'] for e in ordinary}=={'A','B'}
    assert len([b for b in m['bays'] if b['type']=='INTRABAY'])==1
    assert len(m['slots'])==16 and len(m['vehicles'])==1
    assert all(len([d for d in m['docks'] if d['equipment_id']==e['id']])==4 for e in ordinary)
    assert len([d for d in m['docks'] if d['kind']=='track_buffer'])==2
    assert [e['rack_capacity'] for e in m['equipment'] if e['kind'] in ('entry','exit')]==[8,8]
    assert any(n['x_um']==20000000 and n['y_um']==60000000 for n in m['nodes'])
    assert all(r['length_um']>2000000 and len(r['speed_sections'])==3 for r in m['rails'])
    assert all(v['initial_pose_kind']=='holding' for v in m['vehicles'])


def test_same_inputs_seed_reproduce_identical_artifact_and_exact_objective():
    assert generate()==generate()
    history=generate()['provenance']['placement']
    initial=Fraction(history['initial_objective']); final=Fraction(history['final_objective'])
    assert final<=initial and history['termination'] in ('local_optimum','iteration_limit')
    assert history['graph_policy']=='nominal_consecutive_rework_v1'


def test_recorded_objective_matches_independent_directed_port_routes():
    from fab_sim.routing import free_flow_path
    from fab_sim.timebase import to_ticks
    out=generate(); m=out['model']; ordinary={e['tool_group_id']:e for e in m['equipment'] if e['kind']=='ordinary'}
    value=0
    for a,b in [('A','B'),('B','A')]:
        starts=[d for d in m['docks'] if d['equipment_id']==ordinary[a]['id']]
        ends=[d for d in m['docks'] if d['equipment_id']==ordinary[b]['id']]
        value+=min(to_ticks(free_flow_path(out,s,t,'OHTV0')['seconds']) for s in starts for t in ends)+20000000
    assert Fraction(out['provenance']['placement']['final_objective'])==value


def test_complete_target_generates_seven_bays_reproducibly():
    smat=os.environ.get('FAB_SMAT_WORKBOOK'); smt=os.environ.get('FAB_HVLM_WORKBOOK')
    if not smat or not smt: pytest.skip('supplied source workbooks required')
    from fab_sim.sources.smt import prepare_manufacturing
    from fab_sim.sources.smat import prepare_topology
    from fab_sim.generation import generate_topology, load_prepared
    from fab_sim.inputs import canonical_digest
    manufacturing=prepare_manufacturing(Path(smt)); reference=prepare_topology(Path(smat),manufacturing,{'stocker_capacity':8})
    settings={'seed':7,'improvement_limit':1}
    a=generate_topology(manufacturing,reference,settings); b=generate_topology(manufacturing,reference,settings)
    assert canonical_digest(a)==canonical_digest(b)
    model=a['model']
    assert len([e for e in model['equipment'] if e['kind']=='ordinary'])==105
    assert len([bay for bay in model['bays'] if bay['type']=='INTRABAY'])==len(model['vehicles'])==7
    assert len(model['slots'])==112
    assert len([d for d in model['docks'] if d['kind']=='ordinary'])==420
    assert [len(r['steps']) for r in manufacturing['model']['routes']]==[583,343]


def test_count_override_adds_bays_without_changing_source_counts():
    settings={'seed':0,'improvement_limit':1,'counts':{'A':17}}
    out=generate(settings)
    assert len([b for b in out['model']['bays'] if b['type']=='INTRABAY'])==2
    assert len([e for e in out['model']['equipment'] if e['tool_group_id']=='A'])==17
    assert len(out['model']['vehicles'])==2


def test_footprint_that_cannot_fit_rejects_without_shrinking():
    with pytest.raises(ValueError,match='footprint'):
        generate({'seed':1,'improvement_limit':1,'footprints':{'A':{'width_mm':6000,'height_mm':2000}}})
    with pytest.raises(ValueError,match='footprint'):
        generate({'seed':1,'improvement_limit':1,'footprints':{'A':{'width_mm':'0.00001','height_mm':2000}}})


def test_buffer_geometry_cannot_coincide_with_service_docks():
    from fab_sim.generation import DEFAULT_GEOMETRY
    profile=deepcopy(DEFAULT_GEOMETRY); profile['buffer_positions_mm']=[4500,57000]
    with pytest.raises(ValueError,match='buffer'):
        generate({'seed':1,'improvement_limit':1,'geometry':profile})


def test_fleet_larger_than_holding_capacity_rejects():
    with pytest.raises(ValueError,match='fleet'):
        generate({'seed':1,'improvement_limit':1,'fleet_count':10000})


def test_invalid_settings_and_stale_reference_reject():
    from fab_sim.generation import generate_topology
    with pytest.raises(ValueError,match='seed'): generate({'seed':True})
    with pytest.raises(ValueError,match='unknown'): generate({'surprise':1})
    m,p=inputs(); p=deepcopy(p); p['model']['equipment'][0]['width_um']+=1
    with pytest.raises(ValueError,match='identity'): generate_topology(m,p,{'seed':1})


def test_editing_artifact_does_not_mutate_future_default_geometry():
    from fab_sim.generation import DEFAULT_GEOMETRY
    original=deepcopy(DEFAULT_GEOMETRY)
    before=generate()
    try:
        edited=generate(); edited['provenance']['resolved_geometry']['dock_offsets_mm'][0]=-1400
        assert generate()==before
    finally:
        DEFAULT_GEOMETRY.clear(); DEFAULT_GEOMETRY.update(original)


def test_explicit_extra_buffer_positions_preserve_existing_physical_components():
    from fab_sim.generation import DEFAULT_GEOMETRY
    baseline=generate({'seed':1,'improvement_limit':1,'fleet_count':1})
    geometry=deepcopy(DEFAULT_GEOMETRY)
    geometry['buffer_positions_mm']=[54000,57000,51000,52500,55500,58500]
    expanded=generate({'seed':1,'improvement_limit':1,'fleet_count':1,
        'geometry':geometry,'rack_capacities':{'A':6,'B':6}})
    a,b=baseline['model'],expanded['model']
    for field in ('nodes','rails','bays','slots','vehicle_types','vehicles','policy'):
        assert a[field]==b[field],field
    assert [d for d in a['docks'] if d['kind']!='track_buffer']==[d for d in b['docks'] if d['kind']!='track_buffer']
    old_buffers=[d for d in a['docks'] if d['kind']=='track_buffer']
    new_buffers=[d for d in b['docks'] if d['kind']=='track_buffer']
    assert len(new_buffers)==3*len(old_buffers)
    assert all(d in new_buffers for d in old_buffers)
    assert all(d['carrier_capacity']==1 for d in new_buffers)
    from fab_sim.runner import allocate
    _,custody,_,_,_=allocate(b)
    assert len([s for s in custody.slots.values() if s['kind']=='track_buffer'])==6
    assert all(e['rack_capacity']==6 for e in b['equipment'] if e['kind']=='ordinary')


@pytest.mark.parametrize('positions',[[],[54000,54000],[54000,60000],[54000,49500]])
def test_explicit_buffer_positions_reject_empty_duplicate_outside_or_service_overlap(positions):
    from fab_sim.generation import DEFAULT_GEOMETRY
    geometry=deepcopy(DEFAULT_GEOMETRY);geometry['buffer_positions_mm']=positions
    with pytest.raises(ValueError,match='buffer'):
        generate({'seed':1,'improvement_limit':1,'geometry':geometry})
