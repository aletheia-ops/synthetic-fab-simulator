"""Exit observations stop at settled boundaries without asserting an open FAB drained."""
import pytest
from test_outcomes import state
from fab_sim.outcomes import classify_stop


def open_state(**changes):
    s=state();s.update(now_us=5,release_mode='ongoing',cohort_size=None,
        pending_release_count=None,source_open=True,exited_count=3,wip_count=2,
        exit_targets={'p':3},product_exit_counts={'p':3},exit_targets_reached_at_us=5)
    s.update(changes);return s


def test_open_source_target_is_an_observation_not_a_drain():
    r=classify_stop(open_state())
    assert (r['outcome'],r['reason'])==('Incomplete','exit_target_reached')
    assert r['objective_achieved'] is True and r['domain_complete'] is False


def test_unsettled_target_and_output_pending_never_claim_final_observation():
    r=classify_stop(open_state(settled=False,exit_targets_reached_at_us=None))
    assert r['reason']=='awaiting_timestamp_settlement' and not r['stop_domain']
    r=classify_stop(open_state(output_status='pending'))
    assert r['reason']=='required_output_pending' and r['stop_domain'] and r['outcome'] is None


@pytest.mark.parametrize('extra,reason',[({'failure':{'message':'broken'}},'runtime_failure'),
    ({'output_status':'failed'},'required_output_failure'),({'rejection':{'message':'invalid'}},'input_rejected'),
    ({'operator_stop':True},'operator_stop')])
def test_failure_rejection_and_operator_have_precedence(extra,reason):
    r=classify_stop(open_state(**extra));assert r['reason']==reason and r['objective_achieved']


def test_inclusive_horizon_target_beats_horizon_but_finite_drain_wins():
    assert classify_stop(open_state(now_us=10))['reason']=='exit_target_reached'
    s=state();s.update(exit_targets={'p':2},product_exit_counts={'p':2},exit_targets_reached_at_us=10)
    r=classify_stop(s);assert r['outcome']=='Completed' and r['objective_achieved']
    r=classify_stop(open_state(now_us=10,product_exit_counts={'p':2},exit_targets_reached_at_us=None))
    assert r['reason']=='horizon' and r['objective_achieved'] is False


def test_open_source_without_target_cannot_complete_or_prove_all_work_blocked():
    s=open_state();s.pop('exit_targets');s.pop('product_exit_counts');s.pop('exit_targets_reached_at_us')
    assert not classify_stop(s)['stop_domain']
    s['now_us']=10;assert classify_stop(s)['reason']=='horizon'


@pytest.mark.parametrize('change',[{'cohort_size':2},{'pending_release_count':0},{'source_open':False},
    {'exit_targets_reached_at_us':6},{'exit_targets_reached_at_us':True}])
def test_inconsistent_open_or_target_state_is_rejected(change):
    with pytest.raises(ValueError):classify_stop(open_state(**change))


@pytest.mark.parametrize('targets',[{}, {'p':True},{'p':0},{'p':-1},{'p':1.2},{'':1},[],None])
def test_exit_target_config_rejects_bool_zero_empty_and_invalid_shape(tmp_path,targets):
    from fab_sim.runner import load_config
    from test_runner import fixture
    path=fixture(tmp_path,execution={'horizon_us':1000000000,'exit_targets':targets})
    with pytest.raises(ValueError):load_config(path)


def test_target_validation_rejects_unknown_products():
    from fab_sim.outcomes import validate_exit_targets
    with pytest.raises(ValueError):validate_exit_targets({'unknown':3},{'p'})
    assert validate_exit_targets({'p':3},{'p'})=={'p':3}


def test_valid_target_config_is_optional_and_does_not_add_finite_defaults(tmp_path):
    from fab_sim.runner import load_config
    from test_runner import fixture
    assert 'exit_targets' not in load_config(fixture(tmp_path))['execution']
    import json
    path=tmp_path/'run.json';config=json.loads(path.read_text())
    config['execution']['exit_targets']={'p':3};path.write_text(json.dumps(config))
    assert load_config(path)['execution']['exit_targets']=={'p':3}
