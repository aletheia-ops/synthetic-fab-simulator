import pytest
from fab_sim.outcomes import classify_stop


def state(**changes):
    return dict(now_us=10,horizon_us=10,settled=True,cohort_size=2,exited_count=2,
        pending_release_count=0,pending_admission_count=0,wip_count=0,active_execution_count=0,
        loaded_transfer_count=0,output_status='complete',checks_assessed=True,
        violation_ids=[],unresolved_obligations=[],**changes)


def test_last_exit_at_inclusive_horizon_completes_ignoring_empty_moves_and_maintenance():
    s=state();s.update(recurring_maintenance=['unrelated'],empty_repositions=['v1'])
    result=classify_stop(s)
    assert result['outcome']=='Completed' and result['constraint_status']=='satisfied' and result['stop_domain']
    s['settled']=False;assert classify_stop(s)['outcome'] is None


@pytest.mark.parametrize('field',['pending_release_count','pending_admission_count','wip_count','active_execution_count','loaded_transfer_count'])
def test_completion_requires_all_physical_and_intent_conditions(field):
    s=state();s[field]=1
    result=classify_stop(s);assert result['outcome']=='Incomplete' and result['reason']=='horizon'


def test_output_pending_drains_without_advancing_domain_but_failure_beats_completion():
    s=state();s['output_status']='pending';r=classify_stop(s)
    assert r['outcome'] is None and r['domain_complete'] and r['stop_domain'] and r['reason']=='required_output_pending'
    s['output_status']='failed';assert classify_stop(s)['outcome']=='Failed'
    s=state();s['failure']={'kind':'invariant','message':'lost custody'}
    assert classify_stop(s)['outcome']=='Failed'
    assert classify_stop({'rejection':{'reason':'unsupported input'}})['outcome']=='Rejected'


def test_known_constraint_violation_beats_unresolved_even_when_incomplete():
    s=state();s.update(exited_count=1,wip_count=1,violation_ids=['q1'],unresolved_obligations=['q2'],checks_assessed=False)
    r=classify_stop(s);assert r['constraint_status']=='violated' and r['outcome']=='Incomplete'
    assert r['violation_ids']==['q1'] and r['unresolved_obligations']==['q2']
    assert r['violation_count']==1 and r['unresolved_count']==1
    s['violation_ids']=[];assert classify_stop(s)['constraint_status']=='unassessed'


def test_uncertain_wait_cycle_never_proves_permanent_blockage():
    s=state();s.update(now_us=5,exited_count=1,wip_count=1,cycles=[['a','b']],relevant_future_events=[])
    assert classify_stop(s)['outcome'] is None
    s['now_us']=10;assert classify_stop(s)['outcome']=='Incomplete'


def test_operator_stop_is_incomplete_and_unsettled_failure_still_fails():
    s=state();s.update(now_us=1,exited_count=0,wip_count=2,operator_stop=True)
    assert classify_stop(s)['reason']=='operator_stop'
    s.update(settled=False,failure={'message':'partial transition'})
    assert classify_stop(s)['outcome']=='Failed'


def test_cannot_classify_events_executed_after_horizon():
    s=state();s['now_us']=11
    with pytest.raises(ValueError):classify_stop(s)


def test_operator_stop_between_commits_does_not_require_more_dispatch():
    s=state();s.update(now_us=1,settled=False,operator_stop=True,exited_count=1,wip_count=1)
    r=classify_stop(s);assert r['outcome']=='Incomplete' and r['reason']=='operator_stop' and r['stop_domain']


def test_operator_stop_remains_incomplete_even_when_cohort_has_just_exited():
    s=state();s['operator_stop']=True
    r=classify_stop(s);assert r['outcome']=='Incomplete' and r['reason']=='operator_stop' and r['domain_complete']


@pytest.mark.parametrize('field',['cohort_size','pending_release_count'])
def test_finite_assessment_rejects_null_future_counts(field):
    s=state();s[field]=None
    with pytest.raises(ValueError):classify_stop(s)
