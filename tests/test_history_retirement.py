from copy import deepcopy
import pytest
from fab_sim.history_archive import HistoryArchive, retire_closed_clocks
from fab_sim.probes import state_projection, domain_digest
from test_manufacturing_facade import factory

def clock(i,closed=True,violated=False):
    return {'id':str(i),'lot_id':'low','declaring_visit_id':'v'+str(i),
        'target_step_id':'1','deadline_us':100,'closed_at_us':50 if closed else None,
        'violated':violated,'event_id':i}

def test_retirement_preserves_logical_canonical_history(tmp_path):
    m,*_=factory();rules=m.rules
    rules.clocks={str(i):clock(i) for i in range(130)}
    rules.clocks['open']=clock(200,False);rules.clocks['violation']=clock(201,True,True)
    original=deepcopy(rules.clocks)
    expected=state_projection(rules);digest=domain_digest(expected)
    archive=HistoryArchive(tmp_path)
    while retire_closed_clocks(rules,archive):pass
    assert set(rules.clocks)=={'open'}
    assert rules.clock_archive['records']==131 and rules.retired_violation_ids==[[201,'violation']]
    from fab_sim.history_archive import logical_clocks
    assert logical_clocks(rules)==original
    assert state_projection(rules)==expected
    assert domain_digest(state_projection(rules))==digest

def test_archive_failure_does_not_delete_any_clock(tmp_path,monkeypatch):
    m,*_=factory();m.rules.clocks={'1':clock(1)};expected=deepcopy(m.rules.clocks)
    archive=HistoryArchive(tmp_path)
    monkeypatch.setattr(archive,'append',lambda *a: (_ for _ in ()).throw(OSError('disk failure')))
    with pytest.raises(OSError):retire_closed_clocks(m.rules,archive)
    assert m.rules.clocks==expected and m.rules.clock_archive is None


def test_compact_native_checkpoint_restores_without_hydrating_history(tmp_path):
    from test_runner import fixture
    from fab_sim.runner import build_runtime,load_config
    from fab_sim.runtime_state import capture_runtime,restore_runtime,runtime_digest
    from fab_sim.checkpoint_format import encode_value,CheckpointError
    config=load_config(fixture(tmp_path));owner=build_runtime(config,lambda *a:None,initialize=True)
    owner.scheduler.settle(owner.scheduler.next_time_us)
    rules=owner.manufacturing.rules
    rules.clocks={str(i):clock(i) for i in range(1000)}
    original=runtime_digest(owner);full_bytes=len(encode_value(capture_runtime(owner)))
    archive=HistoryArchive(tmp_path/'archive')
    while retire_closed_clocks(rules,archive):pass
    state=capture_runtime(owner)
    assert len(encode_value(state))<full_bytes//2
    clone=build_runtime(config,lambda *a:None,initialize=False)
    restore_runtime(clone,state,clone.actions)
    assert not clone.manufacturing.rules.clocks
    assert runtime_digest(clone)==original
    # Portable: the checkpoint restores without its history; the audit needs it.
    archive._segment_path(rules.clock_archive['head']).unlink()
    other=build_runtime(config,lambda *a:None,initialize=False)
    restore_runtime(other,state,other.actions)
    assert runtime_digest(other)==original
    audit=build_runtime(config,lambda *a:None,initialize=False)
    with pytest.raises(CheckpointError):restore_runtime(audit,state,audit.actions,verify_history=True)


@pytest.mark.parametrize('field,value',[('id','other'),('lot_id','other'),('declaring_visit_id','other'),
    ('target_step_id','2'),('deadline_us',101),('closed_at_us',51),('violated',True),('event_id',999)])
def test_every_clock_field_contributes_to_logical_digest(field,value):
    m,*_=factory();m.rules.clocks={'1':clock(1)}
    before=domain_digest(state_projection(m.rules))
    m.rules.clocks['1'][field]=value
    assert domain_digest(state_projection(m.rules))!=before

