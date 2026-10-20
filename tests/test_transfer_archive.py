from copy import deepcopy
import pytest
from fab_sim.history_archive import HistoryArchive

def prepared(tmp_path,events=None):
    from test_service_archive import runtime
    from fab_sim.history_archive import retire_service_requests
    owner,config=runtime(tmp_path,events);archive=HistoryArchive(tmp_path/'history')
    for _ in range(10000):
        owner.scheduler.settle(owner.scheduler.next_time_us)
        for h in (*owner.manufacturing.handlers.values(),*owner.transfers.exchange.handlers.values()):
            while retire_service_requests(h,archive):pass
        if any(r['status']=='delivered' and r['id'] not in owner.transfers.active.values()
               for r in owner.transfers.demands.values()):return owner,config,archive
    raise AssertionError('no terminal transfer')

def test_transfer_group_archival_preserves_all_bodies_and_physical_digest(tmp_path):
    from fab_sim.history_archive import retire_transfer_groups,durable_record
    from fab_sim.runtime_state import runtime_digest
    owner,config,archive=prepared(tmp_path);t=owner.transfers
    before=runtime_digest(owner);expected={r['id']:durable_record(r) for r in t.demands.values()}
    assert retire_transfer_groups(t,archive)>0
    assert runtime_digest(owner)==before
    removed=set(expected)-set(t.demands);assert removed
    archived={item['id']:item['record'] for item in archive.records(t.history_archive)}
    for did in removed:
        assert archived[did]==expected[did]
        assert all(mid not in t.movements and mid not in owner.traffic.requests for mid in (did+':pickup',did+':drop'))
        assert did not in owner.handoffs.records
        assert all(eid not in t.exchange.requests for eid in (did+':input',did+':output'))
        with pytest.raises(ValueError):t.cancel(did)

def test_partial_transfer_archive_publication_failure_keeps_all_canonical_records(tmp_path,monkeypatch):
    from fab_sim.history_archive import retire_transfer_groups
    from fab_sim.runtime_state import runtime_digest
    owner,config,archive=prepared(tmp_path);before=runtime_digest(owner)
    original=archive.append;calls=[]
    def fail(head,records):
        calls.append(records)
        if len(calls)==2:raise OSError('second ledger publication failed')
        return original(head,records)
    monkeypatch.setattr(archive,'append',fail)
    with pytest.raises(OSError):retire_transfer_groups(owner.transfers,archive)
    assert len(calls)==2 and runtime_digest(owner)==before and owner.transfers.history_archive is None

def test_transfer_group_retains_late_cancel_movement_and_live_exchange(tmp_path):
    from fab_sim.history_archive import retire_transfer_groups
    owner,config,archive=prepared(tmp_path);t=owner.transfers
    did=next(r['id'] for r in t.demands.values() if r['status']=='delivered' and r['id'] not in t.active.values())
    movement=t.movements.get(did+':drop') or t.movements.get(did+':pickup')
    assert movement is not None
    previous=movement['status'];movement['status']='moving'
    retire_transfer_groups(t,archive);assert did in t.demands
    movement['status']=previous
    exchange=t.exchange.requests.get(did+':input') or t.exchange.requests.get(did+':output')
    assert exchange is not None
    previous=exchange['status'];exchange['status']='active'
    retire_transfer_groups(t,archive);assert did in t.demands
    exchange['status']=previous
    assert retire_transfer_groups(t,archive)>0 and did not in t.demands

def test_transfer_archive_same_build_resume_matches_future_evidence(tmp_path):
    from fab_sim.history_archive import retire_transfer_groups
    from fab_sim.runner import build_runtime
    from fab_sim.runtime_state import capture_runtime,restore_runtime,runtime_digest
    from test_checkpoint_runtime_state import advance
    events=[];after=[];owner,config,archive=prepared(tmp_path,events)
    assert retire_transfer_groups(owner.transfers,archive)>0
    state=capture_runtime(owner);events.clear()
    clone=build_runtime(config,lambda k,p:after.append((clone.scheduler.now_us,k,deepcopy(p))),initialize=False)
    restore_runtime(clone,state,clone.actions)
    assert clone.transfers.history_archive==owner.transfers.history_archive
    advance(owner);advance(clone)
    assert events==after and runtime_digest(owner)==runtime_digest(clone)
    assert owner.streams.export_state()==clone.streams.export_state()


