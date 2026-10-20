import hashlib
from fab_sim.checkpoint_format import encode_value

def exercise(tmp_path,compact,count=128):
    from test_runner import fixture
    from fab_sim.runner import build_runtime,load_config
    from fab_sim.progress import Progress
    from fab_sim.runtime_state import runtime_digest,capture_runtime
    from fab_sim.history_archive import (HistoryArchive,retire_closed_clocks,retire_recovered_causes,
        retire_clearance_records,retire_campaign_records,retire_service_requests,retire_idle_requests,
        retire_transfer_groups,retire_manufacturing_records,retire_exited_lots,logical_rng_states)
    tmp_path.mkdir();release={'id':'a','product_id':'p','route_id':'r','priority':1,'super_hot':False,
        'wafer_count':25,'first_at_us':0,'interval_seconds':'10000','lots_per_release':1,'due_allowance_us':10**12}
    config=load_config(fixture(tmp_path,model_updates={'releases':[release]},workload={'count':count}))
    events=hashlib.sha256();event_count=0
    def emit(kind,payload):
        nonlocal event_count
        events.update(encode_value((owner.scheduler.now_us,kind,payload)));event_count+=1
        observer.observe(kind,payload,owner.scheduler.now_us)
    owner=build_runtime(config,emit,initialize=True)
    observer=Progress(owner.manufacturing.lots,owner.manufacturing.rules.visits,['p'])
    archive=HistoryArchive(tmp_path/'history');sizes=[];seen=set()
    def retire():
        for lane,fn in ((owner.manufacturing.rules,retire_closed_clocks),(owner.maintenance,retire_recovered_causes),
                (owner.clearance,retire_clearance_records),(owner.campaigns,retire_campaign_records)):
            while fn(lane,archive):pass
        for h in (*owner.manufacturing.handlers.values(),*owner.transfers.exchange.handlers.values()):
            while retire_service_requests(h,archive):pass
        while retire_idle_requests(owner.traffic,archive):pass
        while retire_transfer_groups(owner.transfers,archive):pass
        while retire_manufacturing_records(owner.manufacturing,archive):pass
        while retire_exited_lots(owner.flow,archive):pass
    settlements=0
    while owner.scheduler.next_time_us is not None:
        owner.scheduler.settle(owner.scheduler.next_time_us);settlements+=1
        assert settlements<100000
        if compact and settlements%32==0:retire()
        exited=observer.exited_count
        if compact and exited in (16,count) and exited not in seen:
            seen.add(exited);retire();sizes.append(len(encode_value(capture_runtime(owner))))
    if compact:retire()
    assert observer.exited_count==count
    if compact:
        assert not owner.manufacturing.lots and not owner.manufacturing.rules.visits
        assert not owner.manufacturing.executions and not owner.transfers.demands and not owner.transfers.movements
        assert not owner.transfers.exchange.requests and not owner.handoffs.records
        assert not owner.streams._streams and not owner.transfers.overflow_visits
        assert len(sizes)==2 and abs(sizes[1]-sizes[0])<8192
        assert owner.flow.retirement_metrics['oversize_attempts']==0
    return events.hexdigest(),event_count,runtime_digest(owner),logical_rng_states(owner.flow),observer.export_state(),settlements

def test_repeated_complete_lifecycles_match_unpruned_reference_and_checkpoint_plateaus(tmp_path):
    reference=exercise(tmp_path/'reference',False)
    compact=exercise(tmp_path/'compact',True)
    assert reference==compact
