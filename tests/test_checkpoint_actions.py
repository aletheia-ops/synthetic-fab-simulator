import pytest
from fab_sim.actions import ActionRef, ActionRegistry
from fab_sim.checkpoint_format import CheckpointError
from fab_sim.scheduler import Scheduler
from fab_sim.service import Timer, SerializedHandler


def registry(events):
    actions = ActionRegistry()
    actions.register('record', lambda owner, payload: events.append((owner, payload)))
    actions.register('duration', lambda owner, payload: payload)
    actions.register('yes', lambda owner, payload: True)
    actions.register('noop', lambda owner, payload: None)
    return actions


def test_same_tick_order_and_cancelled_events_survive():
    events = []; actions = registry(events); s = Scheduler()
    a = Timer(s, 'a', 10, actions.bind(ActionRef('record', 'a', 1)))
    b = Timer(s, 'b', 10, actions.bind(ActionRef('record', 'b', 2)))
    c = Timer(s, 'c', 20, actions.bind(ActionRef('record', 'c', 3))); c.cancel()
    s.settle(0)
    state = s.export_state(); restored = Scheduler()
    Timer.restore(restored, a.export_state(), actions)
    Timer.restore(restored, b.export_state(), actions)
    Timer.restore(restored, c.export_state(), actions)
    restored.restore_state(state)
    restored.settle(10)
    assert events == [('a', 1), ('b', 2)]
    assert restored.next_time_us is None
    assert restored._sequence == 3


def test_restore_paused_timer_without_rescheduling():
    events = []; actions = registry(events); s = Scheduler()
    timer = Timer(s, 'a', 100, actions.bind(ActionRef('record', 'a', None)))
    s.settle(20); timer.pause(20, 'fault1'); timer.pause(20, 'fault2')
    state = s.export_state(); restored = Scheduler()
    clock = Timer.restore(restored, timer.export_state(), actions)
    restored.restore_state(state)
    assert clock.remaining_us == 80
    assert restored.next_time_us is None
    clock.resume(20, 'fault1'); assert restored.next_time_us is None
    clock.resume(20, 'fault2'); assert restored.next_time_us == 100
    restored.settle(100); assert events == [('a', None)]


def test_pending_duration_is_not_sampled_by_restore():
    events = []; draws = []; actions = registry(events)
    actions.register('draw', lambda owner, payload: draws.append(7) or 7)
    s = Scheduler(); s.register_pass('manufacturing', lambda: None)
    h = SerializedHandler(s, 'lane', {}, lambda: s.mark_dirty('manufacturing'), lambda *args: None)
    h.request('a', 'load', ['c'], actions.bind(ActionRef('draw', 'lane', None)),
        actions.bind(ActionRef('record', 'a', 'done')),
        eligible=actions.bind(ActionRef('yes', '', None)), on_start=actions.bind(ActionRef('noop', '', None)))
    s.settle(0)
    state = h.export_state(); restored = Scheduler(); restored.register_pass('manufacturing', lambda: None)
    clone = SerializedHandler.restore(restored, state, actions, carrier_claims={},
        dirty=lambda: restored.mark_dirty('manufacturing'), emit=lambda *args: None)
    restored.restore_state(s.export_state())
    assert draws == []
    clone.dispatch(); restored.settle(0)
    assert draws == [7]
    active_state = clone.export_state(); scheduler_state = restored.export_state()
    third = Scheduler(); third.register_pass('manufacturing', lambda: None)
    active = SerializedHandler.restore(third, active_state, actions, carrier_claims=dict(clone.claims),
        dirty=lambda: third.mark_dirty('manufacturing'), emit=lambda *args: None)
    third.restore_state(scheduler_state); third.settle(7)
    assert draws == [7]
    assert active.active is None
    assert events == [('a', 'done')]


def test_unknown_action_rejected():
    with pytest.raises(CheckpointError):
        ActionRegistry().bind(ActionRef('missing', 'owner', None))


def test_opaque_callback_and_dirty_scheduler_rejected():
    s = Scheduler(); s.register_pass('manufacturing', lambda: None)
    timer = Timer(s, 'a', 10, lambda: None)
    with pytest.raises(CheckpointError): timer.export_state()
    s.mark_dirty('manufacturing')
    with pytest.raises(CheckpointError): s.export_state()


def test_invalid_heap_deadline_rejected():
    s = Scheduler(); state = s.export_state()
    state['heap'] = [(1, 0, 1, 'unknown', {})]; state['pending'] = {1}; state['sequence'] = 1
    with pytest.raises(CheckpointError): Scheduler().restore_state(state)
