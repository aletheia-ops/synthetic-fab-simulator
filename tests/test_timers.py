import pytest


def timer(duration=10):
    from fab_sim.scheduler import Scheduler
    from fab_sim.service import Timer
    s=Scheduler(); calls=[]
    t=Timer(s,'service',duration,lambda:calls.append(s.now_us))
    return s,t,calls


def test_overlapping_causes_preserve_remaining_and_complete_once():
    s,t,calls=timer(); s.settle(3); t.pause(3,'fault-a')
    s.settle(6); t.pause(6,'fault-b')
    assert t.remaining_us==7
    s.settle(7); t.resume(7,'fault-a'); assert s.next_time_us is None
    s.settle(8); t.resume(8,'fault-b'); assert s.next_time_us==15
    s.settle(15); assert calls==[15] and t.completed


def test_repeat_pause_is_idempotent_and_resume_requires_known_cause():
    s,t,calls=timer(); s.settle(3); t.pause(3,'fault'); t.pause(3,'fault')
    assert t.remaining_us==7 and t.causes=={'fault'}
    with pytest.raises(ValueError,match='cause'): t.resume(3,'unknown')
    t.resume(3,'fault'); s.settle(10); assert calls==[10]


def test_completion_at_fault_boundary_precedes_fault():
    s,t,calls=timer(); s.register('fault',lambda p:t.pause(s.now_us,'fault'))
    s.schedule(10,2,'fault',{}); s.settle(10)
    assert calls==[10] and t.completed


def test_zero_duration_completion_is_causal_and_negative_rejects():
    s,t,calls=timer(0); assert calls==[]
    s.settle(0); assert calls==[0]
    with pytest.raises(ValueError,match='duration'): timer(-1)


def test_pausing_requires_current_scheduler_tick():
    s,t,calls=timer()
    with pytest.raises(ValueError,match='time'): t.pause(2,'fault')
