import json
import pytest
from test_checkpoint_store import bundle
from fab_sim.checkpoint_store import CheckpointStore, CheckpointWriteError

def test_retains_three_verified_checkpoints(tmp_path):
    store=CheckpointStore(tmp_path,retain_completed=3)
    manifests=[store.write(bundle(t)) for t in range(8)]
    assert all(not p.exists() for p in manifests[:-3])
    assert all(store.read(p).state==bundle(t).state for t,p in list(enumerate(manifests))[-3:])
    assert len(list(tmp_path.glob('day-*')))==3

def test_publication_failure_never_prunes_previous(tmp_path,monkeypatch):
    import os
    store=CheckpointStore(tmp_path,retain_completed=1)
    prior=store.write(bundle())
    original=os.replace
    def replace(src,dst):
        if str(dst).endswith('latest.json'):raise OSError('publication interrupted')
        return original(src,dst)
    monkeypatch.setattr(os,'replace',replace)
    with pytest.raises(CheckpointWriteError):store.write(bundle(20))
    assert store.read(prior).state==bundle().state

def test_unrecognized_directories_and_symlinks_are_not_pruned(tmp_path):
    store=CheckpointStore(tmp_path,retain_completed=1)
    unknown=tmp_path/'checkpoint-user-data';unknown.mkdir();(unknown/'keep').write_text('keep')
    elsewhere=tmp_path/'elsewhere';elsewhere.mkdir();(elsewhere/'keep').write_text('keep')
    (tmp_path/'checkpoint-link').symlink_to(elsewhere,target_is_directory=True)
    store.write(bundle());store.write(bundle(20))
    assert (unknown/'keep').exists() and (elsewhere/'keep').exists()

@pytest.mark.parametrize('value',[True,0,-1,1.5,'3'])
def test_invalid_retention_rejected(tmp_path,value):
    with pytest.raises(ValueError):CheckpointStore(tmp_path,retain_completed=value)


def test_retention_uses_progress_when_directory_timestamps_reverse(tmp_path,monkeypatch):
    import os
    store=CheckpointStore(tmp_path,retain_completed=3)
    original=store._write_file;writes=[0]
    def tied_timestamp(path,data):
        original(path,data)
        writes[0]+=1
        stamp=1_000_000_000-writes[0]
        os.utime(path.parent,ns=(stamp,stamp))
    monkeypatch.setattr(store,'_write_file',tied_timestamp)
    manifests=[store.write(bundle(t)) for t in range(8)]
    assert all(not p.exists() for p in manifests[:-3])
    assert all(store.read(p).state==bundle(t).state for t,p in list(enumerate(manifests))[-3:])


def test_retain_daily_thins_to_weekly_after_two_weeks(tmp_path):
    from fab_sim.checkpoint_store import DAY_US
    store=CheckpointStore(tmp_path,retain_completed=1,retain_daily=True)
    for day in range(30):store.write(bundle(day*DAY_US))
    kept=sorted(json.loads((d/'manifest.json').read_text())['saved_tick']//DAY_US for d in tmp_path.glob('day-*'))
    assert kept==[0,7,14]+list(range(16,30))


def test_prune_hashes_each_retained_bundle_once(tmp_path,monkeypatch):
    import hashlib
    from fab_sim import checkpoint_store
    store=CheckpointStore(tmp_path,retain_completed=10)
    for t in range(3):store.write(bundle(t))
    calls=[0];original=hashlib.sha256
    def counting(*args):
        calls[0]+=1;return original(*args)
    monkeypatch.setattr(checkpoint_store.hashlib,'sha256',counting)
    store.write(bundle(3))
    assert calls[0]==1  # the new payload's digest only; known bundles are not re-read
    # A bundle changed on disk is verified again (and no longer counts as retained).
    victim=next(d for d in tmp_path.glob('day-*') if json.loads((d/'manifest.json').read_text())['saved_tick']==1)/'state.json'
    victim.write_bytes(b'{}')
    calls[0]=0;store.write(bundle(4))
    assert calls[0]==2


def test_restarted_store_verifies_existing_bundles_again(tmp_path):
    store=CheckpointStore(tmp_path,retain_completed=2)
    store.write(bundle(1));store.write(bundle(2))
    victim=next(d for d in tmp_path.glob('day-*') if json.loads((d/'manifest.json').read_text())['saved_tick']==2)/'state.json'
    victim.write_bytes(b'{}')
    fresh=CheckpointStore(tmp_path,retain_completed=2)
    fresh.write(bundle(3))
    # The corrupted bundle does not count, so the older valid one is kept as the second.
    assert sorted(json.loads((d/'manifest.json').read_text())['saved_tick'] for d in tmp_path.glob('day-*'))==[1,2,3]
