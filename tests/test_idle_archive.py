from copy import deepcopy
import pytest
from fab_sim.history_archive import HistoryArchive

def idle_runtime():
    from test_traffic import factory
    from fab_sim.actions import ActionRef,ActionRegistry
    traffic,scheduler,vehicles,events=factory()
    # Declarative native callbacks, as used by the runner; no opaque closures.
    actions=ActionRegistry();actions.register('noop',lambda *args:None);traffic.actions=actions;actions.register('traffic:arrive',lambda identity,payload:traffic._arrive(traffic.requests[identity]))
    return traffic,scheduler,vehicles,events,actions.bind(ActionRef('noop','',None))

def finish(traffic,scheduler,identity,noop):
    from test_traffic import drain
    target=next(h['id'] for h in traffic.holdings.values() if h['rail_id']=='a' and h['offset_um']==1100)
    traffic.request('v1',target,identity,reposition=True,on_start=noop,on_complete=noop,on_cancel=noop)
    drain(scheduler)
    return traffic.requests[identity]

def test_idle_archive_preserves_full_body_digest_duplicate_and_cancel_semantics(tmp_path):
    from fab_sim.history_archive import retire_idle_requests,traffic_ledger_digest,durable_record
    traffic,scheduler,vehicles,events,noop=idle_runtime()
    record=finish(traffic,scheduler,'idle:1',noop);expected=durable_record(record)
    before=traffic_ledger_digest(traffic);archive=HistoryArchive(tmp_path/'history')
    assert retire_idle_requests(traffic,archive)==1
    assert not traffic.requests and traffic_ledger_digest(traffic)==before
    assert next(archive.records(traffic.history_archive))['record']==expected
    with pytest.raises(ValueError,match='finished'):traffic.cancel('idle:1')
    with pytest.raises(ValueError,match='finished'):traffic.cancel('unknown')

def test_idle_archive_keeps_active_and_transfer_bound_requests(tmp_path):
    from fab_sim.history_archive import retire_idle_requests
    traffic,scheduler,vehicles,events,noop=idle_runtime()
    record=finish(traffic,scheduler,'idle:1',noop)
    class Transfers:movements={'idle:1':{}}
    traffic.transfers=Transfers()
    assert retire_idle_requests(traffic,HistoryArchive(tmp_path/'history'))==0
    traffic.transfers=None;record['reposition']=False
    assert retire_idle_requests(traffic,HistoryArchive(tmp_path/'history'))==0
    record['reposition']=True;traffic.active['v1']='idle:1'
    assert retire_idle_requests(traffic,HistoryArchive(tmp_path/'history'))==0

def test_idle_archive_failure_keeps_canonical_request(tmp_path,monkeypatch):
    from fab_sim.history_archive import retire_idle_requests
    traffic,scheduler,vehicles,events,noop=idle_runtime();record=finish(traffic,scheduler,'idle:1',noop)
    archive=HistoryArchive(tmp_path/'history')
    monkeypatch.setattr(archive,'append',lambda *a:(_ for _ in ()).throw(OSError('disk failure')))
    with pytest.raises(OSError):retire_idle_requests(traffic,archive)
    assert traffic.requests['idle:1'] is record and traffic.history_archive is None

def test_idle_history_storage_stays_bounded_after_10000_completions(tmp_path):
    from fab_sim.history_archive import retire_idle_requests
    traffic,scheduler,vehicles,events,noop=idle_runtime();archive=HistoryArchive(tmp_path/'history')
    for i in range(10000):
        finish(traffic,scheduler,'idle:'+str(i),noop);events.clear()
        if i%64==63:
            while retire_idle_requests(traffic,archive):pass
            assert not traffic.requests and not traffic.active and not traffic.hops and not traffic._idle_requests
    while retire_idle_requests(traffic,archive):pass
    assert not traffic.requests and traffic.history_archive['records']==10000
    assert traffic._history_lookup is None  # no identity index is built at runtime


def test_idle_retirement_does_not_scan_retained_transfer_history(tmp_path):
    from fab_sim.history_archive import retire_idle_requests
    traffic,scheduler,vehicles,events,noop=idle_runtime();record=finish(traffic,scheduler,'idle:1',noop)
    class NoHistoryScan(dict):
        def items(self):raise AssertionError('full traffic history scan')
        def values(self):raise AssertionError('full traffic history scan')
        def __iter__(self):raise AssertionError('full traffic history scan')
    traffic.requests=NoHistoryScan(traffic.requests)
    assert retire_idle_requests(traffic,HistoryArchive(tmp_path/'history'))==1
    assert not traffic.requests

def test_native_idle_archive_resume_does_not_hydrate_history(tmp_path):
    from test_service_archive import runtime
    from fab_sim.runner import build_runtime
    from fab_sim.runtime_state import capture_runtime,restore_runtime,runtime_digest
    from fab_sim.history_archive import retire_idle_requests
    events=[];after=[];owner,config=runtime(tmp_path,events)
    while owner.scheduler.next_time_us is not None:owner.scheduler.settle(owner.scheduler.next_time_us)
    traffic=owner.traffic
    # A genuine native zero-distance autonomous request at a prepared safe pose.
    vid=next(vid for vid,v in traffic.vehicles.items() if not v.get('assignment_id'))
    target=next(h for h in traffic.holdings.values() if all(h[k]==traffic.vehicles[vid][k] for k in ('rail_id','offset_um')))
    traffic.request(vid,target['id'],'idle:extra',reposition=True)
    owner.scheduler.settle(owner.scheduler.now_us)
    assert traffic.requests['idle:extra']['status']=='complete'
    before=runtime_digest(owner)
    assert retire_idle_requests(traffic,HistoryArchive(tmp_path/'history'))>0
    assert runtime_digest(owner)==before
    state=capture_runtime(owner);clone=build_runtime(config,lambda k,p:after.append((clone.scheduler.now_us,k,deepcopy(p))),initialize=False)
    restore_runtime(clone,state,clone.actions)
    assert 'idle:extra' not in clone.traffic.requests and clone.traffic.history_archive==traffic.history_archive
    assert runtime_digest(owner)==runtime_digest(clone)
    with pytest.raises(ValueError,match='finished'):clone.traffic.cancel('idle:extra')
    events.clear();after.clear()
    for o in (owner,clone):
        o.traffic.request(vid,target['id'],'idle:next',reposition=True);o.scheduler.settle(o.scheduler.now_us)
    assert events==after and runtime_digest(owner)==runtime_digest(clone)
