import gc
import weakref
from test_runner import fixture
from fab_sim.runner import build_runtime,load_config
from fab_sim.runtime_state import capture_runtime,restore_runtime,runtime_digest
from fab_sim.checkpoint_format import encode_value
from fab_sim.derived_cache import DerivedCache

def busy(tmp_path):
    config=load_config(fixture(tmp_path))
    owner=build_runtime(config,lambda *a:None,initialize=True)
    for _ in range(500):
        owner.scheduler.settle(owner.scheduler.next_time_us)
        if any(h.active for h in owner.manufacturing.handlers.values()):return config,owner
    raise AssertionError('fixture never has an active timer')

def listeners(owner):
    return (len(owner.custody._listeners),len(owner.custody._resource_listeners),
        len(owner.manufacturing._resource_listeners))

def test_repeated_restore_does_not_duplicate_listeners(tmp_path):
    config,owner=busy(tmp_path);state=capture_runtime(owner)
    expected=listeners(owner);digest=runtime_digest(owner)
    for _ in range(20):
        restore_runtime(owner,state,owner.actions)
        assert listeners(owner)==expected
        assert runtime_digest(owner)==digest
    abandoned=[]
    for _ in range(20):
        clone=build_runtime(config,lambda *a:None,initialize=False)
        restore_runtime(clone,state,clone.actions)
        assert listeners(clone)==expected
        abandoned.append(weakref.ref(clone.manufacturing))
        del clone
    gc.collect()
    assert all(ref() is None for ref in abandoned)

def test_warm_cold_cache_capture_matches(tmp_path):
    _,owner=busy(tmp_path)
    before=encode_value(capture_runtime(owner));digest=runtime_digest(owner)
    cache=owner.traffic._geometry_cache
    for i in range(1000):cache.put('test',i,tuple(range(100)))
    assert cache.stats()['entries']
    assert encode_value(capture_runtime(owner))==before
    assert runtime_digest(owner)==digest
    cache.clear()
    assert encode_value(capture_runtime(owner))==before
