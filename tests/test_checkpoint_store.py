from dataclasses import replace
import json
import os
import pytest
from fab_sim.checkpoint_format import CheckpointManifest, CheckpointBundle, CheckpointError
from fab_sim.checkpoint_store import CheckpointStore, CheckpointWriteError


def bundle(tick=10):
    return CheckpointBundle(CheckpointManifest(
        format_version=1, state_contract='fab-native-v1', writer_build='test-build',
        runtime_compatibility={'python': 'test', 'rng': 'MT19937-v3'},
        physical_identity={'input_hashes': {'m': 'abc'}, 'policy': 'v1'},
        saved_tick=tick, settlements=3, parent_run_id='parent', parent_checkpoint=None,
        last_committed_sequence=7, physical_digest='digest', payload_checksums={}),
        {'heap': [(tick + 1, 1, 2, 'timer', None)], 'large': 2**80})


def test_round_trip_and_unique_completed_bundles(tmp_path):
    store = CheckpointStore(tmp_path)
    first = store.write(bundle()); second = store.write(bundle())
    assert first != second
    result = store.read(first)
    assert result.state == bundle().state
    assert result.manifest.saved_tick == 10
    assert result.manifest.last_committed_sequence == 7
    assert json.loads((tmp_path / 'latest.json').read_text())['manifest'] == str(second.relative_to(tmp_path))


@pytest.mark.parametrize('failure', ['payload', 'rename', 'fsync', 'latest'])
def test_interrupted_write_preserves_previous_checkpoint(tmp_path, monkeypatch, failure):
    store = CheckpointStore(tmp_path); previous = store.write(bundle())
    latest_bytes = (tmp_path / 'latest.json').read_bytes()
    original_replace = os.replace
    original_fsync = os.fsync
    def fail_replace(src, dst):
        if failure == 'rename' or (failure == 'latest' and str(dst).endswith('latest.json')):
            raise OSError('injected rename failure')
        return original_replace(src, dst)
    monkeypatch.setattr(os, 'replace', fail_replace)
    if failure == 'fsync':
        monkeypatch.setattr(os, 'fsync', lambda fd: (_ for _ in ()).throw(OSError('fsync failed')))
    if failure == 'payload':
        monkeypatch.setattr(store, '_write_file', lambda *args: (_ for _ in ()).throw(OSError('write failed')))
    with pytest.raises(CheckpointWriteError):
        store.write(bundle(20))
    assert (tmp_path / 'latest.json').read_bytes() == latest_bytes
    assert store.read(previous).state == bundle().state


@pytest.mark.parametrize('damage', ['checksum', 'missing', 'version', 'partial'])
def test_corrupt_or_incomplete_bundle_rejected(tmp_path, damage):
    store = CheckpointStore(tmp_path); manifest = store.write(bundle())
    if damage == 'checksum':
        (manifest.parent / 'state.json').write_bytes(b'corrupt')
    elif damage == 'missing':
        (manifest.parent / 'state.json').unlink()
    elif damage == 'version':
        data = json.loads(manifest.read_text()); data['format_version'] = 999
        manifest.write_text(json.dumps(data))
    else:
        partial = tmp_path / '.pending-test'; manifest.parent.rename(partial)
        manifest = partial / 'manifest.json'
    with pytest.raises(CheckpointError):
        store.read(manifest)


def test_bundles_are_named_by_simulated_day():
    from fab_sim.checkpoint_store import bundle_name
    assert bundle_name(5218696399281)=='day-060.40'
    assert bundle_name(0)=='day-000.00' and bundle_name(86_400_000_000*123+1)=='day-123.00'
