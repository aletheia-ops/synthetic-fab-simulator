from copy import deepcopy
import pytest
from fab_sim.history_archive import HistoryArchive

def prepared(tmp_path,mode='ordinary'):
    from test_manufacturing_archive import prepared as manufacturing
    from fab_sim.history_archive import retire_manufacturing_records,retire_closed_clocks,retire_clearance_records,retire_campaign_records
    owner,config,archive=manufacturing(tmp_path,mode)
    while retire_closed_clocks(owner.manufacturing.rules,archive):pass
    while retire_clearance_records(owner.clearance,archive):pass
    while retire_campaign_records(owner.campaigns,archive):pass
    while retire_manufacturing_records(owner.manufacturing,archive):pass
    return owner,config,archive

@pytest.mark.parametrize('mode',['ordinary','batch','lot','wafer'])
def test_exited_lot_archive_preserves_full_lot_visit_rng_and_progress(tmp_path,mode):
    from fab_sim.history_archive import retire_exited_lots,logical_lot_histories,logical_rng_states
    from fab_sim.runtime_state import runtime_digest,capture_runtime,restore_runtime
    from fab_sim.runner import build_runtime
    from fab_sim.progress import Progress
    owner,config,archive=prepared(tmp_path,mode);flow=owner.flow;m=owner.manufacturing
    assert m.lots and all(lot['exited_at_us'] is not None for lot in m.lots.values())
    expected_lots=deepcopy(m.lots);expected_visits=deepcopy(m.rules.visits);expected_rng=owner.streams.export_state()['states']
    observer=Progress(m.lots,m.rules.visits,['p']);before=runtime_digest(owner)
    assert sum(retire_exited_lots(flow,archive) for _ in range(len(m.lots)))==len(expected_lots)
    assert not m.lots and not m.rules.visits and runtime_digest(owner)==before
    assert logical_lot_histories(flow)==(expected_lots,expected_visits)
    assert logical_rng_states(flow)==expected_rng
    compact=Progress(m.lots,m.rules.visits,['p'],retired=flow.retired_facts)
    assert compact.export_state()==observer.export_state()
    state=capture_runtime(owner);clone=build_runtime(config,lambda *a:None,initialize=False)
    restore_runtime(clone,state,clone.actions)
    assert not clone.manufacturing.lots and runtime_digest(clone)==before
    Progress(clone.manufacturing.lots,clone.manufacturing.rules.visits,['p'],retired=clone.flow.retired_facts).restore_state(observer.export_state())
    # Retired LOT RNG states remain available through the explicit archive reader.

def test_lot_retirement_blocks_unresolved_dependencies_and_keeps_failed_publication(tmp_path,monkeypatch):
    from fab_sim.history_archive import retire_exited_lots
    from fab_sim.runtime_state import runtime_digest
    owner,config,archive=prepared(tmp_path);flow=owner.flow;lot=next(iter(flow.lots.values()));lid=lot['lot_id']
    owner.manufacturing.rules.clocks['open']={'lot_id':lid}
    while retire_exited_lots(flow,archive):pass
    assert lid in flow.lots
    del owner.manufacturing.rules.clocks['open'];before=runtime_digest(owner)
    monkeypatch.setattr(archive,'append',lambda *a:(_ for _ in ()).throw(OSError('disk failure')))
    with pytest.raises(OSError):retire_exited_lots(flow,archive)
    assert flow.lots[lid] is lot and runtime_digest(owner)==before

def test_corrupt_retired_progress_facts_reject_restore(tmp_path):
    from fab_sim.history_archive import retire_exited_lots
    from fab_sim.runtime_state import capture_runtime,restore_runtime
    from fab_sim.runner import build_runtime
    from fab_sim.checkpoint_format import CheckpointError
    owner,config,archive=prepared(tmp_path)
    while retire_exited_lots(owner.flow,archive):pass
    owner.flow.retired_facts['total']+=1
    state=capture_runtime(owner);clone=build_runtime(config,lambda *a:None,initialize=False)
    # A normal restore trusts the checkpoint; the history audit detects the mismatch.
    with pytest.raises(CheckpointError,match='retired'):restore_runtime(clone,state,clone.actions,verify_history=True)

def test_oversized_lot_history_stays_resident_instead_of_breaking_run(tmp_path):
    from fab_sim.history_archive import retire_exited_lots
    owner,config,archive=prepared(tmp_path)
    for lot in owner.flow.lots.values():lot['test_large_body']='x'*archive.max_bytes
    assert retire_exited_lots(owner.flow,archive)==0
    assert owner.flow.lots and owner.flow.retirement_metrics['oversize_attempts']>0


def test_overflow_history_is_preserved_in_lot_bundle_and_removed_from_warm_state(tmp_path):
    from fab_sim.history_archive import retire_exited_lots
    from fab_sim.runtime_state import runtime_digest,capture_runtime,restore_runtime
    from fab_sim.runner import build_runtime
    owner,config,archive=prepared(tmp_path)
    lot=next(iter(owner.flow.lots.values()))
    pair=(lot['carrier_id'],lot['route_history'][0])
    owner.transfers.overflow_visits.add(pair)
    before=runtime_digest(owner)
    while retire_exited_lots(owner.flow,archive):pass
    assert not owner.transfers.overflow_visits
    assert runtime_digest(owner)==before
    leaves=list(archive.records(owner.flow.history_archive))
    assert pair in next(x['record']['overflow_visits'] for x in leaves if x['id']==lot['lot_id'])
    clone=build_runtime(config,lambda *a:None,initialize=False)
    restore_runtime(clone,capture_runtime(owner),clone.actions)
    assert runtime_digest(clone)==before and not clone.transfers.overflow_visits
