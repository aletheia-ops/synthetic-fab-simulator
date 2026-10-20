"""Physical exits, open supply, FIFO admission backlog and output precedence."""
import json
from copy import deepcopy
from test_runner import fixture


def observation_fixture(tmp_path,**settings):
    # Transport in this synthetic layout takes hundreds of seconds. Keep the
    # stream open without thousands of irrelevant due intents in exit tests.
    release={'id':'a','product_id':'p','route_id':'r','priority':1,'super_hot':False,
        'wafer_count':25,'first_at_us':0,'interval_seconds':'100','lots_per_release':1,'due_allowance_us':100000000}
    return fixture(tmp_path,model_updates={'releases':[release]},**settings)


def rows(tmp_path,name='events.jsonl'):
    return [json.loads(line) for line in (tmp_path/'result'/name).read_text().splitlines()]


def test_ongoing_physical_run_reaches_three_exits_with_source_still_open(tmp_path):
    from fab_sim.runner import run
    result=run(observation_fixture(tmp_path,workload={},execution={
        'horizon_us':3000000000,'master_seed':7,'exit_targets':{'p':3}}))
    assert result['reason']=='exit_target_reached' and result['objective_achieved']
    assert result['outcome']=='Incomplete' and not result['domain_complete']
    assert result['source_open'] is True and result['cohort_size'] is None and result['pending_release_count'] is None
    assert result['product_exit_counts']['p']==3 and result['progress']['products']['p']['admitted']>2
    events=rows(tmp_path);exits=[row for row in events if row['event_type']=='lot_exited']
    assert len(exits)==3 and result['exit_targets_reached_at_us']==exits[-1]['simulation_time_us']
    manifest=json.loads((tmp_path/'result/manifest.json').read_text())
    assert manifest==result
    for kind,name in [('events','events.jsonl'),('oht','oht.jsonl'),('diagnostics','diagnostics.jsonl')]:
        assert manifest['record_counts'][kind]==len(rows(tmp_path,name))


def test_faster_releases_stop_with_actual_wip(tmp_path):
    from fab_sim.runner import run
    release={'id':'a','product_id':'p','route_id':'r','priority':1,'super_hot':False,
        'wafer_count':25,'first_at_us':0,'interval_seconds':'50','lots_per_release':1,'due_allowance_us':100000000}
    result=run(fixture(tmp_path,model_updates={'releases':[release]},workload={},execution={
        'horizon_us':3000000000,'exit_targets':{'p':3}}))
    assert result['objective_achieved'] and result['wip_count']>0 and result['pending_admission_count']>0
    assert not result['domain_complete']


def test_entry_backlog_preserves_due_intents_and_age(tmp_path):
    from fab_sim.runner import run
    result=run(fixture(tmp_path,workload={},execution={'horizon_us':50000000}))
    events=rows(tmp_path);due=[r['payload'] for r in events if r['event_type']=='release_intent_due']
    admitted=[r['payload'] for r in events if r['event_type']=='lot_admitted']
    assert [r['scheduled_at_us'] for r in due]==list(range(0,50000001,1000000))
    assert [r['lot_id'] for r in admitted]==[r['lot_id'] for r in due[:len(admitted)]]
    assert result['pending_admission_count']==len(due)-len(admitted)
    assert result['progress']['oldest_pending_admission_age_us']==result['now_us']-due[len(admitted)]['scheduled_at_us']
    assert result['reason']=='horizon' and result['source_open']


def test_ongoing_horizon_misses_target_honestly(tmp_path):
    from fab_sim.runner import run
    result=run(fixture(tmp_path,workload={},execution={
        'horizon_us':50000000,'exit_targets':{'p':3}}))
    assert result['reason']=='horizon' and result['objective_achieved'] is False
    assert result['exit_targets_reached_at_us'] is None and not result['domain_complete']


def test_required_output_failure_overrides_target(tmp_path,monkeypatch):
    from fab_sim.runner import run
    from fab_sim.output import Output
    original=Output._manifest
    def fail(self,summary,status):
        if summary.get('objective_achieved') and status=='complete':raise OSError('final storage failure')
        return original(self,summary,status)
    monkeypatch.setattr(Output,'_manifest',fail)
    result=run(observation_fixture(tmp_path,workload={},execution={
        'horizon_us':3000000000,'exit_targets':{'p':3}}))
    assert result['outcome']=='Failed' and result['reason']=='required_output_failure'
    assert result['objective_achieved'] and not result['domain_complete']


def test_finite_target_uses_real_exits_but_drain_keeps_completed_semantics(tmp_path):
    from fab_sim.runner import run
    result=run(fixture(tmp_path,execution={'horizon_us':1000000000,'exit_targets':{'p':2}}))
    assert result['outcome']=='Completed' and result['objective_achieved']
    assert result['reason']=='cohort_exited' and result['domain_complete']


def test_target_at_horizon_and_operator_boundary_preserves_precedence(tmp_path):
    from fab_sim.runner import run
    base=tmp_path/'base';base.mkdir()
    initial=run(observation_fixture(base,workload={},execution={
        'horizon_us':3000000000,'exit_targets':{'p':3}}))
    for name,execution,reason in [
        ('horizon',{'horizon_us':initial['now_us'],'exit_targets':{'p':3}},'exit_target_reached'),
        ('operator',{'horizon_us':3000000000,'exit_targets':{'p':3},'max_settlements':initial['settlements']},'operator_stop')]:
        directory=tmp_path/name;directory.mkdir()
        result=run(observation_fixture(directory,workload={},execution=execution))
        assert result['objective_achieved'] and result['reason']==reason
        assert result['now_us']==initial['now_us']


def test_ongoing_campaign_retains_seven_member_staging_rule(tmp_path):
    from fab_sim.runner import run
    from fab_sim.generation import DEFAULT_GEOMETRY
    from test_route_rules import step
    release={'id':'a','product_id':'p','route_id':'r','priority':1,'super_hot':False,
        'wafer_count':25,'first_at_us':0,'interval_seconds':'0.000001','lots_per_release':1,'due_allowance_us':100000000}
    model={'releases':[release],
        'tool_groups':[{'id':'A','area':'a','batching':False,'cascading':False,'initial_setup':'A','loading_us':2,'unloading_us':3}],
        'routes':[{'id':'r','product_id':'p','steps':[step(1,setup={'id':'B','when':'need','fallback':None})],'dedication_components':[]}],
        'setup_transitions':[{'current':None,'new':'B','duration_us':4,'minimum_runs':7}]}
    generation={'entry_capacity':8,'rack_capacities':{'A':8},
        'geometry':{**DEFAULT_GEOMETRY,'buffer_positions_mm':list(range(28000,60000,4000))}}
    for horizon in (5,6):
        directory=tmp_path/str(horizon);directory.mkdir()
        result=run(fixture(directory,model_updates=deepcopy(model),generation_settings=generation,
            workload={},execution={'horizon_us':horizon}))
        assert result['outcome']=='Incomplete' and result['source_open']
        campaigns=[row for row in rows(directory) if row['event_type']=='setup_campaign_committed']
        assert len(campaigns)==(1 if horizon==6 else 0)
        if campaigns:assert len(campaigns[0]['payload']['members'])==7
