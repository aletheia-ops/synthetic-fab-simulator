"""Configuration-driven physical runs using prepared, content-bound inputs."""
import json
from copy import deepcopy
import pytest
from fab_sim.inputs import bind_validation,canonical_text
from fab_sim.sources.smt import ADAPTER,VALIDATOR
from test_generation import inputs
from test_route_rules import step


def fixture(tmp_path,*,model_updates=None,generation_settings=None,**overrides):
    from fab_sim.generation import generate_topology
    m,p=inputs()
    m['model'].update(required_tool_group_ids=['A'],tool_groups=[{'id':'A','area':'a','batching':False,
        'cascading':False,'initial_setup':None,'loading_us':2,'unloading_us':3}],
        routes=[{'id':'r','product_id':'p','steps':[step(1)],'dedication_components':[]}],
        maintenance=[],breakdowns=[],setup_transitions=[],releases=[{'id':'a','product_id':'p','route_id':'r',
        'priority':1,'super_hot':False,'wafer_count':25,'first_at_us':0,'interval_seconds':'1',
        'lots_per_release':1,'due_allowance_us':100000000}])
    m['model'].update(model_updates or {})
    m=bind_validation(m,{'tool_groups':m['model']['required_tool_group_ids']},VALIDATOR)
    from fab_sim.sources.smat import traffic_requirements,VALIDATOR as PV
    p=bind_validation(p,traffic_requirements(m),PV)
    top=generate_topology(m,p,{'seed':1,'improvement_limit':1,'entry_capacity':2,'exit_capacity':1,
                             **(generation_settings or {})})
    for name,value in [('manufacturing',m),('topology',top),('reference',p)]:
        (tmp_path/(name+'.json')).write_text(canonical_text(value))
    config={'inputs':{'manufacturing_path':'manufacturing.json','topology_path':'topology.json'},
        'workload':{'count':2},'execution':{'horizon_us':1000000000,'master_seed':7},
        'output':{'directory':'result','run_id':'fixture'}}
    config.update(overrides)
    path=tmp_path/'run.json';path.write_text(canonical_text(config));return path


def test_local_run_allocates_declared_capacity_and_drains_real_carriers(tmp_path):
    from fab_sim.runner import run
    result=run(fixture(tmp_path))
    assert result['outcome']=='Completed' and result['exited_count']==2
    assert result['domain_digest_version']=='full-physical-state-archive-v9'
    assert result['capacity']['entry']==2 and result['capacity']['exit']==1
    rows=[json.loads(x) for x in (tmp_path/'result/events.jsonl').read_text().splitlines()]
    assert sum(r['event_type']=='lot_exited' for r in rows)==2
    assert {'carrier_exchange_completed','processing_completed'}<={r['event_type'] for r in rows}
    assert not any(r['event_type'].startswith('traffic_') for r in rows)
    oht=[json.loads(x) for x in (tmp_path/'result/oht.jsonl').read_text().splitlines()]
    assert {'oht_dispatched','oht_segment','oht_load_completed','oht_unload_completed'}<={r['event_type'] for r in oht}
    assert sorted(r['event_sequence'] for r in rows+oht)==list(range(1,len(rows)+len(oht)+1))
    assert result['resolved_config']['inputs']['manufacturing_path']==str(tmp_path/'manufacturing.json')
    assert (tmp_path/'result/oht.jsonl').stat().st_size>0


def test_preflight_reports_when_entry_cannot_stage_remainder_safe_root_batch(tmp_path):
    from fab_sim.preflight import preflight
    from fab_sim.generation import DEFAULT_GEOMETRY
    path=fixture(tmp_path,model_updates={
        'routes':[{'id':'r','product_id':'p','steps':[step(1,processing_unit='batch',batch_min=50,batch_max=75)],
                   'dedication_components':[]}],
        'tool_groups':[{'id':'A','area':'a','batching':True,'cascading':False,
                       'initial_setup':None,'loading_us':2,'unloading_us':3}]},
        generation_settings={'geometry':{**DEFAULT_GEOMETRY,'buffer_positions_mm':[54000,57000,51000,52500]}},
        workload={'count':3})
    result=preflight(path)
    finding=next(f for f in result['findings'] if f['code']=='finite_batch_remainder_staging_risk')
    assert finding['severity']=='warning' and finding['evidence']['required_entry_capacity']==3
    assert finding['evidence']['entry_capacity']==2


