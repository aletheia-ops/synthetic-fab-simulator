"""Retries retain real wakeups without rebuilding impossible assignments."""
import sys
from fab_sim.clearance import ClearancePool
from test_clearance import lease_fixture
from test_transport_resources import blocked_pickup


def count_calls(code, action):
    calls = 0
    def trace(frame, event, arg):
        nonlocal calls
        if event == 'call' and frame.f_code is code:
            calls += 1
    previous = sys.getprofile()
    try:
        sys.setprofile(trace)
        result = action()
    finally:
        sys.setprofile(previous)
    return calls, result


def test_insufficient_clearance_capacity_skips_pair_checks_and_release_retries():
    pool, t, m, s, c, lots = lease_fixture()
    c.reserve('protected:1', 'foreign')
    t._router()
    calls, result = count_calls(ClearancePool._compatible.__code__, pool.dispatch)
    assert result is False
    assert calls == 0
    assert pool.waits['t']['required_capacity'] == 2
    assert pool.waits['t']['available_capacity'] == 1
    c.release('protected:1', 'foreign')
    assert pool.dispatch() is True
    assert len(pool.records) == 1
    assert len(next(iter(pool.records.values()))['slots']) == 2


def test_custom_compatibility_retains_original_pair_evaluation():
    pool, t, m, s, c, lots = lease_fixture()
    c.reserve('protected:1', 'foreign')
    seen = []
    original = pool._compatible
    def compatible(member, sid):
        seen.append((member['visit_id'], sid))
        return original(member, sid)
    pool._compatible = compatible
    assert pool.dispatch() is False
    assert len(seen) == 2


def test_assignment_does_not_rank_already_dispatched_demands():
    traffic, t, m, s, c, lots, v, events, did = blocked_pickup()
    demand = t.demands[did]
    assert demand['status'] == 'pickup_wait'
    class NoRank(dict):
        def __getitem__(self, key):
            if key == 'original_request_time_us':
                raise AssertionError('nonpending demand was ranked')
            return super().__getitem__(key)
    t.demands[did] = NoRank(demand)
    assert t.assign() is False
