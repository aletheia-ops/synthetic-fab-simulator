"""Forked background checkpoints match synchronous ones and leave the run unchanged."""
import json
import pytest
from test_runner import fixture
from test_checkpoint_equivalence import rows, normalized, latest
from fab_sim.runner import run
from fab_sim.checkpoint_store import CheckpointStore


def ticking():
    wall=[0]
    def clock():wall[0]+=1;return wall[0]
    return clock


def saved(directory):
    out={}
    for manifest in directory.glob('day-*/manifest.json'):
        out[json.loads(manifest.read_text())['saved_tick']]=manifest.parent
    return out


def checkpointed(tmp_path,name,background):
    path=fixture(tmp_path);config=json.loads(path.read_text())
    config['output']={'directory':name,'run_id':name}
    config['checkpoint']={'enabled':True,'interval_wall_seconds':10,'retain_completed':1000,'background':background}
    path.write_text(json.dumps(config))
    return run(path,observation_clock=ticking())


def test_background_checkpoints_are_byte_identical_and_leave_events_unchanged(tmp_path):
    baseline=run(fixture(tmp_path))
    # Same directory for both modes: archive segments and state embed their paths.
    sync=checkpointed(tmp_path,'run',False);(tmp_path/'run').rename(tmp_path/'sync')
    background=checkpointed(tmp_path,'run',True)
    for result in (sync,background):
        assert result['outcome']!='Failed',result.get('failure')
        assert result['domain_digest']==baseline['domain_digest']
    assert normalized(rows(tmp_path/'run/events.jsonl'))==normalized(rows(tmp_path/'result/events.jsonl'))
    assert normalized(rows(tmp_path/'run/oht.jsonl'))==normalized(rows(tmp_path/'result/oht.jsonl'))
    forked=[m for m in background['checkpoint_metrics'] if m.get('background')]
    assert forked, background['checkpoint_metrics']
    a,b=saved(tmp_path/'sync/checkpoints'),saved(tmp_path/'run/checkpoints')
    # Each save retires history into archive segments, so segment hashes depend on
    # the earlier save points. Compare boundaries with the same save history.
    common=[t for t in sorted(set(a)&set(b)) if {x for x in a if x<t}=={x for x in b if x<t}]
    assert common
    for tick in common:
        assert (a[tick]/'state.json').read_bytes()==(b[tick]/'state.json').read_bytes()


def test_resume_from_a_background_checkpoint_matches_uninterrupted_evidence(tmp_path):
    path=fixture(tmp_path);config=json.loads(path.read_text())
    baseline=run(path);baseline_events=rows(tmp_path/'result/events.jsonl')
    config['output']={'directory':'part1','run_id':'part1'}
    config['checkpoint']={'enabled':True,'interval_wall_seconds':10,'background':True}
    config['execution']['max_settlements']=25
    path.write_text(json.dumps(config));first=run(path,observation_clock=ticking())
    assert first['outcome']!='Failed',first.get('failure')
    manifest=latest(tmp_path/'part1/checkpoints');bundle=CheckpointStore(tmp_path).read(manifest)
    config['output']={'directory':'part2','run_id':'part2'}
    config['checkpoint']={'enabled':False,'resume_from':str(manifest)}
    del config['execution']['max_settlements'];path.write_text(json.dumps(config));second=run(path)
    assert second['outcome']!='Failed',second.get('failure')
    head=[r for r in rows(tmp_path/'part1/events.jsonl') if r['event_sequence']<=bundle.manifest.last_committed_sequence]
    assert normalized(head+rows(tmp_path/'part2/events.jsonl'))==normalized(baseline_events)
    assert second['domain_digest']==baseline['domain_digest']