def test_config_relative_paths_and_bad_supplied_path_do_not_fallback(tmp_path):
    from fab_sim.runner import run
    path=fixture(tmp_path,inputs={'manufacturing_path':'manufacturing.json','topology_path':'missing.json',
                              'physical_reference_path':'reference.json'})
    with pytest.raises((ValueError,FileNotFoundError)):run(path)
    assert not (tmp_path/'result').exists()


def test_edited_prepared_content_rejects_before_output_creation(tmp_path):
    from fab_sim.runner import run
    path=fixture(tmp_path);top=json.loads((tmp_path/'topology.json').read_text());top['model']['equipment'][0]['rack_capacity']=999
    (tmp_path/'topology.json').write_text(json.dumps(top))
    with pytest.raises(ValueError,match='identity'):run(path)
    assert not (tmp_path/'result').exists()


@pytest.mark.parametrize('overrides',[{'execution':{'horizon_us':0}}, {'execution':{'horizon_us':100,'typo':1}},
    {'output':{'directory':'result','live':True}}, {'workload':{'mode':'unknown'}}])
def test_invalid_or_unsupported_run_config_rejects(tmp_path,overrides):
    from fab_sim.runner import run
    with pytest.raises(ValueError):run(fixture(tmp_path,**overrides))
    assert not (tmp_path/'result').exists()


def test_existing_output_is_preserved(tmp_path):
    from fab_sim.runner import run
    path=fixture(tmp_path);(tmp_path/'result').mkdir();(tmp_path/'result/marker').write_text('keep')
    with pytest.raises(FileExistsError):run(path)
    assert (tmp_path/'result/marker').read_text()=='keep'


def test_horizon_is_inclusive_and_operator_stop_remains_incomplete(tmp_path):
    from fab_sim.runner import run
    path=fixture(tmp_path);baseline=run(path);end=baseline['now_us']
    config=json.loads(path.read_text());config['execution']['horizon_us']=end;config['output']['directory']='inclusive'
    path.write_text(json.dumps(config));assert run(path)['outcome']=='Completed'
    config['output']['directory']='stopped';config['execution']['max_settlements']=0
    path.write_text(json.dumps(config));stopped=run(path)
    assert stopped['outcome']=='Incomplete' and stopped['reason']=='operator_stop' and stopped['exited_count']==0


def test_missing_topology_generates_from_prepared_reference_with_capacity_overrides(tmp_path):
    from fab_sim.runner import run
    result=run(fixture(tmp_path,inputs={'manufacturing_path':'manufacturing.json','physical_reference_path':'reference.json'},
        generation={'entry_capacity':3,'exit_capacity':4,'rack_capacities':{'A':2},'improvement_limit':1}))
    assert result['outcome']=='Completed' and result['capacity']['entry']==3 and result['capacity']['exit']==4
    assert list(result['capacity']['racks'].values())==[2]


def test_probe_setting_does_not_change_seeded_domain_state(tmp_path):
    from fab_sim.runner import run
    path=fixture(tmp_path);observed=run(path)
    config=json.loads(path.read_text());config['probes']={'enabled':False};config['output']['directory']='no-probes'
    path.write_text(json.dumps(config));plain=run(path)
    assert observed['domain_digest']==plain['domain_digest']
    assert observed['probes']['counts'] and not plain['probes']['counts']


