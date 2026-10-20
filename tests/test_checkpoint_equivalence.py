from copy import deepcopy
import json
import pytest
from test_runner import fixture
from fab_sim.runner import run
from fab_sim.checkpoint_store import CheckpointStore


def rows(path):return [json.loads(line) for line in path.read_text().splitlines()]


def normalized(records):
    return [{k:v for k,v in r.items() if k not in ('run_id','event_id')} for r in records]


def latest(directory):
    return directory/json.loads((directory/'latest.json').read_text())['manifest']


@pytest.mark.parametrize('ongoing,lookahead',[(False,None),(True,None),(True,'5000')])
def test_repeated_resume_matches_uninterrupted_evidence(tmp_path,ongoing,lookahead):
    overrides={'workload':{},'execution':{'horizon_us':200000000,'exit_targets':{'p':3}}} if ongoing else {}
    if lookahead is not None:overrides['execution']['hop_lookahead_mm']=lookahead
    path=fixture(tmp_path,**overrides);config=json.loads(path.read_text())
    baseline=run(path);baseline_events=rows(tmp_path/'result/events.jsonl')
    config['output']={'directory':'part1','run_id':'part1'}
    config['checkpoint']={'enabled':True};config['execution']['max_settlements']=12
    path.write_text(json.dumps(config));first=run(path)
    assert first['outcome']!='Failed',first.get('failure')
    first_manifest=latest(tmp_path/'part1/checkpoints');first_seq=first['last_committed_sequence']
    first_bundle=CheckpointStore(tmp_path).read(first_manifest)
    config['output']={'directory':'part2','run_id':'part2'}
    config['checkpoint']['resume_from']=str(first_manifest);config['execution']['max_settlements']=25
    path.write_text(json.dumps(config));second=run(path)
    assert second['outcome']!='Failed',second.get('failure')
    second_manifest=latest(tmp_path/'part2/checkpoints')
    config['output']={'directory':'part3','run_id':'part3'}
    config['checkpoint']={'resume_from':str(second_manifest),'enabled':True}
    del config['execution']['max_settlements'];path.write_text(json.dumps(config));third=run(path)
    assert third['outcome']!='Failed',third.get('failure')
    combined=rows(tmp_path/'part1/events.jsonl')+rows(tmp_path/'part2/events.jsonl')+rows(tmp_path/'part3/events.jsonl')
    assert normalized(combined)==normalized(baseline_events)
    diagnostics=sum((rows(tmp_path/name/'diagnostics.jsonl') for name in ('part1','part2','part3')),[])
    assert normalized(diagnostics)==normalized(rows(tmp_path/'result/diagnostics.jsonl'))
    oht=sum((rows(tmp_path/name/'oht.jsonl') for name in ('part1','part2','part3')),[])
    assert oht and normalized(oht)==normalized(rows(tmp_path/'result/oht.jsonl'))
    assert third['domain_digest']==baseline['domain_digest']
    assert third['progress']['first_exit_at_us']==baseline['progress']['first_exit_at_us']
    assert third['settlements']==baseline['settlements']
    assert first_bundle.manifest.last_committed_sequence==first_seq
    final_bundle=CheckpointStore(tmp_path).read(latest(tmp_path/'part3/checkpoints'))
    assert final_bundle.manifest.physical_digest==baseline['domain_digest']


def test_periodic_checkpointing_does_not_change_events(tmp_path):
    path=fixture(tmp_path);config=json.loads(path.read_text());baseline=run(path)
    config['output']['directory']='saved';config['checkpoint']={'enabled':True,'interval_wall_seconds':10}
    path.write_text(json.dumps(config));wall=[0]
    def clock():wall[0]+=1;return wall[0]
    candidate=run(path,observation_clock=clock)
    assert candidate['outcome']!='Failed',candidate.get('failure')
    assert candidate['domain_digest']==baseline['domain_digest']
    assert normalized(rows(tmp_path/'saved/events.jsonl'))==normalized(rows(tmp_path/'result/events.jsonl'))
    bundles=list((tmp_path/'saved/checkpoints').glob('day-*/manifest.json'))
    assert len(bundles)>2
    for manifest in bundles:
        bundle=CheckpointStore(tmp_path).read(manifest)
        assert bundle.state['runtime']['scheduler']['now_us']==bundle.manifest.saved_tick
        assert set(bundle.state)=={'runtime','progress','control','evidence'}


def test_resuming_final_checkpoint_does_not_advance(tmp_path):
    path=fixture(tmp_path,checkpoint={'enabled':True});parent=run(path)
    config=json.loads(path.read_text());config['output']['directory']='child'
    config['checkpoint']['resume_from']=str(latest(tmp_path/'result/checkpoints'))
    path.write_text(json.dumps(config));child=run(path)
    assert child['outcome']==parent['outcome']=='Completed'
    assert child['domain_digest']==parent['domain_digest']
    assert child['settlements']==parent['settlements']
    assert child['record_counts']['events']==0


def test_horizon_extension_and_changed_exit_target_continue_cumulatively(tmp_path):
    path=fixture(tmp_path,workload={},execution={'horizon_us':1000000000,'exit_targets':{'p':1}},checkpoint={'enabled':True})
    parent=run(path);assert parent['objective_achieved']
    config=json.loads(path.read_text());config['output']['directory']='child'
    config['execution'].update(horizon_us=3000000000,exit_targets={'p':3})
    config['checkpoint']['resume_from']=str(latest(tmp_path/'result/checkpoints'))
    path.write_text(json.dumps(config));child=run(path)
    assert child['outcome']!='Failed',child.get('failure')
    assert child['product_exit_counts']['p']==3
    assert child['progress']['first_exit_at_us']==parent['progress']['first_exit_at_us']
    assert child['exit_targets_reached_at_us']>parent['exit_targets_reached_at_us']
    assert child['settlements']>parent['settlements']


def test_bounded_checkpoint_verifier_reports_exact_equivalence(tmp_path):
    from tools.verify_checkpoint import verify_checkpoint
    path=fixture(tmp_path)
    result=verify_checkpoint(path,tmp_path/'verification',split_settlements=(10,20))
    assert result['equivalent'] is True
    assert result['checks']=={'events':True,'diagnostics':True,'oht':True,'physical_digest':True,'rng':True,'first_exits':True,'settlements':True}
    assert result['checkpoint_metrics']
