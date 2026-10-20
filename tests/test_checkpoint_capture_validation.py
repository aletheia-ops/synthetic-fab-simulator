"""Checkpoint capture and restore reject inconsistent runtime state."""
from copy import deepcopy
import pytest
from test_runner import fixture
from fab_sim.runner import build_runtime,load_config
from fab_sim.runtime_state import capture_runtime,restore_runtime
from fab_sim.checkpoint_format import CheckpointError


def busy_owner(tmp_path, *, service=False):
    config=load_config(fixture(tmp_path))
    owner=build_runtime(config,lambda *args:None,initialize=True)
    for _ in range(500):
        owner.scheduler.settle(owner.scheduler.next_time_us)
        if service:
            found=any(h.active is not None for h in owner.manufacturing.handlers.values())
        else:
            found=bool(owner.traffic.active)
        if found:return config,owner
    raise AssertionError('fixture never became busy')


@pytest.mark.parametrize('mutation', ['owner','payload'])
def test_invalid_native_action_rejected_before_continuation(tmp_path,mutation):
    config,owner=busy_owner(tmp_path)
    state=capture_runtime(owner)
    def mutate(value):
        if type(value) is list:
            if len(value)==2 and value[0]=='action' and value[1]['kind']=='traffic:arrive':
                value[1]['owner_id' if mutation=='owner' else 'payload']='missing-native-owner'
                return True
            return any(mutate(v) for v in value)
        if type(value) is dict:return any(mutate(v) for v in value.values())
        return False
    assert mutate(state['nodes'])
    clone=build_runtime(config,lambda *args:None,initialize=False)
    with pytest.raises(CheckpointError):restore_runtime(clone,state,clone.actions)


@pytest.mark.parametrize('extension', ['deadline','pass','event'])
def test_custom_scheduler_callbacks_rejected_at_capture(tmp_path,extension):
    _,owner=busy_owner(tmp_path)
    if extension=='deadline':owner.scheduler.add_deadline_check(lambda:None)
    elif extension=='pass':owner.scheduler._passes['traffic']=lambda:None
    else:owner.scheduler.register('custom:event',lambda payload:None)
    with pytest.raises(CheckpointError):capture_runtime(owner)


def test_duplicate_active_service_alias_rejected(tmp_path):
    config,owner=busy_owner(tmp_path,service=True)
    state=capture_runtime(owner)
    handler=next(node for node in state['nodes'] if node[0]=='SerializedHandler' and node[1]['active']!=['value',None])
    request_index=handler[1]['active'][1]
    state['nodes'].append(deepcopy(state['nodes'][request_index]))
    handler[1]['active']=['ref',len(state['nodes'])-1]
    clone=build_runtime(config,lambda *args:None,initialize=False)
    with pytest.raises(CheckpointError):restore_runtime(clone,state,clone.actions)


def test_invalid_action_bundle_rejects_before_child_output(tmp_path):
    import json
    from test_checkpoint_config import saved_run
    from fab_sim.runner import run
    from fab_sim.checkpoint_store import CheckpointStore
    path,manifest,_=saved_run(tmp_path)
    store=CheckpointStore(tmp_path/'malformed')
    bundle=store.read(manifest)
    def mutate(value):
        if type(value) is list:
            if len(value)==2 and value[0]=='action' and value[1]['kind']=='traffic:arrive':
                value[1]['owner_id']='missing-native-owner'
                return True
            return any(mutate(v) for v in value)
        if type(value) is dict:return any(mutate(v) for v in value.values())
        return False
    assert mutate(bundle.state['runtime']['nodes'])
    bad=store.write(bundle)
    config=json.loads(path.read_text());config['output']['directory']='child'
    config['checkpoint']={'resume_from':str(bad)};path.write_text(json.dumps(config))
    with pytest.raises(CheckpointError):run(path)
    assert not (tmp_path/'child').exists()


def test_active_timer_must_own_its_scheduled_deadline(tmp_path):
    config,owner=busy_owner(tmp_path)
    state=capture_runtime(owner)
    timer=next(node for node in state['nodes'] if node[0]=='Timer'
        and node[1]['completed']==['value',False] and node[1]['_event'][0]=='value'
        and node[1]['_event'][1] is not None)
    timer[1]['_event']=['value',999999999]
    clone=build_runtime(config,lambda *args:None,initialize=False)
    with pytest.raises(CheckpointError):restore_runtime(clone,state,clone.actions)
