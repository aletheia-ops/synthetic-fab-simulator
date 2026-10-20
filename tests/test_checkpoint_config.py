from dataclasses import replace
import json
from pathlib import Path
import pytest
from test_runner import fixture
from fab_sim.runner import load_config, run
from fab_sim.checkpoint_format import CheckpointError


@pytest.mark.parametrize('interval',[True,False,0,-1,1.5,'300'])
def test_invalid_interval_rejects_before_output(tmp_path,interval):
    path=fixture(tmp_path,checkpoint={'enabled':True,'interval_wall_seconds':interval})
    with pytest.raises(ValueError,match='interval'):run(path)
    assert not (tmp_path/'result').exists()


def test_defaults_paths_and_cli_precedence(tmp_path):
    path=fixture(tmp_path);config=load_config(path)
    assert config['checkpoint']['enabled'] is False
    path=fixture(tmp_path,checkpoint={'enabled':True})
    config=load_config(path)
    assert config['checkpoint']['interval_wall_seconds']==300
    assert config['checkpoint']['directory']==str(tmp_path/'result/checkpoints')
    data=json.loads(path.read_text());data['checkpoint']['directory']='saved';path.write_text(json.dumps(data))
    config=load_config(path,checkpoint_interval_wall_seconds=7)
    assert config['checkpoint']['enabled'] is True
    assert config['checkpoint']['interval_wall_seconds']==7
    assert config['checkpoint']['directory']==str(tmp_path/'saved')


def test_live_publication_with_checkpoint_rejected(tmp_path):
    path=fixture(tmp_path,checkpoint={'enabled':True},output={'directory':'result','live':True})
    with pytest.raises(ValueError,match='live'):load_config(path)
    assert not (tmp_path/'result').exists()


def saved_run(tmp_path):
    path=fixture(tmp_path,checkpoint={'enabled':True},execution={'horizon_us':1000000000,'max_settlements':10})
    result=run(path)
    assert result['outcome']!='Failed',result.get('failure')
    latest=json.loads((tmp_path/'result/checkpoints/latest.json').read_text())['manifest']
    return path,tmp_path/'result/checkpoints'/latest,result


def test_resume_without_saving_preserves_cumulative_limits(tmp_path):
    path,manifest,parent=saved_run(tmp_path)
    config=json.loads(path.read_text());config['checkpoint']={'resume_from':str(manifest)}
    config['output']['directory']='child';path.write_text(json.dumps(config))
    child=run(path)
    assert child['outcome']!='Failed',child.get('failure')
    assert child['settlements']==parent['settlements']==10
    assert child['now_us']==parent['now_us']
    assert child['record_counts']['events']==0
    assert child['base_sequence']==parent['last_committed_sequence']
    assert not (tmp_path/'child/checkpoints').exists()


@pytest.mark.parametrize('change',['inputs','rng','contract','horizon','existing'])
def test_incompatible_resume_rejects_before_continuation_output(tmp_path,change):
    path,manifest,parent=saved_run(tmp_path)
    config=json.loads(path.read_text());config['checkpoint']={'resume_from':str(manifest),'allow_compatible_build':True}
    config['output']['directory']='child'
    if change=='inputs':config['execution']['master_seed']=999
    elif change=='horizon':config['execution']['horizon_us']=parent['now_us']-1
    elif change=='existing':(tmp_path/'child').mkdir()
    else:
        metadata=json.loads(manifest.read_text())
        if change=='build':metadata['writer_build']='unvalidated-build'
        elif change=='rng':metadata['runtime_compatibility']['rng']='other'
        else:metadata['state_contract']='other'
        manifest.write_text(json.dumps(metadata))
    path.write_text(json.dumps(config))
    with pytest.raises((CheckpointError,ValueError,FileExistsError)):run(path)
    assert not (tmp_path/'child/events.jsonl').exists()


def _child(tmp_path,path,manifest,*,writer_build=None,execution=None,**checkpoint):
    if writer_build is not None:
        metadata=json.loads(manifest.read_text());metadata['writer_build']=writer_build
        manifest.write_text(json.dumps(metadata))
    config=json.loads(path.read_text());config['output']['directory']='child'
    config['execution'].update(execution or {})
    config['checkpoint']={'resume_from':str(manifest),**checkpoint};path.write_text(json.dumps(config))
    return path


def test_other_build_requires_explicit_compatible_build_flag(tmp_path):
    path,manifest,parent=saved_run(tmp_path)
    _child(tmp_path,path,manifest,writer_build='other-build')
    with pytest.raises(CheckpointError,match='build'):run(path)
    assert not (tmp_path/'child/events.jsonl').exists()


