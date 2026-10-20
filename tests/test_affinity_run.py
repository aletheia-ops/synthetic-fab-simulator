"""Affinity changes execution placement, not the physical trajectory."""
import os
import pytest
from test_runner import fixture


@pytest.mark.skipif(not hasattr(os,'sched_setaffinity'),reason='Linux affinity required')
def test_pinned_run_has_identical_physical_evidence_and_restores_affinity(tmp_path):
    from tools.measure_run import measure
    original=os.sched_getaffinity(0)
    results=[]
    for name,policy in [('default','none'),('pinned',str(min(original)))]:
        directory=tmp_path/name;directory.mkdir()
        report=measure(fixture(directory),directory/'measurement.json',cpu_affinity=policy)
        assert report['result']['outcome']=='Completed' and report['result']['exited_count']==2
        assert os.sched_getaffinity(0)==original
        results.append(report)
    assert results[0]['result']['domain_digest']==results[1]['result']['domain_digest']
    for name in ('events.jsonl','oht.jsonl','diagnostics.jsonl'):
        assert (tmp_path/'default'/'result'/name).read_bytes()==(tmp_path/'pinned'/'result'/name).read_bytes()
