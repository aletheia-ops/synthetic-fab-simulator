from copy import deepcopy
import pytest
from fab_sim.history_archive import HistoryArchive

def runtime(tmp_path,events=None):
    from test_runner import fixture
    from fab_sim.runner import build_runtime,load_config
    config=load_config(fixture(tmp_path))
    owner=build_runtime(config,lambda k,p:events.append((owner.scheduler.now_us,k,deepcopy(p))) if events is not None else None,initialize=True)
    return owner,config

def terminal(owner):
    for _ in range(1000):
        owner.scheduler.settle(owner.scheduler.next_time_us)
        for handler in owner.manufacturing.handlers.values():
            for record in handler.requests.values():
                if record['status']=='complete':return handler,record
    raise AssertionError('no completed service request')

def test_service_archive_preserves_complete_native_callback_and_timer_body(tmp_path):
    from fab_sim.history_archive import retire_service_requests,durable_record,service_ledger_digest
    owner,_=runtime(tmp_path);handler,record=terminal(owner);identity=record['id']
    expected=durable_record(record);before=service_ledger_digest(handler)
    archive=HistoryArchive(tmp_path/'history')
    assert retire_service_requests(handler,archive)>0
    assert identity not in handler.requests and service_ledger_digest(handler)==before
    records={item['id']:item['record'] for item in archive.records(handler.history_archive)}
    assert records[identity]==expected
    assert records[identity]['timer']['fields']['_on_complete']['action']['kind']=='service:complete'
    assert records[identity]['on_complete']['action']['kind'].startswith('manufacturing:')
    # Request IDs derive from checkpointed sequences; retired IDs are not looked up.

def test_archive_service_failure_does_not_remove_request(tmp_path,monkeypatch):
    from fab_sim.history_archive import retire_service_requests
    owner,_=runtime(tmp_path);handler,record=terminal(owner);identity=record['id']
    archive=HistoryArchive(tmp_path/'history')
    monkeypatch.setattr(archive,'append',lambda *a: (_ for _ in ()).throw(OSError('disk failure')))
    with pytest.raises(OSError):retire_service_requests(handler,archive)
    assert handler.requests[identity] is record and handler.history_archive is None

def test_service_archive_same_build_resume_matches_future_evidence(tmp_path):
    from fab_sim.runner import build_runtime
    from fab_sim.runtime_state import capture_runtime,restore_runtime,runtime_digest
    from test_checkpoint_runtime_state import advance
    from fab_sim.history_archive import retire_service_requests
    events=[];after=[];owner,config=runtime(tmp_path,events);handler,record=terminal(owner)
    before=runtime_digest(owner)
    assert retire_service_requests(handler,HistoryArchive(tmp_path/'history'))>0
    assert runtime_digest(owner)==before
    state=capture_runtime(owner);events.clear()
    clone=build_runtime(config,lambda k,p:after.append((clone.scheduler.now_us,k,deepcopy(p))),initialize=False)
    restore_runtime(clone,state,clone.actions)
    other=clone.manufacturing.handlers[next(tid for tid,h in owner.manufacturing.handlers.items() if h is handler)]
    assert record['id'] not in other.requests and other.history_archive==handler.history_archive
    advance(owner);advance(clone)
    assert events==after and runtime_digest(owner)==runtime_digest(clone)
    assert owner.streams.export_state()==clone.streams.export_state()

def test_service_request_and_lookup_storage_plateaus_over_10000_completions(tmp_path):
    from fab_sim.actions import ActionRef
    from fab_sim.history_archive import retire_service_requests
    owner,_=runtime(tmp_path)
    # Drain real work, then exercise the same native lane and callback registry.
    while owner.scheduler.next_time_us is not None:owner.scheduler.settle(owner.scheduler.next_time_us)
    handler=next(iter(owner.manufacturing.handlers.values()));archive=HistoryArchive(tmp_path/'history')
    duration=owner.actions.bind(ActionRef('constant','',1));complete=owner.actions.bind(ActionRef('noop','',None))
    for i in range(10000):
        handler.request('test:'+str(i),'load',(),duration,complete)
        handler.dispatch();owner.scheduler.settle(owner.scheduler.next_time_us)
        if i%64==63:
            while retire_service_requests(handler,archive):pass
            assert not handler.requests and not handler._pending
    while retire_service_requests(handler,archive):pass
    assert not handler.requests and not handler._pending
    assert handler.history_archive['records']>=10000
    assert handler._history_lookup is None  # no identity index is built at runtime