def test_compatible_build_flag_resumes_and_records_both_builds(tmp_path):
    from fab_sim.checkpoint import build_identity
    path,manifest,parent=saved_run(tmp_path)
    _child(tmp_path,path,manifest,writer_build='other-build',allow_compatible_build=True)
    child=run(path)
    assert child['outcome']!='Failed',child.get('failure')
    assert child['settlements']==parent['settlements'] and child['now_us']==parent['now_us']
    continuation=json.loads((tmp_path/'child/manifest.json').read_text())['continuation']
    assert continuation['cross_build']=={'writer_build':'other-build','resume_build':build_identity()}
    assert 'policy_changes' not in continuation


def test_declared_policy_setting_needs_policy_change_flag(tmp_path):
    path,manifest,parent=saved_run(tmp_path)
    _child(tmp_path,path,manifest,execution={'hop_lookahead_mm':10000},allow_compatible_build=True)
    with pytest.raises(CheckpointError,match='policy'):run(path)
    assert not (tmp_path/'child/events.jsonl').exists()


def test_policy_change_flag_resumes_and_records_the_change(tmp_path):
    path,manifest,parent=saved_run(tmp_path)
    _child(tmp_path,path,manifest,execution={'hop_lookahead_mm':10000},allow_policy_change=True)
    child=run(path)
    assert child['outcome']!='Failed',child.get('failure')
    continuation=json.loads((tmp_path/'child/manifest.json').read_text())['continuation']
    assert continuation['policy_changes']=={'hop_lookahead_mm':{'checkpoint':None,'resume':10000}}
    assert 'cross_build' not in continuation


def test_policy_change_flag_does_not_admit_other_settings(tmp_path):
    path,manifest,parent=saved_run(tmp_path)
    _child(tmp_path,path,manifest,execution={'hop_lookahead_mm':10000,'master_seed':999},allow_policy_change=True)
    with pytest.raises(CheckpointError,match='policy'):run(path)
    assert not (tmp_path/'child/events.jsonl').exists()


def test_decimal_physical_settings_survive_manifest_round_trip(tmp_path):
    path=fixture(tmp_path,checkpoint={'enabled':True},workload={'count':2,'release_time_scale':0.5})
    parent=run(path);assert parent['outcome']!='Failed',parent.get('failure')
    latest=json.loads((tmp_path/'result/checkpoints/latest.json').read_text())['manifest']
    config=json.loads(path.read_text());config['output']['directory']='child'
    config['checkpoint']['resume_from']=str(tmp_path/'result/checkpoints'/latest)
    path.write_text(json.dumps(config));child=run(path)
    assert child['outcome']==parent['outcome']
    assert child['domain_digest']==parent['domain_digest']


def test_input_content_change_at_same_path_is_rejected(tmp_path):
    path,manifest,parent=saved_run(tmp_path)
    config=json.loads(path.read_text());config['output']['directory']='child'
    config['checkpoint']={'resume_from':str(manifest)};path.write_text(json.dumps(config))
    with (tmp_path/'manufacturing.json').open('a') as stream:stream.write('\n')
    with pytest.raises(CheckpointError,match='input'):run(path)
    assert not (tmp_path/'child').exists()


def test_invalid_control_state_rejected_before_output_creation(tmp_path):
    from fab_sim.checkpoint_store import CheckpointStore
    from fab_sim.checkpoint_format import CheckpointBundle
    path,manifest,parent=saved_run(tmp_path);store=CheckpointStore(tmp_path/'bad')
    saved=store.read(manifest);saved.state['control']['exit_targets_reached_at_us']='invalid'
    saved.state['control']['exit_targets']={'p':1}
    bad=store.write(saved)
    config=json.loads(path.read_text());config['output']['directory']='child'
    config['execution']['exit_targets']={'p':1};config['checkpoint']={'resume_from':str(bad)}
    path.write_text(json.dumps(config))
    with pytest.raises((CheckpointError,ValueError)):run(path)
    assert not (tmp_path/'child').exists()


def test_older_state_contracts_are_rejected():
    from types import SimpleNamespace
    from fab_sim.checkpoint import validate_resume,runtime_compatibility,build_identity
    identity={'policy':'p','settings':{'execution':{'master_seed':7},'generation':{},'workload':{},'injected_faults':[]},'input_hashes':{}}
    manifest=SimpleNamespace(state_contract='fab-native-checkpoint-archive-v11',runtime_compatibility=runtime_compatibility(),
        physical_identity=identity,writer_build=build_identity())
    bundle=SimpleNamespace(manifest=manifest,state={})
    assert validate_resume(bundle,identity,allow_compatible_build=False)=={}
    manifest.state_contract='fab-native-checkpoint-archive-v9'
    with pytest.raises(CheckpointError,match='contract'):validate_resume(bundle,identity,allow_compatible_build=False)