def test_native_maintenance_and_equipment_and_amhs_faults_are_wired(tmp_path):
    from fab_sim.runner import run
    from test_maintenance import duration
    native={'id':'pm','scope':'toolgroup','scope_id':'A','type':'calendar','interval':1000000000,
            'first':duration(0),'duration':duration(1000000)}
    faults=[{'id':'eq','target_type':'equipment','target_id':'A:tool:001','onset_us':0,'duration_us':100},
            {'id':'oht','target_type':'oht','target_id':'OHT:0001','onset_us':0,'duration_us':100}]
    # Use the actual generated fleet identifier rather than a source naming guess.
    path=fixture(tmp_path,model_updates={'maintenance':[native]})
    config=json.loads(path.read_text());top=json.loads((tmp_path/'topology.json').read_text())
    faults[1]['target_id']=top['model']['vehicles'][0]['id'];config['injected_faults']=faults
    path.write_text(json.dumps(config));result=run(path)
    assert result['outcome']=='Completed'
    rows=[json.loads(x) for x in (tmp_path/'result/events.jsonl').read_text().splitlines()]
    assert any(r['event_type']=='maintenance_started' for r in rows)
    assert any(r['event_type']=='maintenance_recovered' for r in rows)


def test_runtime_budget_failure_exports_partial_state_evidence(tmp_path):
    from fab_sim.runner import run
    result=run(fixture(tmp_path,execution={'horizon_us':1000000000,'transition_budget':1}))
    assert result['outcome']=='Failed' and result['failure']['category']=='TransitionBudgetExceeded'
    assert result['failure']['state_may_be_partial'] is True
    assert result['failure']['recent'] and (tmp_path/'result/diagnostics.jsonl').stat().st_size>0


@pytest.mark.parametrize('capacity',[0,True,-1])
def test_generated_racks_reject_invalid_or_source_incompatible_capacity(tmp_path,capacity):
    from fab_sim.runner import run
    with pytest.raises(ValueError):run(fixture(tmp_path,
        inputs={'manufacturing_path':'manufacturing.json','physical_reference_path':'reference.json'},
        generation={'rack_capacities':{'A':capacity}}))
    assert not (tmp_path/'result').exists()


def test_pacing_and_required_publication_preserve_seeded_domain_state(tmp_path):
    from fab_sim.runner import run
    from test_publisher import Clock,Adapter
    path=fixture(tmp_path);baseline=run(path)
    config=json.loads(path.read_text());config['output'].update(directory='live',live=True,
        publication={'batch_records':4,'flush_interval_s':.1})
    config['replay']={'rate':1000};path.write_text(json.dumps(config))
    clock=Clock();adapter=Adapter(clock=clock,cost=.02)
    result=run(path,publication_adapter=adapter,clock=clock,sleep=clock.sleep)
    assert result['outcome']=='Completed' and result['domain_digest']==baseline['domain_digest']
    assert result['publication']['queue_records']==0 and result['publication']['accepted_count']==result['record_counts']['events']+result['record_counts']['oht']
    assert result['publication']['blocked_s']>0


def test_required_publication_failure_cannot_complete_a_domain_run(tmp_path):
    from fab_sim.runner import run
    from test_publisher import Clock,Adapter
    path=fixture(tmp_path);config=json.loads(path.read_text());config['output'].update(live=True,
        publication={'batch_records':1,'max_attempts':1});path.write_text(json.dumps(config))
    clock=Clock();adapter=Adapter([{'status':'permanent','accepted_ids':[]}],clock=clock)
    result=run(path,publication_adapter=adapter,clock=clock,sleep=clock.sleep)
    assert result['outcome']=='Failed' and result['output_status']=='failed'
    assert result['last_committed_sequence']==1 and result['publication']['pending_ids']==['fixture:1']


def test_midrun_publication_failure_preserves_already_observed_cqt_violation(tmp_path):
    from fab_sim.runner import run
    from test_publisher import Clock
    steps=[step(1,cqt={'target_step_id':'2','threshold_us':0}),step(2)]
    path=fixture(tmp_path,model_updates={'routes':[{'id':'r','product_id':'p','steps':steps,'dedication_components':[]}]})
    config=json.loads(path.read_text());config['output'].update(live=True,publication={'batch_records':1});path.write_text(json.dumps(config))
    class Adapter:
        def publish(self,records,*,timeout_s):
            if any(r['event_type']=='cqt_violation' for r in records):return {'status':'permanent','accepted_ids':[]}
            return {'status':'ok','accepted_ids':[r['event_id'] for r in records]}
    clock=Clock();result=run(path,publication_adapter=Adapter(),clock=clock,sleep=clock.sleep)
    assert result['outcome']=='Failed' and result['constraint_status']=='violated' and result['violation_count']>0


