from fractions import Fraction
import pytest
from test_route_rules import step


def state(current='A',transitions=None):
    from fab_sim.manufacturing import SetupState
    return SetupState(current,transitions or [])


def setup_step(target='B',when='need',fallback=None):
    return step(1,setup={'id':target,'when':when,'fallback':fallback})


def test_exact_transition_precedes_wildcard_and_expected_rank_never_samples():
    transitions=[{'current':None,'new':'B','duration_us':100,'minimum_runs':7},
                 {'current':'A','new':'B','duration_us':10,'minimum_runs':0}]
    setup=state(transitions=transitions); decision=setup.decision(setup_step())
    assert decision['duration_us']==10 and decision['expected_us']==Fraction(10)
    assert decision['minimum_runs']==0


def test_need_matching_setup_skips_and_always_matching_uses_fallback():
    setup=state(); fallback={'distribution':'uniform','mean_seconds':'2','offset_seconds':'1'}
    assert not setup.decision(setup_step('A',fallback=fallback))['required']
    decision=setup.decision(setup_step('A','always',fallback))
    assert decision['required'] and decision['expected_us']==2000000


def test_initial_setup_has_no_lock_and_actual_change_requires_seven_completed_lots():
    setup=state(transitions=[{'current':None,'new':'B','duration_us':10,'minimum_runs':7},
                            {'current':None,'new':'A','duration_us':10,'minimum_runs':7}])
    assert setup.can_use(setup_step())
    setup.apply(setup.decision(setup_step()))
    assert not setup.can_use(setup_step('A')) and setup.can_use(setup_step('B'))
    for _ in range(6): setup.credit(1)
    assert not setup.can_use(setup_step('A'))
    setup.credit(1); assert setup.can_use(setup_step('A'))


def test_feasible_queue_rank_prefers_priority_setup_then_age_without_lookahead():
    from fab_sim.manufacturing import queue_rank
    setup=state(transitions=[{'current':None,'new':'B','duration_us':10,'minimum_runs':0}])
    ready={'id':'visit:1','priority':10,'queue_ready_at_us':0}
    hot={'id':'visit:2','priority':20,'queue_ready_at_us':2}
    assert queue_rank(hot,setup_step(),setup)<queue_rank(ready,setup_step('A'),setup)
    matching={**ready,'id':'visit:3','queue_ready_at_us':100}
    assert queue_rank(matching,setup_step('A'),setup)<queue_rank(ready,setup_step(),setup)
    # An upstream candidate has no physical queue-ready time and cannot rank.
    with pytest.raises(ValueError,match='ready'): queue_rank({**hot,'queue_ready_at_us':None},setup_step(),setup)


def test_always_matching_setup_preserves_earned_change_lock():
    setup=state(transitions=[{'current':None,'new':'B','duration_us':10,'minimum_runs':7}])
    setup.apply(setup.decision(setup_step()))
    setup.credit(2)
    setup.apply(setup.decision(setup_step('B','always')))
    assert setup.completed_lots==2 and setup.minimum_runs==7
    assert not setup.can_use(setup_step('A'))


def test_stale_and_locked_setup_decisions_reject_without_change():
    setup=state(transitions=[{'current':None,'new':'B','duration_us':10,'minimum_runs':7},
                            {'current':None,'new':'A','duration_us':10,'minimum_runs':7}])
    before=setup.decision(setup_step())
    setup.apply(before)
    with pytest.raises(RuntimeError,match='stale'):setup.apply(before)
    with pytest.raises(RuntimeError,match='credits'):setup.apply(setup.decision(setup_step('A')))
    assert setup.current=='B'


def test_invalid_when_rejects_even_for_matching_recipe():
    with pytest.raises(ValueError,match='WHEN'):state().decision(setup_step('A','bogus'))