def test_background_saves_fall_back_to_foreground_without_enough_memory(tmp_path,monkeypatch):
    import os
    from fab_sim import checkpoint
    baseline=run(fixture(tmp_path))
    monkeypatch.setattr(checkpoint,'writer_memory_check',lambda:(False,10,5))
    def no_fork():raise AssertionError('forked despite insufficient memory')
    monkeypatch.setattr(os,'fork',no_fork)
    result=checkpointed(tmp_path,'tight',True)
    assert result['outcome']!='Failed',result.get('failure')
    assert result['domain_digest']==baseline['domain_digest']
    metrics=result['checkpoint_metrics'];assert metrics
    assert all(m['background'] is False for m in metrics)
    assert {'reason':'insufficient_memory','needed_bytes':10,'available_bytes':5} in [m.get('background_fallback') for m in metrics]


def test_writer_memory_check_needs_headroom_over_the_run(monkeypatch):
    from fab_sim import checkpoint
    monkeypatch.setattr(checkpoint,'resident_bytes',lambda:1000*2**20)
    monkeypatch.setattr(checkpoint,'available_memory_bytes',lambda:1700*2**20)
    assert checkpoint.writer_memory_check()==(False,1300*2**20+checkpoint.WRITER_MEMORY_MARGIN_BYTES,1700*2**20)
    monkeypatch.setattr(checkpoint,'available_memory_bytes',lambda:4*2**30)
    assert checkpoint.writer_memory_check()[0] is True
    monkeypatch.setattr(checkpoint,'available_memory_bytes',lambda:None)
    assert checkpoint.writer_memory_check()==(True,None,None)


def test_background_write_failure_fails_the_run(tmp_path,monkeypatch):
    from fab_sim import checkpoint_store
    def broken(self,bundle):raise checkpoint_store.CheckpointWriteError('disk full (test)')
    monkeypatch.setattr(checkpoint_store.CheckpointStore,'write',broken)
    result=checkpointed(tmp_path,'broken',True)
    assert result['outcome']=='Failed' and result['reason']=='checkpoint_write_failure'
    assert 'disk full (test)' in json.dumps(result.get('failure'))


def test_retain_daily_keeps_the_first_checkpoint_of_each_simulated_day(tmp_path):
    path=fixture(tmp_path);config=json.loads(path.read_text())
    config['output']={'directory':'run','run_id':'run'}
    config['checkpoint']={'enabled':True,'interval_wall_seconds':10,'retain_completed':1,'retain_daily':True}
    path.write_text(json.dumps(config))
    result=run(path,observation_clock=ticking())
    assert result['outcome']!='Failed',result.get('failure')
    kept=saved(tmp_path/'run/checkpoints')
    written=len(result['checkpoint_metrics'])
    # The fixture stays within simulated day 0: retain_completed=1 alone would
    # keep only the newest; retain_daily also keeps the day's first bundle.
    assert written>2 and len(kept)==2
    assert max(kept)==max(m['saved_tick'] for m in result['checkpoint_metrics'])


def test_synchronous_capture_runs_without_the_collector_and_restores_it(tmp_path):
    import gc
    from types import SimpleNamespace
    from fab_sim.checkpoint import CheckpointCoordinator, CheckpointSettings
    from fab_sim.checkpoint_format import CheckpointError
    from fab_sim.checkpoint_store import CheckpointWriteError
    settings=CheckpointSettings(True,10,tmp_path,None,False)
    path=tmp_path/'checkpoint-1-x';path.mkdir();(path/'state.json').write_bytes(b'{}')
    store=SimpleNamespace(write=lambda bundle:path/'manifest.json')
    seen=[]
    def capture():
        seen.append(gc.isenabled())
        return SimpleNamespace(manifest=SimpleNamespace(saved_tick=1,settlements=1))
    assert gc.isenabled()
    coordinator=CheckpointCoordinator(settings,store,clock=ticking())
    assert coordinator.maybe_save(capture,boundary=1,final=True)==path/'manifest.json'
    assert seen==[False] and gc.isenabled()
    assert coordinator.metrics[-1]['peak_rss_bytes_so_far']>0
    def failing():raise CheckpointError('boom')
    with pytest.raises(CheckpointWriteError):coordinator.maybe_save(failing,boundary=2,final=True)
    assert gc.isenabled()
