"""Check that checkpoint resume reproduces an uninterrupted run: run a configuration (at most 1,000
settlements) once straight through and once split at the given settlements, and compare the outputs.
Writes into a new directory only."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import time
from fab_sim.runner import load_config,run
from fab_sim.inputs import canonical_text
from fab_sim.checkpoint_store import CheckpointStore
from tools.measure_run import _peak_rss_bytes


def _latest(directory):
    directory=Path(directory)
    return directory/json.loads((directory/'latest.json').read_text())['manifest']


def _records(path):
    with Path(path).open() as stream:
        return [{key:value for key,value in json.loads(line).items() if key not in ('run_id','event_id')}
            for line in stream if line.strip()]


def verify_checkpoint(config_path: Path, output_root: Path, *, split_settlements: tuple[int,...]) -> dict[str,object]:
    config=load_config(config_path)
    if (not split_settlements or any(type(n) is not int or n<=0 for n in split_settlements)
            or tuple(sorted(set(split_settlements)))!=split_settlements):
        raise ValueError('split settlements must be strictly increasing positive integers')
    if config['execution'].get('max_settlements',1000)>1000:
        raise ValueError('checkpoint verification requires a limit of at most 1000 settlements')
    config['execution'].setdefault('max_settlements',1000)
    config['output']['live']=False;config['output']['publication']={};config['replay']['rate']=None
    root=Path(output_root).resolve();root.mkdir(parents=True,exist_ok=False)
    metrics=[];timings=[]
    def execute(name,limit,resume):
        candidate=deepcopy(config);candidate['output'].update(directory=str(root/name),run_id=name)
        candidate['execution']['max_settlements']=limit
        candidate['checkpoint']={'enabled':True,'interval_wall_seconds':300,'resume_from':str(resume) if resume else None}
        path=root/(name+'.json');path.write_text(canonical_text(candidate))
        wall=time.perf_counter();cpu=time.process_time();result=run(path)
        timings.append({'segment':name,'wall_seconds':time.perf_counter()-wall,'cpu_seconds':time.process_time()-cpu,
            'restore':result.get('checkpoint_restore_metrics')})
        if result['outcome']=='Failed':raise RuntimeError(str(result.get('failure')))
        metrics.extend(result.get('checkpoint_metrics',[]))
        return result,_latest(root/name/'checkpoints')
    baseline,baseline_manifest=execute('uninterrupted',config['execution']['max_settlements'],None)
    if split_settlements[-1]>=baseline['settlements']:
        raise ValueError('split must precede the baseline settled stop')
    resume=None;segments=[]
    for index,limit in enumerate((*split_settlements,config['execution']['max_settlements'])):
        name='segment-'+str(index+1);result,resume=execute(name,limit,resume);segments.append(name)
    combined=lambda filename:sum((_records(root/name/filename) for name in segments),[])
    store=CheckpointStore(root)
    checks={'events':combined('events.jsonl')==_records(root/'uninterrupted/events.jsonl'),
        'diagnostics':combined('diagnostics.jsonl')==_records(root/'uninterrupted/diagnostics.jsonl'),
        'oht':combined('oht.jsonl')==_records(root/'uninterrupted/oht.jsonl'),
        'physical_digest':result['domain_digest']==baseline['domain_digest'],
        'rng':store.read(resume).state['runtime']['streams']==store.read(baseline_manifest).state['runtime']['streams'],
        'first_exits':result['progress']['first_exit_at_us']==baseline['progress']['first_exit_at_us'],
        'settlements':result['settlements']==baseline['settlements']}
    report={'equivalent':all(checks.values()),'checks':checks,'split_settlements':split_settlements,
        'checkpoint_metrics':metrics,'segment_timings':timings,'process_peak_rss_bytes':_peak_rss_bytes(),
        'final_tick':result['now_us'],'final_settlements':result['settlements'],
        'limitations':['Bounded acceptance timing is not steady-state performance.',
            'RSS is this verification process lifetime high-water, including prior segments.']}
    (root/'verification.json').write_text(json.dumps(report,indent=2)+'\n')
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True);parser.add_argument('--output-root',required=True)
    parser.add_argument('--split-settlements',default='20,40')
    args=parser.parse_args(argv)
    report=verify_checkpoint(Path(args.config),Path(args.output_root),
        split_settlements=tuple(int(value) for value in args.split_settlements.split(',')))
    print(json.dumps(report,indent=2));return 0 if report['equivalent'] else 1


if __name__=='__main__':raise SystemExit(main())
