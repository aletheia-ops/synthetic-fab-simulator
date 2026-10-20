import pytest
from decimal import Decimal


def scheduler(**settings):
    from fab_sim.scheduler import Scheduler
    return Scheduler(**settings)


def test_pending_events_use_tick_phase_then_insertion_order():
    s=scheduler(); calls=[]; s.register('record',calls.append)
    for phase in (3,2,1,0,0): s.schedule(1,phase,'record',{'phase':phase})
    s.settle(1)
    assert [p['phase'] for p in calls]==[0,0,1,2,3]


def test_zero_duration_child_follows_parent_before_pending_later_phase():
    from fab_sim.timebase import to_ticks
    s=scheduler(); calls=[]
    def parent(payload):
        calls.append('parent'); s.schedule(s.now_us+to_ticks(Decimal('0.0000001')),0,'child',{})
    s.register('parent',parent); s.register('child',lambda p:calls.append('child'))
    s.register('pending',lambda p:calls.append('pending'))
    s.schedule(0,2,'parent',{}); s.schedule(0,3,'pending',{})
    s.settle(0)
    assert calls==['parent','child','pending']


def test_recovery_then_new_fault_settle_before_dispatch():
    s=scheduler(); calls=[]; unavailable={'old'}
    s.register('recover',lambda p:unavailable.remove('old'))
    s.register('fault',lambda p:unavailable.add('new'))
    s.register_pass('manufacturing',lambda:calls.append(tuple(sorted(unavailable))))
    s.mark_dirty('manufacturing'); s.schedule(1,1,'recover',{}); s.schedule(1,2,'fault',{})
    s.settle(1)
    assert calls==[('new',)]


def test_dirty_closure_settles_children_and_returns_to_earlier_passes():
    s=scheduler(); calls=[]
    s.register('event',lambda p:calls.append('event'))
    s.register_pass('admission',lambda:calls.append('admission'))
    def manufacturing():
        calls.append('manufacturing'); s.schedule(s.now_us,0,'event',{}); s.mark_dirty('admission')
    s.register_pass('manufacturing',manufacturing)
    s.register_pass('transfer',lambda:calls.append('transfer'))
    s.register_pass('traffic',lambda:calls.append('traffic'))
    for name in ('traffic','transfer','manufacturing','admission'): s.mark_dirty(name)
    s.settle(0)
    assert calls==['admission','manufacturing','event','admission','transfer','traffic']


def test_noop_pass_is_not_repeated_and_deadlines_follow_closure():
    s=scheduler(); calls=[]
    s.register_pass('traffic',lambda:calls.append('traffic'))
    s.add_deadline_check(lambda:calls.append('deadline'))
    s.mark_dirty('traffic'); s.settle(0)
    assert calls==['traffic','deadline'] and not s.dirty


def test_past_invalid_tick_phase_or_unknown_kind_reject():
    s=scheduler(); s.register('x',lambda p:None); s.settle(2)
    for tick in (1,-1,True,2.0):
        with pytest.raises(ValueError): s.schedule(tick,0,'x',{})
    with pytest.raises(ValueError): s.schedule(2,4,'x',{})
    with pytest.raises(ValueError): s.schedule(2,0,'missing',{})


def test_cancellation_removes_stale_next_tick():
    s=scheduler(); calls=[]; s.register('x',calls.append)
    first=s.schedule(1,0,'x',{'id':1}); s.schedule(2,0,'x',{'id':2}); s.cancel(first)
    assert s.next_time_us==2
    s.settle(2); assert calls==[{'id':2}]


def test_skipping_unsettled_earlier_event_rejects():
    s=scheduler(); s.register('x',lambda p:None); s.schedule(1,0,'x',{})
    with pytest.raises(ValueError,match='unsettled'): s.settle(2)


def test_suspected_zero_time_loop_fails_with_bounded_evidence():
    from fab_sim.scheduler import TransitionBudgetExceeded
    s=scheduler(transition_budget=5,recent_limit=3)
    s.register('loop',lambda p:s.schedule(s.now_us,0,'loop',{}))
    s.schedule(0,0,'loop',{})
    with pytest.raises(TransitionBudgetExceeded) as error: s.settle(0)
    assert error.value.evidence['simulation_time_us']==0
    assert len(error.value.evidence['recent'])<=3
    assert error.value.evidence['reason']=='same_timestamp_budget'
