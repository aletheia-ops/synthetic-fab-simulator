from copy import deepcopy
import pytest
from fab_sim.history_archive import HistoryArchive

def prepared(tmp_path,mode='ordinary',events=None):
    from test_runner import fixture
    from test_route_rules import step
    from fab_sim.runner import load_config,build_runtime
    definition=step(1)
    if mode=='batch':definition.update(processing_unit='batch',batch_min=50,batch_max=50)
    if mode in ('lot','wafer'):definition.update(processing_unit=mode,cascade_interval_us=5)
    group={'id':'A','area':'a','batching':mode=='batch','cascading':mode in ('lot','wafer'),
        'initial_setup':None,'loading_us':2,'unloading_us':3}
    from fab_sim.generation import DEFAULT_GEOMETRY
    geometry={'geometry':{**DEFAULT_GEOMETRY,'buffer_positions_mm':[54000,57000,51000,52500]}} if mode=='batch' else None
    config=load_config(fixture(tmp_path,generation_settings=geometry,model_updates={'routes':[{'id':'r','product_id':'p','steps':[definition],'dedication_components':[]}],'tool_groups':[group]}))
    owner=build_runtime(config,lambda k,p:events.append((owner.scheduler.now_us,k,deepcopy(p))) if events is not None else None,initialize=True)
    while owner.scheduler.next_time_us is not None:owner.scheduler.settle(owner.scheduler.next_time_us)
    archive=HistoryArchive(tmp_path/'history')
    from fab_sim.history_archive import retire_service_requests,retire_transfer_groups
    for h in (*owner.manufacturing.handlers.values(),*owner.transfers.exchange.handlers.values()):
        while retire_service_requests(h,archive):pass
    while retire_transfer_groups(owner.transfers,archive):pass
    return owner,config,archive

@pytest.mark.parametrize('mode',['ordinary','batch','lot','wafer'])
def test_retired_manufacturing_records_preserve_complete_logical_state(tmp_path,mode):
    from fab_sim.history_archive import retire_manufacturing_records,logical_native_records,durable_record
    from fab_sim.runtime_state import runtime_digest,capture_runtime,restore_runtime
    from fab_sim.runner import build_runtime
    owner,config,archive=prepared(tmp_path,mode);m=owner.manufacturing
    assert m.executions and all(e['status']=='unloaded' for e in m.executions.values())
    expected={identity:durable_record(e) for identity,e in m.executions.items()};before=runtime_digest(owner)
    assert retire_manufacturing_records(m,archive)>0
    assert not m.executions and runtime_digest(owner)==before
    assert logical_native_records(m.executions,m.executions_archive,'manufacturing-execution')==expected
    assert not m.batches.assemblies
    for pipe in m.pipelines.values():
        assert not pipe.units and not pipe.executions
    state=capture_runtime(owner);clone=build_runtime(config,lambda *a:None,initialize=False)
    restore_runtime(clone,state,clone.actions)
    assert runtime_digest(owner)==runtime_digest(clone)

def test_manufacturing_retirement_keeps_active_execution_and_failed_publication(tmp_path,monkeypatch):
    from fab_sim.history_archive import retire_manufacturing_records
    from fab_sim.runtime_state import runtime_digest
    owner,config,archive=prepared(tmp_path);m=owner.manufacturing;e=next(iter(m.executions.values()))
    e['status']='processing';retire_manufacturing_records(m,archive);assert e['id'] in m.executions
    e['status']='unloaded';before=runtime_digest(owner)
    monkeypatch.setattr(archive,'append',lambda *a:(_ for _ in ()).throw(OSError('disk failure')))
    with pytest.raises(OSError):retire_manufacturing_records(m,archive)
    assert m.executions[e['id']] is e and runtime_digest(owner)==before
