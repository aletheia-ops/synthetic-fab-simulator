from copy import deepcopy
import pytest
from test_runner import fixture
from fab_sim.runner import build_runtime, load_config
from fab_sim.runtime_state import capture_runtime, restore_runtime, runtime_digest
from fab_sim.actions import ActionRegistry
from fab_sim.checkpoint_format import encode_value, decode_value, CheckpointError


def advance(owners, limit=1000):
    for _ in range(limit):
        tick = owners.scheduler.next_time_us
        if tick is None: return
        owners.scheduler.settle(tick)
    raise AssertionError('fixture did not drain')


@pytest.mark.parametrize('split', [1, 4, 10, 20])
def test_busy_native_state_continuation_matches(tmp_path, split):
    config = load_config(fixture(tmp_path))
    before = []; owners = build_runtime(config, lambda k,p: before.append((owners.scheduler.now_us,k,deepcopy(p))), initialize=True)
    for i in range(split):
        tick = owners.scheduler.next_time_us
        if tick is None: break
        owners.scheduler.settle(tick)
    state = decode_value(encode_value(capture_runtime(owners)))
    saved_digest = runtime_digest(owners); before.clear()
    after = []; clone = build_runtime(config, lambda k,p: after.append((clone.scheduler.now_us,k,deepcopy(p))), initialize=False)
    restore_runtime(clone, state, clone.actions)
    assert runtime_digest(clone) == saved_digest
    assert clone.streams.export_state() == owners.streams.export_state()
    assert clone.transfers.manufacturing is clone.manufacturing
    assert clone.manufacturing.lots is clone.flow.lots
    advance(owners); advance(clone)
    assert after == before
    assert runtime_digest(clone) == runtime_digest(owners)
    assert clone.streams.export_state() == owners.streams.export_state()


def test_restore_allocation_does_not_seed_sources_or_faults(tmp_path):
    config = load_config(fixture(tmp_path))
    clone = build_runtime(config, lambda *args: None, initialize=False)
    assert clone.scheduler._heap == []
    assert clone.streams.export_state()['states'] == {}
    assert clone.source.sequence == 0


def test_unrecognized_runtime_field_rejected(tmp_path):
    config = load_config(fixture(tmp_path)); owners = build_runtime(config, lambda *args: None, initialize=True)
    owners.scheduler.settle(owners.scheduler.next_time_us)
    owners.manufacturing.unrecognized_physical_owner = object()
    with pytest.raises(CheckpointError): capture_runtime(owners)


@pytest.mark.parametrize('paused', [False,True])
@pytest.mark.parametrize('mode', ['batch', 'lot-cascade', 'wafer-cascade', 'setup'])
def test_processing_variants_restore_busy_shared_state(tmp_path, mode, paused):
    from test_route_rules import step
    from fab_sim.generation import DEFAULT_GEOMETRY
    definition = step(1, processing={'distribution':'uniform','mean_seconds':'0.0001','offset_seconds':'0.00001'})
    group = {'id':'A','area':'a','batching':mode=='batch','cascading':'cascade' in mode,
        'initial_setup':None,'loading_us':2,'unloading_us':3}
    transitions = []
    if mode == 'batch': definition.update(processing_unit='batch',batch_min=50,batch_max=50)
    elif 'cascade' in mode: definition.update(processing_unit=mode.split('-')[0],cascade_interval_us=10)
    else:
        definition['setup']={'id':'recipe','when':'need','fallback':None}
        transitions=[{'current':None,'new':'recipe','duration_us':7,'minimum_runs':0}]
    path=fixture(tmp_path,model_updates={'tool_groups':[group],
        'routes':[{'id':'r','product_id':'p','steps':[definition],'dedication_components':[]}],
        'setup_transitions':transitions}, generation_settings={'rack_capacities':{'A':2},
            'geometry':{**DEFAULT_GEOMETRY,'buffer_positions_mm':[54000,57000,51000,52500]}})
    config=load_config(path); before=[]
    owners=build_runtime(config,lambda k,p:before.append((owners.scheduler.now_us,k,deepcopy(p))),initialize=True)
    for _ in range(500):
        tick=owners.scheduler.next_time_us
        assert tick is not None
        owners.scheduler.settle(tick)
        if mode=='setup': busy=any(h.active and h.active['kind']=='setup' for h in owners.manufacturing.handlers.values())
        else: busy=any(e['status']=='processing' for e in owners.manufacturing.executions.values())
        if busy: break
    assert busy
    tool=next(iter(owners.manufacturing.tools))
    if paused:
        owners.manufacturing.pause(tool,'checkpoint-test-fault')
        owners.scheduler.settle(owners.scheduler.now_us)
    state=decode_value(encode_value(capture_runtime(owners)));before.clear();after=[]
    clone=build_runtime(config,lambda k,p:after.append((clone.scheduler.now_us,k,deepcopy(p))),initialize=False)
    restore_runtime(clone,state,clone.actions)
    if paused:
        for runtime in (owners,clone):
            assert 'checkpoint-test-fault' in runtime.manufacturing.causes[tool]
            runtime.manufacturing.resume(tool,'checkpoint-test-fault')
            runtime.scheduler.settle(runtime.scheduler.now_us)
    advance(owners);advance(clone)
    assert after==before
    assert runtime_digest(clone)==runtime_digest(owners)


def test_overlapping_native_faults_resume_without_duplicate_seeding(tmp_path):
    config=load_config(fixture(tmp_path)); seed=build_runtime(config,lambda *args:None,initialize=False)
    tool=next(iter(seed.manufacturing.tools));vehicle=next(iter(seed.transfers.vehicles))
    config['injected_faults']=[
        {'id':'eq1','target_type':'equipment','target_id':tool,'onset_us':0,'duration_us':30},
        {'id':'eq2','target_type':'equipment','target_id':tool,'onset_us':10,'duration_us':40},
        {'id':'oht','target_type':'oht','target_id':vehicle,'onset_us':0,'duration_us':20}]
    before=[];owners=build_runtime(config,lambda k,p:before.append((owners.scheduler.now_us,k,deepcopy(p))),initialize=True)
    owners.scheduler.settle(0);owners.scheduler.settle(10)
    state=decode_value(encode_value(capture_runtime(owners)));before.clear();after=[]
    clone=build_runtime(config,lambda k,p:after.append((clone.scheduler.now_us,k,deepcopy(p))),initialize=False)
    restore_runtime(clone,state,clone.actions)
    advance(owners);advance(clone)
    assert after==before
    assert clone.amhs_faults.records['oht']['status']=='recovered'
    assert runtime_digest(clone)==runtime_digest(owners)