def test_real_qtime_rework_continuation_after_archive_restore(tmp_path):
    from test_runner import fixture
    from test_route_rules import step
    from fab_sim.runner import build_runtime,load_config
    from fab_sim.runtime_state import capture_runtime,restore_runtime,runtime_digest
    from test_checkpoint_runtime_state import advance
    from fab_sim.history_archive import logical_clocks
    path=fixture(tmp_path,model_updates={'routes':[{'id':'r','product_id':'p','steps':[
        step(1,cqt={'target_step_id':'2','threshold_us':1000000000000}),
        step(2,rework={'probability':'0.25','target_step_id':'1'})],'dedication_components':[]}]})
    config=load_config(path);before=[];after=[]
    owner=build_runtime(config,lambda k,p:before.append((owner.scheduler.now_us,k,deepcopy(p))),initialize=True)
    for _ in range(1000):
        owner.scheduler.settle(owner.scheduler.next_time_us)
        if any(c['closed_at_us'] is not None for c in owner.manufacturing.rules.clocks.values()):break
    else:raise AssertionError('real Q-time never closed')
    rules=owner.manufacturing.rules;original=deepcopy(rules.clocks);digest=runtime_digest(owner)
    archive=HistoryArchive(tmp_path/'history')
    assert retire_closed_clocks(rules,archive)>0
    assert logical_clocks(rules)==original and runtime_digest(owner)==digest
    state=capture_runtime(owner);before.clear()
    clone=build_runtime(config,lambda k,p:after.append((clone.scheduler.now_us,k,deepcopy(p))),initialize=False)
    restore_runtime(clone,state,clone.actions)
    advance(owner);advance(clone)
    assert before==after and runtime_digest(owner)==runtime_digest(clone)
    assert owner.streams.export_state()==clone.streams.export_state()


def test_closed_violated_clocks_retire_and_assessment_keeps_their_order(tmp_path):
    from fab_sim.runner import assessed_obligations
    from fab_sim.history_archive import logical_clocks
    m,*_=factory();rules=m.rules
    # Creation (event_id) order: an old violated clock still open, newer closed violated ones.
    rules.clocks={'old-late-open':clock(1,False,True),'a':clock(2),'late-closed':clock(3,True,True),
        'open':clock(4,False),'late-closed-2':clock(5,True,True)}
    original=deepcopy(rules.clocks);before=assessed_obligations(rules.clocks)
    expected=state_projection(rules)
    archive=HistoryArchive(tmp_path)
    while retire_closed_clocks(rules,archive):pass
    assert set(rules.clocks)=={'old-late-open','open'}
    assert rules.retired_violation_ids==[[3,'late-closed'],[5,'late-closed-2']]
    assert logical_clocks(rules)==original
    assert state_projection(rules)==expected
    assert assessed_obligations(rules.clocks,rules.retired_violation_ids)==before==(
        ['old-late-open','late-closed','late-closed-2'],['open'])


def test_v10_bundle_gains_an_empty_retired_violation_index(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from test_runner import fixture
    from fab_sim import runtime_state
    from fab_sim.runner import build_runtime,load_config
    from fab_sim.runtime_state import capture_runtime,restore_runtime,runtime_digest
    from fab_sim.checkpoint import upgrade_state
    config=load_config(fixture(tmp_path));owner=build_runtime(config,lambda *a:None,initialize=True)
    owner.scheduler.settle(owner.scheduler.next_time_us)
    owner.manufacturing.rules.clocks={'late':clock(1,True,True)}
    digest=runtime_digest(owner)
    # Capture as a v10 writer did: no retired_violation_ids field.
    with monkeypatch.context() as mp:
        fields,ignored=runtime_state.SCHEMAS['rules']
        mp.setitem(runtime_state.SCHEMAS,'rules',(fields.replace(' retired_violation_ids',''),ignored))
        del owner.manufacturing.rules.retired_violation_ids
        state=capture_runtime(owner)
    assert 'retired_violation_ids' not in state['owners']['rules']
    bundle=SimpleNamespace(manifest=SimpleNamespace(state_contract='fab-native-checkpoint-archive-v10'),state={'runtime':state})
    upgrade_state(bundle)
    clone=build_runtime(config,lambda *a:None,initialize=False)
    restore_runtime(clone,state,clone.actions)
    assert clone.manufacturing.rules.retired_violation_ids==[] and runtime_digest(clone)==digest
    archive=HistoryArchive(tmp_path/'archive')
    while retire_closed_clocks(clone.manufacturing.rules,archive):pass
    assert clone.manufacturing.rules.retired_violation_ids==[[1,'late']] and runtime_digest(clone)==digest


@pytest.mark.parametrize('deferred', [False, True])
def test_archive_segments_are_dropped_from_the_page_cache_once_durable(tmp_path, monkeypatch, deferred):
    import os
    calls = []
    real = getattr(os, 'posix_fadvise', None)
    def fadvise(fd, offset, length, advice):
        calls.append(advice)
        if real:real(fd, offset, length, advice)
    monkeypatch.setattr(os, 'posix_fadvise', fadvise, raising=False)
    monkeypatch.setattr(os, 'POSIX_FADV_DONTNEED', getattr(os, 'POSIX_FADV_DONTNEED', 4), raising=False)
    archive = HistoryArchive(tmp_path, deferred_sync=deferred)
    head = archive.append(None, [{'kind': 'clock', 'id': '1', 'record': clock(1)}])
    if deferred:
        assert not calls
        archive.sync()
    assert calls == [os.POSIX_FADV_DONTNEED]
    assert archive._read(archive._segment_path(head['head']))
