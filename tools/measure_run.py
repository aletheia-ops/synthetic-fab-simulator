"""Run one configuration and write a measurement report (outcome, timings, memory, checkpoints).

Run from the repository root: python -m tools.measure_run --config <run.json>
--report <new-report.json> [--progress] [--resume-from MANIFEST]. The report path must not exist.
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import platform
import sys
import time
from fab_sim.runner import run
from tools.cpu_affinity import affinity_context


def summarize_evidence(directory, expected_sequence, *, base_sequence=0):
    # events.jsonl and oht.jsonl share one sequence; together it is contiguous.
    directory=Path(directory);counts=Counter();records=[];per_stream={}
    for stream_name in ('events','oht'):
        with (directory/(stream_name+'.jsonl')).open(encoding='utf-8') as stream:
            rows=[json.loads(line) for line in stream]
        per_stream[stream_name]=len(rows)
        records.extend((r['event_sequence'],r['simulation_time_us'],r['event_type']) for r in rows)
    records.sort();last_tick=None;burst=maximum=0
    for count,(sequence,tick,kind) in enumerate(records,1):
        if sequence!=base_sequence+count:raise ValueError('noncontiguous committed event sequence')
        if last_tick is not None and tick<last_tick:raise ValueError('event time moved backwards')
        burst=burst+1 if tick==last_tick else 1;maximum=max(maximum,burst);last_tick=tick
        counts[kind]+=1
    count=len(records)
    if base_sequence+count!=expected_sequence:raise ValueError('manifest sequence differs from committed evidence')
    def count_lines(name):
        with (directory/name).open(encoding='utf-8') as stream:return sum(1 for line in stream if line.strip())
    actual={**per_stream,'diagnostics':count_lines('diagnostics.jsonl')}
    manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    expected=manifest.get('record_counts')
    if not isinstance(expected,dict) or set(expected)!=set(actual):
        raise ValueError('manifest required output counts are missing or invalid')
    for kind,total in actual.items():
        if type(expected[kind]) is not int or expected[kind]<0 or total!=expected[kind]:
            raise ValueError(kind+' count differs from manifest required evidence')
    return {'event_count':count,'event_counts':dict(sorted(counts.items())),
        'last_event_time_us':last_tick,'max_same_tick_event_burst':maximum,
        'oht_event_count':actual['oht'],'diagnostic_count':actual['diagnostics'],
        'output_bytes':{name:(directory/name).stat().st_size for name in
                        ('events.jsonl','oht.jsonl','diagnostics.jsonl','manifest.json')}}


def _peak_rss_bytes():
    try:
        import resource
        peak=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak if sys.platform=='darwin' else peak*1024
    except ImportError:return None


def measure(config_path, report_path, *, progress=False, progress_interval=15, cpu_affinity="none", checkpoint_interval_wall_seconds=None, resume_from=None):
    report_path=Path(report_path);report_path.parent.mkdir(parents=True,exist_ok=True)
    # Claim the report before domain work; an existing report never starts a run.
    with report_path.open('x',encoding='utf-8') as report_file:
        wall=time.perf_counter();cpu=time.process_time();affinity=None
        try:
            with affinity_context(cpu_affinity) as affinity:
                result=run(Path(config_path),progress=(lambda record: print(json.dumps(record,sort_keys=True),file=sys.stderr,flush=True))
                    if progress else None,progress_interval=progress_interval,
                    checkpoint_interval_wall_seconds=checkpoint_interval_wall_seconds,resume_from=resume_from)
                elapsed=time.perf_counter()-wall;cpu_elapsed=time.process_time()-cpu
                output=Path(result['resolved_config']['output']['directory'])
                evidence=summarize_evidence(output,result['last_committed_sequence'],base_sequence=result.get('base_sequence',0))
                settlement_ns=result.get('probes',{}).get('wall_ns',{}).get('settlement')
                report={'schema_version':'fab-measurement-v1','measurement_status':'recorded',
                    'config_path':str(Path(config_path).resolve()),'output_directory':str(output),
                    'runtime':{'python':platform.python_version(),'system':platform.system(),
                               'release':platform.release(),'machine':platform.machine(),
                               'processor':platform.processor(),'logical_cpu_count':os.cpu_count(),
                               'cpu_affinity':affinity},
                    'resources':{'run_wall_seconds':elapsed,'run_cpu_seconds':cpu_elapsed,
                        'work_counts':result.get('probes',{}).get('work_counts',{}),
                        'process_peak_rss_bytes':_peak_rss_bytes(),
                        'settlement_wall_seconds':None if settlement_ns is None else settlement_ns/1e9,
                        'outside_settlements_wall_seconds':None if settlement_ns is None else elapsed-settlement_ns/1e9,
                        'committed_events_per_run_wall_second':evidence['event_count']/elapsed},
                    'checkpoint_metrics':result.get('checkpoint_metrics',[]),'checkpoint_restore_metrics':result.get('checkpoint_restore_metrics'),'continuation':result.get('continuation'),
                    'result':{key:result.get(key) for key in ('outcome','reason','domain_complete','constraint_status',
                        'cohort_size','exited_count','wip_count','pending_release_count','pending_admission_count',
                        'now_us','horizon_us','settlements','violation_count','unresolved_count','proofs','batch_policy','equipment_assignment_policy',
                        'domain_digest_version','domain_digest','capacity','release_policy','wait_diagnostics','progress','controller_policy','transport_policy','clearance_pool','scenario','handoff_plans','handoff_waits','clearance_leases','pending_transfer_count','committed_transfer_count','loaded_transfer_count','preflight')},
                    'evidence':evidence,
                    'limitations':['Wall rate includes startup, local export and finalization; it is not paced output rate.',
                        'Settlement timing includes synchronous event export; residual timing is not a startup/drain split.',
                        'RSS is process lifetime high-water and may include earlier work; unavailable on Windows.',
                        'Same-tick burst is simulated-time density, not wall-clock export throughput.']}
                for key in ('release_mode','source_open','exit_targets','product_exit_counts','exit_targets_reached_at_us','objective_achieved'):
                    if key in result:report['result'][key]=result[key]
        except Exception as error:
            json.dump({'schema_version':'fab-measurement-v1','measurement_status':'failed',
                       'error_type':type(error).__name__,'error':str(error),
                       'cpu_affinity':affinity,'cpu_affinity_policy':cpu_affinity},report_file,indent=2)
            report_file.write('\n');raise
        json.dump(report,report_file,indent=2,sort_keys=True);report_file.write('\n')
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True);parser.add_argument('--report',required=True)
    parser.add_argument('--progress',action='store_true');parser.add_argument('--progress-interval',type=float,default=15)
    parser.add_argument('--checkpoint-interval-wall-seconds',type=int)
    parser.add_argument('--resume-from')
    parser.add_argument('--cpu-affinity',default='none',help='none, performance, or logical CPU IDs (2,4-6)')
    args=parser.parse_args(argv);report=measure(args.config,args.report,progress=args.progress,progress_interval=args.progress_interval,cpu_affinity=args.cpu_affinity,checkpoint_interval_wall_seconds=args.checkpoint_interval_wall_seconds,resume_from=args.resume_from)
    print(json.dumps({**report['result'],'report_path':str(Path(args.report).resolve())},sort_keys=True))
    return 1 if report['result']['outcome']=='Failed' else 0


if __name__=='__main__':raise SystemExit(main())
