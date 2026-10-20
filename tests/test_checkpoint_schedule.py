from pathlib import Path
import pytest
from fab_sim.checkpoint import CheckpointSettings, CheckpointCoordinator
from fab_sim.checkpoint_store import CheckpointStore, CheckpointWriteError
from test_checkpoint_store import bundle


def settings(tmp_path,interval=300):
    return CheckpointSettings(True,interval,tmp_path,None,False)


def test_default_is_five_wall_minutes(tmp_path):
    wall=[10];coordinator=CheckpointCoordinator(settings(tmp_path),CheckpointStore(tmp_path),clock=lambda:wall[0])
    wall[0]=309;assert coordinator.maybe_save(bundle,boundary=(10,3)) is None
    wall[0]=310;manifest=coordinator.maybe_save(bundle,boundary=(10,3))
    assert manifest.is_file()
    assert coordinator.next_deadline==610


def test_slow_save_resets_deadline_from_completion(tmp_path):
    wall=[0];coordinator=CheckpointCoordinator(settings(tmp_path),CheckpointStore(tmp_path),clock=lambda:wall[0])
    def capture():
        wall[0]=1900
        return bundle()
    wall[0]=900
    assert coordinator.maybe_save(capture,boundary=(10,3)) is not None
    assert coordinator.next_deadline==2200
    wall[0]=2000
    assert coordinator.maybe_save(bundle,boundary=(20,4)) is None


def test_final_save_deduplicates_boundary(tmp_path):
    wall=[0];coordinator=CheckpointCoordinator(settings(tmp_path,5),CheckpointStore(tmp_path),clock=lambda:wall[0])
    wall[0]=5;coordinator.maybe_save(bundle,boundary=(10,3))
    coordinator.maybe_save(bundle,boundary=(10,3),final=True)
    assert len(list(tmp_path.glob('day-*')))==1


def test_disabled_coordinator_has_no_capture_side_effect(tmp_path):
    coordinator=CheckpointCoordinator(CheckpointSettings(False,300,tmp_path,None,False),CheckpointStore(tmp_path),clock=lambda:0)
    assert coordinator.maybe_save(lambda:pytest.fail('unexpected capture'),boundary=(0,0),final=True) is None
    assert not list(tmp_path.iterdir())


def test_write_failure_is_terminal(tmp_path,monkeypatch):
    store=CheckpointStore(tmp_path);coordinator=CheckpointCoordinator(settings(tmp_path),store,clock=lambda:1000)
    monkeypatch.setattr(store,'_write_file',lambda *args:(_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(CheckpointWriteError):coordinator.maybe_save(bundle,boundary=(10,3),final=True)
    assert coordinator.last_boundary is None