def test_transfer_history_and_checkpoint_storage_plateau_over_10000_cancellations(tmp_path):
    from fab_sim.history_archive import retire_transfer_groups
    from fab_sim.runtime_state import capture_runtime
    from fab_sim.checkpoint_format import encode_value
    owner,config,archive=prepared(tmp_path);t=owner.transfers
    template=next(r for r in t.demands.values() if r['status']=='delivered' and r['id'] not in t.active.values())
    carrier,lid,visit,destinations=(template[k] for k in ('carrier_id','lot_id','visit_id','destinations'))
    while retire_transfer_groups(t,archive):pass
    sizes=[];first=None
    # Exercise genuine internal request/cancellation transitions. No assignment
    # or physical movement is attempted for these pending test demands.
    for i in range(10000):
        did=t._request(carrier,lid,visit,destinations,owner.scheduler.now_us)
        if first is None:first=did
        t.cancel(did);owner.scheduler.settle(owner.scheduler.now_us)
        if i%64==63:
            while retire_transfer_groups(t,archive):pass
            assert len(t.demands)<8 and len(t.movements)<8 and len(t.exchange.requests)<8
        if i in (1023,9983):sizes.append(len(encode_value(capture_runtime(owner))))
    while retire_transfer_groups(t,archive):pass
    assert sizes[1]-sizes[0]<4096
    assert t.history_archive['records']>=10000
    with pytest.raises(ValueError):t.cancel(first)
    assert t._archive_lookup().stats()['page_cache_kib']==2048
    assert t._archive_lookup().stats()['namespaces']<=5

def test_new_warm_handoff_attempt_does_not_query_archived_ids(tmp_path,monkeypatch):
    from fab_sim.history_archive import retire_transfer_groups
    owner,config,archive=prepared(tmp_path);t=owner.transfers
    while owner.scheduler.next_time_us is not None:owner.scheduler.settle(owner.scheduler.next_time_us)
    # The real route produces at least one completed rack-output handoff.
    from fab_sim.history_archive import retire_service_requests
    for h in (*owner.manufacturing.handlers.values(),*t.exchange.handlers.values()):
        while retire_service_requests(h,archive):pass
    while retire_transfer_groups(t,archive):pass
    assert owner.handoffs.history_archive is not None
    did='test:warm';t.demands[did]={'id':did,'status':'cancelled'}
    monkeypatch.setattr(owner.handoffs._archive_lookup(),'contains',lambda *a:(_ for _ in ()).throw(AssertionError('archive query for warm demand')))
    assert owner.handoffs.prepare(did,'unused') is False


def test_genuine_rack_reuse_is_terminal_and_archivable(tmp_path):
    from test_transfers import factory,place
    from fab_sim.history_archive import retire_transfer_groups
    t,m,s,c,lots,vehicles,events=factory()
    v=m.enter_visit('low','1');place(c,'clow','rack')
    did=t.request('clow',v,('port',));s.settle(0)
    assert t.demands[did]['status']=='reused'
    archive=HistoryArchive(tmp_path/'history')
    assert retire_transfer_groups(t,archive)==1
    assert did not in t.demands
    assert next(archive.records(t.history_archive))['record']['status']=='reused'

def test_compound_retirement_after_resume_continues_chains_in_the_new_attempt_directory(tmp_path):
    from fab_sim.history_archive import retire_transfer_groups,retire_idle_requests,retire_service_requests
    from fab_sim.runner import build_runtime
    from fab_sim.runtime_state import capture_runtime,restore_runtime,runtime_digest
    owner,config,archive=prepared(tmp_path)
    while owner.scheduler.next_time_us is not None:owner.scheduler.settle(owner.scheduler.next_time_us)
    for h in (*owner.manufacturing.handlers.values(),*owner.transfers.exchange.handlers.values()):
        while retire_service_requests(h,archive):pass
    while retire_idle_requests(owner.traffic,archive):pass
    assert owner.traffic.history_archive and owner.transfers.history_archive is None
    clone=build_runtime(config,lambda *a:None,initialize=False)
    restore_runtime(clone,capture_runtime(owner),clone.actions)
    before=runtime_digest(clone)
    child=HistoryArchive(tmp_path/'child-history')
    assert retire_transfer_groups(clone.transfers,child)>0
    # Every chain continues in the resumed attempt's directory; parents.json links the parent.
    assert clone.traffic.history_archive['directory']==str(child.directory)
    linked=HistoryArchive(clone.traffic.history_archive['directory'])
    assert sum(1 for _ in linked.records(clone.traffic.history_archive))==clone.traffic.history_archive['records']
    assert runtime_digest(clone)==before
