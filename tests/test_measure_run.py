import json
import pytest


def test_evidence_summary_streams_contiguous_commits_and_counts_physical_exits(tmp_path):
    from tools.measure_run import summarize_evidence
    rows=[{'event_sequence':i,'simulation_time_us':tick,'event_type':kind}
          for i,tick,kind in [(1,0,'lot_admitted'),(2,0,'visit_entered'),(4,10,'lot_exited')]]
    (tmp_path/'events.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    (tmp_path/'oht.jsonl').write_text(json.dumps({'event_sequence':3,'simulation_time_us':5,'event_type':'oht_segment'})+'\n')
    (tmp_path/'diagnostics.jsonl').write_text('{}\n')
    (tmp_path/'manifest.json').write_text(json.dumps({'record_counts':{'events':3,'oht':1,'diagnostics':1}}))
    result=summarize_evidence(tmp_path,4)
    assert result['event_count']==4 and result['event_counts']['lot_exited']==1
    assert result['max_same_tick_event_burst']==2 and result['last_event_time_us']==10
    assert result['oht_event_count']==result['diagnostic_count']==1


def test_evidence_summary_rejects_missing_commits_instead_of_reporting_success(tmp_path):
    from tools.measure_run import summarize_evidence
    (tmp_path/'events.jsonl').write_text(json.dumps({'event_sequence':2,'simulation_time_us':0,'event_type':'lot_exited'})+'\n')
    (tmp_path/'oht.jsonl').write_text('')
    with pytest.raises(ValueError,match='sequence'):summarize_evidence(tmp_path,2)


def test_measurement_never_replaces_a_report_or_starts_another_run(tmp_path,monkeypatch):
    from tools.measure_run import measure
    path=tmp_path/'report.json';path.write_text('existing')
    monkeypatch.setattr('tools.measure_run.run',lambda config, **kwargs:pytest.fail('run must not start'))
    with pytest.raises(FileExistsError):measure(tmp_path/'config.json',path)
    assert path.read_text()=='existing'


def test_measurement_preserves_incomplete_result(tmp_path,monkeypatch):
    from tools.measure_run import measure
    (tmp_path/'events.jsonl').write_text('');(tmp_path/'oht.jsonl').write_text('')
    (tmp_path/'diagnostics.jsonl').write_text('{}\n')
    (tmp_path/'manifest.json').write_text(json.dumps({'record_counts':{'events':0,'oht':0,'diagnostics':1}}))
    result={'resolved_config':{'output':{'directory':str(tmp_path)}},'last_committed_sequence':0,
            'outcome':'Incomplete','reason':'horizon','exited_count':0,'cohort_size':24}
    monkeypatch.setattr('tools.measure_run.run',lambda config, **kwargs:result)
    report=measure(tmp_path/'config.json',tmp_path/'report.json')
    assert report['result']['outcome']=='Incomplete' and report['result']['reason']=='horizon'
    assert report['evidence']['event_count']==0


@pytest.mark.parametrize('truncated_kind',['oht','diagnostics'])
def test_evidence_summary_rejects_truncated_output(tmp_path,truncated_kind):
    from tools.measure_run import summarize_evidence
    (tmp_path/'events.jsonl').write_text('')
    (tmp_path/'oht.jsonl').write_text(json.dumps({'event_sequence':1,'simulation_time_us':0,'event_type':'oht_segment'})+'\n')
    (tmp_path/'diagnostics.jsonl').write_text('{}\n')
    counts={'events':0,'oht':1,'diagnostics':1};counts[truncated_kind]=2
    (tmp_path/'manifest.json').write_text(json.dumps({'record_counts':counts}))
    with pytest.raises(ValueError,match=truncated_kind):summarize_evidence(tmp_path,1)


def test_measurement_records_failure_for_inconsistent_required_evidence(tmp_path,monkeypatch):
    from tools.measure_run import measure
    (tmp_path/'events.jsonl').write_text('')
    (tmp_path/'oht.jsonl').write_text('')
    (tmp_path/'diagnostics.jsonl').write_text('{}\n')
    (tmp_path/'manifest.json').write_text(json.dumps({'record_counts':{'events':0,'oht':0,'diagnostics':2}}))
    monkeypatch.setattr('tools.measure_run.run',lambda config, **kwargs:{
        'resolved_config':{'output':{'directory':str(tmp_path)}},'last_committed_sequence':0})
    with pytest.raises(ValueError,match='diagnostics'):measure(tmp_path/'config.json',tmp_path/'report.json')
    assert json.loads((tmp_path/'report.json').read_text())['measurement_status']=='failed'