def test_explicit_extension_metadata_is_preserved_without_becoming_runtime_options(tmp_path):
    from fab_sim.runner import run
    result=run(fixture(tmp_path,replay={'extensions':{'note':'pace metadata'}},
        probes={'extensions':{'note':'probe metadata'}},output={'directory':'result','extensions':{'note':'output metadata'}}))
    assert result['outcome']=='Completed'
    assert result['resolved_config']['probes']['extensions']['note']=='probe metadata'


def test_failed_final_manifest_write_returns_failed_in_memory_outcome(tmp_path,monkeypatch):
    from fab_sim.runner import run
    import fab_sim.output as output_module
    original=output_module.os.replace;calls=0
    def replace(source,target):
        nonlocal calls
        calls+=1
        if calls>1:raise OSError('simulated manifest storage failure')
        return original(source,target)
    path=fixture(tmp_path);monkeypatch.setattr(output_module.os,'replace',replace)
    result=run(path)
    assert result['outcome']=='Failed' and result['output_status']=='failed'
    assert result['last_committed_sequence']>0 and 'storage failure' in result['failure']


def test_events_include_applicable_entity_references(tmp_path):
    from fab_sim.runner import run
    run(fixture(tmp_path))
    rows=[json.loads(x) for x in (tmp_path/'result/events.jsonl').read_text().splitlines()]
    admitted=next(r for r in rows if r['event_type']=='lot_admitted')
    assert admitted['entity_refs']['lot_ids']==[admitted['payload']['lot_id']]
    assert admitted['entity_refs']['carrier_ids']==[admitted['payload']['carrier_id']]
    transfer=next(r for r in rows if r['event_type']=='transfer_assigned')
    assert transfer['entity_refs']['transfer_ids']==[transfer['payload']['id']]
    assert transfer['entity_refs']['oht_ids']==[transfer['payload']['vehicle_id']]


def test_movement_references_preserve_separate_transfer_and_traffic_identities():
    from fab_sim.runner import event_references
    refs=event_references('transfer_movement_requested',{'id':'d:pickup','demand_id':'d','vehicle_id':'v'})
    assert refs=={'movement_ids':['d:pickup'],'transfer_ids':['d'],'oht_ids':['v']}
    assert event_references('traffic_arrived',{'request_id':'d:pickup','vehicle_id':'v'})=={
        'movement_ids':['d:pickup'],'oht_ids':['v']}
    assert event_references('execution_committed',{'id':'e','visit_ids':['l:1']})=={
        'execution_ids':['e'],'visit_ids':['l:1']}




def test_v3_references_name_tools_carriers_lots_and_steps():
    from fab_sim.runner import event_references
    assert event_references('service_started',{'id':'x','carriers':['P:1:carrier'],
        'owner_id':'manufacturing:T_1:tool:004:execution:P:1:visit:2:setup'})=={
        'carrier_ids':['P:1:carrier'],'equipment_ids':['T_1:tool:004'],'lot_ids':['P:1']}
    assert event_references('handoff_committed',{'demand_id':'d','vehicle_id':'v','carrier_id':'P:1:carrier',
        'source_rack_id':'T_1:tool:004:rack:0','output_port_id':'T_1:tool:004:port:1',
        'destination_id':'U_2:tool:010:port:0'})=={'transfer_ids':['d'],'oht_ids':['v'],
        'carrier_ids':['P:1:carrier'],'lot_ids':['P:1'],'equipment_ids':['T_1:tool:004','U_2:tool:010']}
    assert event_references('visit_skipped',{'lot_id':'P:1','source_step_id':'12'})=={
        'lot_ids':['P:1'],'step_ids':['12']}


def test_v3_transport_abstraction_is_diagnostic_only():
    from fab_sim.streams import StreamProjection
    projection=StreamProjection.__new__(StreamProjection)
    assert projection.project('transport_abstraction_replaced',{'lot_id':'P:1','source_step_id':'3'})==[
        ('diagnostics','transport_abstraction_replaced',{'lot_id':'P:1','source_step_id':'3'})]
