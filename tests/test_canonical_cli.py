"""Command-line entry points: run, prepare and generate."""
import json
import os
from pathlib import Path
import subprocess
import sys
from test_runner import fixture


def invoke(*args):
    return subprocess.run([sys.executable,'-m','fab_sim.cli',*map(str,args)],
        env={**os.environ,'PYTHONPATH':str(Path('src').resolve())},capture_output=True,text=True)


def test_canonical_run_uses_config_and_physical_exit(tmp_path):
    result=invoke('run','--config',fixture(tmp_path))
    assert result.returncode==0,result.stderr
    value=json.loads(result.stdout)
    assert value['outcome']=='Completed' and value['exited_count']==2


def test_invalid_run_is_explicitly_rejected(tmp_path):
    path=tmp_path/'bad.json';path.write_text('{}')
    result=invoke('run','--config',path)
    assert result.returncode==2
    assert json.loads(result.stdout)['outcome']=='Rejected'




def test_prepare_and_generate_use_prepared_artifacts_and_exclusive_outputs(tmp_path):
    fixture(tmp_path)
    settings=tmp_path/'settings.json';settings.write_text('{"entry_capacity":3,"exit_capacity":4,"improvement_limit":1}')
    out=tmp_path/'generated.json'
    result=invoke('generate-topology','--manufacturing',tmp_path/'manufacturing.json',
        '--reference',tmp_path/'reference.json','--settings',settings,'--output',out)
    assert result.returncode==0,result.stderr
    before=out.read_bytes()
    assert invoke('generate-topology','--manufacturing',tmp_path/'manufacturing.json',
        '--reference',tmp_path/'reference.json','--settings',settings,'--output',out).returncode==2
    assert out.read_bytes()==before


def test_supplied_workbooks_prepare_through_canonical_cli(tmp_path):
    import pytest
    production=os.environ.get('FAB_HVLM_WORKBOOK');physical=os.environ.get('FAB_SMAT_WORKBOOK')
    if not production or not physical:pytest.skip('supplied Dataset1/physical sources required')
    m=tmp_path/'manufacturing.json';p=tmp_path/'physical.json';profile=tmp_path/'profile.json'
    profile.write_text('{"stocker_capacity":8}')
    result=invoke('prepare-manufacturing','--source',production,'--output',m)
    assert result.returncode==0,result.stderr
    assert [len(r['steps']) for r in json.loads(m.read_text())['model']['routes']]==[583,343]
    result=invoke('prepare-topology','--source',physical,'--manufacturing',m,'--profile',profile,'--output',p)
    assert result.returncode==0,result.stderr
    assert json.loads(p.read_text())['validation']['validated'] is True


def test_malformed_workbook_is_rejected_as_input(tmp_path):
    source=tmp_path/'bad.xlsx';source.write_bytes(b'not a workbook')
    result=invoke('prepare-manufacturing','--source',source,'--output',tmp_path/'out.json')
    assert result.returncode==2
    assert json.loads(result.stdout)['outcome']=='Rejected'
    assert not (tmp_path/'out.json').exists()
