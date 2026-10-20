import json
from copy import deepcopy
from test_runner import fixture


def observer():
    from fab_sim.progress import Progress
    lots = {'a': {'product_id': 'p', 'admitted_at_us': 0}, 'b': {'product_id': 'q', 'admitted_at_us': 0}}
    visits = {'v': {'lot_id': 'a', 'source_step_id': '12'}}
    return Progress(lots, visits, ['p', 'q']), lots, visits


def test_maintenance_is_not_manufacturing_progress_and_first_exits_are_separate():
    progress, lots, visits = observer()
    progress.observe('processing_completed', {'lot_id': 'a'}, 10)
    progress.observe('transfer_delivered', {'lot_id': 'a'}, 20)
    progress.observe('maintenance_recovered', {}, 100)
    snapshot = progress.snapshot(100)
    assert snapshot['last_progress_at_us']['processing_completed'] == 10
    assert snapshot['last_progress_at_us']['transfer_delivered'] == 20
    assert snapshot['products']['p']['max_entered_step'] == 12
    lots['a']['exited_at_us'] = 110
    progress.observe('lot_exited', {'lot_id': 'a'}, 110)
    assert progress.snapshot(110)['first_of_each_kind_at_us'] is None
    lots['b']['exited_at_us'] = 120
    progress.observe('lot_exited', {'lot_id': 'b'}, 120)
    assert progress.snapshot(120)['first_of_each_kind_at_us'] == 120


def test_inactivity_is_an_observation_not_a_stop_rule():
    progress, lots, visits = observer()
    result = progress.snapshot(86_400_000_000)
    assert result['inactivity_warning'] is True
    assert 'stop_domain' not in result


def test_progress_counts_follow_events_without_scanning_history():
    from test_active_ledgers import Evidence
    progress,lots,visits=observer()
    progress.lots=Evidence(lots);progress.visits=Evidence(visits)
    lots['c']={'lot_id':'c','product_id':'p','admitted_at_us':5}
    progress.lots['c']=lots['c']
    progress.observe('lot_admitted',lots['c'],5)
    progress.observe('visit_entered',{'lot_id':'c','source_step_id':'14'},6)
    progress.observe('visit_entered',{'lot_id':'c','source_step_id':'4'},7)
    lots['c']['exited_at_us']=8;progress.observe('lot_exited',{'lot_id':'c'},8)
    result=progress.snapshot(8)
    assert result['products']['p']=={'admitted':2,'max_entered_step':14,'exited':1}
    assert result['products']['q']=={'admitted':1,'max_entered_step':None,'exited':0}
    result['products']['p']['admitted']=999
    assert progress.snapshot(8)['products']['p']['admitted']==2


def test_observation_does_not_change_events_or_digest(tmp_path):
    from fab_sim.runner import run
    path = fixture(tmp_path)
    baseline = run(path)
    rows = (tmp_path/'result/events.jsonl').read_bytes()
    config = json.loads(path.read_text())
    config['output']['directory'] = 'observed'
    path.write_text(json.dumps(config))
    reports = []
    tick = iter(range(100000))
    observed = run(path, progress=reports.append, progress_interval=1, observation_clock=lambda: next(tick))
    assert reports and observed['domain_digest'] == baseline['domain_digest']
    assert (tmp_path/'observed/events.jsonl').read_bytes() == rows
    assert observed['progress']['products']['p']['exited'] == 2


def test_reporter_failure_does_not_fail_domain(tmp_path):
    from fab_sim.runner import run
    def broken(record): raise OSError('closed progress pipe')
    result = run(fixture(tmp_path), progress=broken, observation_clock=lambda: 10)
    assert result['outcome'] == 'Completed'


def test_progress_cli_keeps_result_json_on_stdout(tmp_path):
    from test_canonical_cli import invoke
    result = invoke('run', '--config', fixture(tmp_path), '--progress', '--progress-interval', '1')
    assert result.returncode == 0
    assert json.loads(result.stdout)['outcome'] == 'Completed'
    assert all(json.loads(line)['schema'] == 'fab-progress-v1' for line in result.stderr.splitlines())


def test_long_operation_and_partial_batch_proof_continue():
    from fab_sim.outcomes import classify_stop
    from test_outcomes import state
    value = state()
    value.update(now_us=5, exited_count=0, wip_count=2, active_execution_count=1,
        remaining_lot_ids=['a', 'b'], relevant_future_events=[],
        blockage_proofs=[{'kind': 'resource_unavailable', 'required_condition': 'rack release',
            'possible_producers': [], 'assessment_complete': True, 'covered_lot_ids': ['a', 'b']}])
    assert not classify_stop(value)['stop_domain']
