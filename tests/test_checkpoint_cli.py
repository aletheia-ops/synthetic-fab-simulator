import json
import subprocess
import sys
from test_runner import fixture


def test_cli_checkpoint_interval_enables_saving(tmp_path):
    path=fixture(tmp_path)
    result=subprocess.run([sys.executable,'-m','fab_sim.cli','run','--config',str(path),
        '--checkpoint-interval-wall-seconds','9'],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert (tmp_path/'result/checkpoints/latest.json').exists()


def test_measurement_cli_resumes_with_interval_override(tmp_path):
    path=fixture(tmp_path,checkpoint={'enabled':True},execution={'horizon_us':1000000000,'max_settlements':10})
    result=subprocess.run([sys.executable,'-m','tools.measure_run','--config',str(path),
        '--report',str(tmp_path/'parent-report.json')],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    checkpoint_dir=tmp_path/'result/checkpoints'
    manifest=checkpoint_dir/json.loads((checkpoint_dir/'latest.json').read_text())['manifest']
    config=json.loads(path.read_text());config['output']['directory']='child'
    del config['execution']['max_settlements'];config['checkpoint']={};path.write_text(json.dumps(config))
    result=subprocess.run([sys.executable,'-m','tools.measure_run','--config',str(path),
        '--report',str(tmp_path/'child-report.json'),'--resume-from',str(manifest),
        '--checkpoint-interval-wall-seconds','9'],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    report=json.loads((tmp_path/'child-report.json').read_text())
    assert report['result']['outcome']=='Completed'
    assert report['continuation']['parent_checkpoint']==str(manifest)
    assert report['evidence']['event_count']>0
    assert (tmp_path/'child/checkpoints/latest.json').exists()
