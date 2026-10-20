from decimal import Decimal
import sys
from fractions import Fraction
import pytest
from fab_sim.checkpoint_format import (CheckpointError, decode_state_payload, decode_value, encode_value,
    write_encoded_value)

CASES = [None, True, False, 0, -5, 10**40, 'a"b\\cé\x7f\n', b'\x00\xff', 1.5, -0.0, Decimal('1.10'),
    Fraction(3, 7), {'b': 1, 'a': [1, 2, (3,)]}, {1: 'x', (1, 2): 'y', 'z': None, 'aé': 2},
    {'a"': 1, 'a': 2}, {3, 1, 'x', (1, 2)}, frozenset(), [[], {}, (), {'k': {'n': [{'q': {'a', 'b'}}]}}],
    {(): 1, frozenset([1]): 2}]


def streamed(value, chunk_chars=3):
    chunks = []; write_encoded_value(value, chunks.append, chunk_chars=chunk_chars)
    return b''.join(chunks)


@pytest.mark.parametrize('value', CASES)
def test_streamed_encoding_equals_encode_value(value):
    assert streamed(value) == encode_value(value)


def test_streamed_encoding_rejects_what_encode_value_rejects():
    for bad in (float('nan'), object()):
        with pytest.raises(CheckpointError):streamed(bad)
    cyclic = []; cyclic.append(cyclic)
    with pytest.raises(CheckpointError):streamed(cyclic)


def captured_state(tmp_path):
    from test_runner import fixture
    from fab_sim.runner import build_runtime, load_config
    from fab_sim.runtime_state import capture_runtime
    owner = build_runtime(load_config(fixture(tmp_path)), lambda *a: None, initialize=True)
    owner.scheduler.settle(owner.scheduler.next_time_us)
    return {'control': {'settlements': 1}, 'evidence': {}, 'progress': {}, 'runtime': capture_runtime(owner)}


def test_runtime_state_streams_and_restores_identically(tmp_path):
    state = captured_state(tmp_path)
    payload = encode_value(state)
    assert streamed(state, chunk_chars=1 << 16) == payload
    assert len(state['runtime']['nodes']) > 100
    assert decode_state_payload(payload) == decode_value(payload) == state


def test_payload_without_the_node_layout_uses_the_plain_decode():
    for state in ({'other': 1}, {'runtime': {'nodes': [], 'owners': {}}}):
        assert decode_state_payload(encode_value(state)) == state
    with pytest.raises(CheckpointError):
        decode_state_payload(b'["dict",[[["str","runtime"],["dict",[[["str","nodes"],["list",[["none"];]]]]]]]')


def test_checkpoint_and_log_pages_are_dropped_from_the_cache_after_fsync(tmp_path, monkeypatch):
    import json, os
    from test_runner import fixture
    from fab_sim.runner import run
    calls = []
    real = getattr(os, 'posix_fadvise', None)
    def fadvise(fd, offset, length, advice):
        calls.append((os.fstat(fd).st_size, advice))
        if real:real(fd, offset, length, advice)
    monkeypatch.setattr(os, 'posix_fadvise', fadvise, raising=False)
    monkeypatch.setattr(os, 'POSIX_FADV_DONTNEED', getattr(os, 'POSIX_FADV_DONTNEED', 4), raising=False)
    path = fixture(tmp_path); config = json.loads(path.read_text())
    baseline = run(path)
    config['output'] = {'directory': 'ck', 'run_id': 'ck'}
    config['checkpoint'] = {'enabled': True, 'interval_wall_seconds': 10, 'retain_completed': 2}
    path.write_text(json.dumps(config))
    wall = [0]
    def clock():wall[0] += 1;return wall[0]
    result = run(path, observation_clock=clock)
    assert result['domain_digest'] == baseline['domain_digest']
    assert calls and all(advice == os.POSIX_FADV_DONTNEED for _, advice in calls)
    assert any(size > 0 for size, _ in calls)


def test_lazy_capture_streams_the_same_bytes_as_an_eager_capture(tmp_path):
    from test_runner import fixture
    from fab_sim.runner import build_runtime, load_config
    from fab_sim.runtime_state import capture_runtime
    from fab_sim.checkpoint_format import LazyList, encode_value, write_encoded_value
    config=load_config(fixture(tmp_path))
    owners=build_runtime(config,lambda *a:None,initialize=True)
    for _ in range(40):owners.scheduler.settle(owners.scheduler.next_time_us)
    eager=capture_runtime(owners);lazy=capture_runtime(owners,lazy=True)
    assert type(lazy['nodes']) is LazyList and len(lazy['nodes'])==len(eager['nodes'])
    out=[];write_encoded_value(lazy,out.append)
    assert b''.join(out)==encode_value(eager)==encode_value(lazy)


def test_restore_interns_string_leaves(tmp_path):
    from test_runner import fixture
    from fab_sim.runner import build_runtime, load_config
    from fab_sim.runtime_state import capture_runtime, restore_runtime, runtime_digest
    from fab_sim.checkpoint_format import encode_value, decode_value
    config=load_config(fixture(tmp_path))
    owners=build_runtime(config,lambda *a:None,initialize=True)
    owners.scheduler.settle(owners.scheduler.next_time_us)
    state=decode_value(encode_value(capture_runtime(owners)))
    clone=build_runtime(config,lambda *a:None,initialize=False)
    restore_runtime(clone,state,clone.actions)
    assert runtime_digest(clone)==runtime_digest(owners)
    keys=[k for record in clone.manufacturing.lots.values() for k in record if type(k) is str]
    assert keys and all(k is sys.intern(k) for k in keys)
