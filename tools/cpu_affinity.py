"""CPU placement for a run (`measure_run --cpu-affinity`). Run directly, it prints the plan it would
apply without pinning anything."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import platform
import re

HYBRID_CPUS=Path('/sys/bus/event_source/devices/cpu_core/cpus')


def _cpu_list(value):
    cpus=set()
    for part in value.strip().split(','):
        if not re.fullmatch(r'[0-9]+(?:-[0-9]+)?',part):
            raise ValueError('CPU list must contain IDs or inclusive ranges, e.g. 2,4-6')
        ends=[int(item) for item in part.split('-')]
        first,last=ends[0],ends[-1]
        if last<first or last>1_000_000:raise ValueError('invalid CPU range')
        cpus.update(range(first,last+1))
    return cpus


def _allowed_cpus():
    try:return os.sched_getaffinity(0)
    except (AttributeError,OSError):return None


def plan_affinity(policy, *, allowed, release, metadata=HYBRID_CPUS):
    """Return JSON-safe selection using caller-supplied OS facts; do not mutate."""
    guest=any(word in release.lower() for word in ('microsoft','wsl'))
    report={'policy':policy,'scope':'guest_vcpu' if guest else 'os_logical_cpu',
            'allowed_cpus':None if allowed is None else sorted(allowed),
            'selected_cpus':None,'effective_cpus':None if allowed is None else sorted(allowed),
            'status':'unchanged','reason':None,'detection_source':None}
    if policy=='none':return report
    if policy!='performance':
        selected=_cpu_list(policy)
        if allowed is None:raise ValueError('explicit CPU affinity is unavailable on this OS')
        if not selected<=set(allowed):raise ValueError('requested CPUs are outside the allowed affinity mask')
    else:
        report['status']='unavailable'
        if guest:
            report['reason']='WSL guest CPU IDs do not establish host P-core identity'
            return report
        if allowed is None:
            report['reason']='OS affinity API is unavailable'
            return report
        try:selected=_cpu_list(Path(metadata).read_text(encoding='ascii'))&set(allowed)
        except (OSError,ValueError):
            report['reason']='Intel hybrid core metadata is missing or invalid'
            return report
        if not selected:
            report['reason']='No reported performance CPUs are in the allowed affinity mask'
            return report
        report['detection_source']=str(metadata)
    report.update(status='planned',selected_cpus=sorted(selected))
    return report


def inspect_affinity(policy='none'):
    return plan_affinity(policy,allowed=_allowed_cpus(),release=platform.release())


def _restore_affinity(original):
    os.sched_setaffinity(0,set(original))
    if sorted(os.sched_getaffinity(0))!=original:
        raise OSError('OS affinity restoration readback differs from original mask')


@contextmanager
def affinity_context(policy='none'):
    """Pin the calling Linux thread (the serial simulator), then restore its mask."""
    report=inspect_affinity(policy)
    original=report['allowed_cpus'];selected=report['selected_cpus']
    changed=False
    try:
        if selected is not None:
            try:
                os.sched_setaffinity(0,set(selected))
                changed=True
                effective=sorted(os.sched_getaffinity(0))
                if effective!=selected:raise OSError('OS affinity readback differs from selection')
                report.update(status='applied',effective_cpus=effective)
            except OSError as error:
                # Restoration failures are fatal: never silently leave an unknown mask.
                if changed:
                    changed=False
                    _restore_affinity(original)
                if policy!='performance':raise
                report.update(status='unavailable',reason=str(error),effective_cpus=original)
        yield report
    finally:
        if changed:_restore_affinity(original)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy',default='none',help='none, performance, or logical CPU IDs (2,4-6)')
    args=parser.parse_args(argv)
    try:report=inspect_affinity(args.policy)
    except ValueError as error:parser.error(str(error))
    print(json.dumps(report,sort_keys=True))
    return 0


if __name__=='__main__':raise SystemExit(main())
