"""Launcher affinity must be verifiable, reversible, and honest about VMs."""
import os
import json
import pytest


def test_selection_respects_allowed_mask_and_hybrid_metadata(tmp_path):
    from tools.cpu_affinity import plan_affinity
    metadata=tmp_path/'cpus';metadata.write_text('0-3,8-11\n')
    plan=plan_affinity('performance',allowed={2,3,4,9},release='linux',metadata=metadata)
    assert plan['selected_cpus']==[2,3,9]
    assert plan['status']=='planned'


@pytest.mark.parametrize('value',['','2-1','-1','x','1,,2','1-3-5'])
def test_invalid_explicit_cpu_lists_are_rejected(value):
    from tools.cpu_affinity import plan_affinity
    with pytest.raises(ValueError):plan_affinity(value,allowed={1,2,3},release='linux')


def test_explicit_mask_cannot_escape_existing_affinity():
    from tools.cpu_affinity import plan_affinity
    with pytest.raises(ValueError,match='allowed'):plan_affinity('1,4',allowed={1,2},release='linux')
    assert plan_affinity('1-2',allowed={1,2,3},release='linux')['selected_cpus']==[1,2]


def test_wsl_does_not_claim_guest_cpu_ids_are_host_p_cores(tmp_path):
    from tools.cpu_affinity import plan_affinity
    metadata=tmp_path/'cpus';metadata.write_text('0-3')
    plan=plan_affinity('performance',allowed={0,1},release='6.6-microsoft-standard-WSL2',metadata=metadata)
    assert plan['status']=='unavailable' and plan['selected_cpus'] is None
    assert plan['scope']=='guest_vcpu'
    explicit=plan_affinity('1',allowed={0,1},release='WSL2')
    assert explicit['selected_cpus']==[1] and explicit['scope']=='guest_vcpu'


@pytest.mark.parametrize('content',[None,'bad','4-5'])
def test_missing_invalid_or_disallowed_core_metadata_does_not_guess(tmp_path,content):
    from tools.cpu_affinity import plan_affinity
    metadata=tmp_path/'cpus'
    if content is not None:metadata.write_text(content)
    plan=plan_affinity('performance',allowed={0,1},release='linux',metadata=metadata)
    assert plan['status']=='unavailable' and plan['selected_cpus'] is None


def test_unsupported_os_keeps_default_but_rejects_explicit_selection():
    from tools.cpu_affinity import plan_affinity
    assert plan_affinity('performance',allowed=None,release='windows')['status']=='unavailable'
    assert plan_affinity('none',allowed=None,release='windows')['status']=='unchanged'
    with pytest.raises(ValueError):plan_affinity('0',allowed=None,release='windows')


@pytest.mark.skipif(not hasattr(os,'sched_setaffinity'),reason='Linux affinity required')
@pytest.mark.parametrize('raises',[False,True])
def test_real_process_affinity_is_verified_and_restored(raises):
    from tools.cpu_affinity import affinity_context
    original=os.sched_getaffinity(0);selected=min(original)
    try:
        with affinity_context(str(selected)) as report:
            assert os.sched_getaffinity(0)=={selected}
            assert report['effective_cpus']==[selected] and report['status']=='applied'
            if raises:raise RuntimeError('domain failure')
    except RuntimeError:
        assert raises
    assert os.sched_getaffinity(0)==original


def test_existing_report_never_changes_affinity(tmp_path,monkeypatch):
    from tools.measure_run import measure
    path=tmp_path/'report.json';path.write_text('existing')
    monkeypatch.setattr(os,'sched_setaffinity',lambda *args:pytest.fail('must not change affinity'))
    with pytest.raises(FileExistsError):measure(tmp_path/'config.json',path,cpu_affinity='performance')


def test_measurement_records_affinity_without_changing_simulator_result(tmp_path,monkeypatch):
    from tools.measure_run import measure
    for name in ('events.jsonl','oht.jsonl','diagnostics.jsonl'):(tmp_path/name).write_text('')
    (tmp_path/'manifest.json').write_text(json.dumps({'record_counts':{'events':0,'oht':0,'diagnostics':0}}))
    result={'resolved_config':{'output':{'directory':str(tmp_path)}},'last_committed_sequence':0,
            'outcome':'Incomplete','reason':'horizon'}
    monkeypatch.setattr('tools.measure_run.run',lambda config,**kwargs:result)
    report=measure(tmp_path/'config.json',tmp_path/'report.json',cpu_affinity='none')
    assert report['runtime']['cpu_affinity']['status']=='unchanged'
    assert report['result']['reason']=='horizon'


def test_invalid_affinity_records_failure_before_domain_work(tmp_path,monkeypatch):
    from tools.measure_run import measure
    monkeypatch.setattr('tools.measure_run.run',lambda *args,**kwargs:pytest.fail('domain must not start'))
    path=tmp_path/'report.json'
    with pytest.raises(ValueError):measure(tmp_path/'config.json',path,cpu_affinity='invalid')
    assert json.loads(path.read_text())['measurement_status']=='failed'


def test_performance_apply_error_is_reported_and_keeps_original_mask(monkeypatch):
    from tools import cpu_affinity
    monkeypatch.setattr(cpu_affinity,'inspect_affinity',lambda policy:{
        'policy':policy,'allowed_cpus':[1,2],'selected_cpus':[1],'effective_cpus':[1,2],'status':'planned'})
    def reject(*args):raise OSError('permission denied')
    monkeypatch.setattr(os,'sched_setaffinity',reject)
    with cpu_affinity.affinity_context('performance') as report:
        assert report['status']=='unavailable' and report['effective_cpus']==[1,2]
        assert 'permission denied' in report['reason']
    with pytest.raises(OSError):
        with cpu_affinity.affinity_context('1'):pytest.fail('explicit request must fail')


@pytest.mark.parametrize('application_mismatch,domain_failure',[(False,False),(False,True),(True,False)])
def test_silent_restoration_mismatch_is_fatal(monkeypatch,application_mismatch,domain_failure):
    from tools import cpu_affinity
    monkeypatch.setattr(cpu_affinity,'inspect_affinity',lambda policy:{
        'policy':policy,'allowed_cpus':[1,2],'selected_cpus':[1],'effective_cpus':[1,2],'status':'planned'})
    masks=iter([{2} if application_mismatch else {1},{1}])
    monkeypatch.setattr(os,'sched_getaffinity',lambda pid:next(masks))
    monkeypatch.setattr(os,'sched_setaffinity',lambda *args:None)
    with pytest.raises(OSError,match='restor'):
        with cpu_affinity.affinity_context('performance'):
            if domain_failure:raise RuntimeError('domain failed')
