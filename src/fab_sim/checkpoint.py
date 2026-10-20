"""Wall-time checkpoint policy and strict native continuation compatibility."""
from dataclasses import dataclass
import gc
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import resource
import sys
import time
from .checkpoint_format import CheckpointError, encode_value
from .checkpoint_store import CheckpointWriteError

STATE_CONTRACT='fab-native-checkpoint-archive-v11'
# v10 keeps closed violated clocks live and has no retired_violation_ids;
# upgrade_state() adds the empty index and the first retirement pass archives
# those clocks.
LEGACY_STATE_CONTRACTS=frozenset({'fab-native-checkpoint-archive-v10'})
PHYSICAL_POLICY='native-physical-194aea9-v1'
# Add pairs only after the branch's end-to-end equivalence suite validates them.
VALIDATED_BUILD_PAIRS=frozenset()
# Declared project-policy execution settings (docs/settings.md). With
# allow_policy_change a checkpoint may resume under different values of these
# keys only; inputs, generation, workload, seed and other settings must match.
POLICY_SETTINGS=frozenset({'hop_lookahead_mm','setup_policy','bay_entry_divert','queue_divert','buffer_selection','super_hot_reservation','extra_processing','down_tool_output','congestion_routing_s','congestion_destination_s','dispatch_metering','busy_vehicle_cap','super_hot_expedite','down_tool_avoid_s','replication_salt'})


@dataclass(frozen=True)
class CheckpointSettings:
    enabled: bool
    interval_wall_seconds: int
    directory: Path
    resume_from: Path | None
    allow_compatible_build: bool
    allow_policy_change: bool=False
    background: bool=False
    # Re-read every history chain at restore (audit); not needed to resume.
    verify_history: bool=False

    def __post_init__(self):
        if (type(self.enabled) is not bool or type(self.allow_compatible_build) is not bool
                or type(self.allow_policy_change) is not bool or type(self.background) is not bool
                or type(self.verify_history) is not bool
                or type(self.interval_wall_seconds) is not int or self.interval_wall_seconds<=0):
            raise ValueError('invalid checkpoint settings')


def build_identity():
    digest=hashlib.sha256()
    for path in sorted(Path(__file__).parent.rglob('*.py')):
        digest.update(str(path.relative_to(Path(__file__).parent)).encode());digest.update(b'\0')
        digest.update(path.read_bytes());digest.update(b'\0')
    return digest.hexdigest()


def runtime_compatibility():
    return {'implementation':platform.python_implementation(),'python':list(sys.version_info[:2]),
        'rng':'MT19937-state-v3','rng_policy':'sha256-mt19937-v1'}


def physical_identity(config: dict, input_paths: dict[str, Path]) -> dict[str, object]:
    excluded={'horizon_us','max_settlements','exit_targets','derived_cache_budget_mib'}
    settings={name:config[name] for name in ('generation','workload','injected_faults')}
    settings['execution']={k:v for k,v in config['execution'].items() if k not in excluded}
    return {'policy':PHYSICAL_POLICY,'settings':settings,
        'input_hashes':{name:hashlib.sha256(Path(path).read_bytes()).hexdigest() for name,path in input_paths.items()}}


def _without(identity,keys):
    identity=dict(identity);settings=dict(identity['settings'])
    settings['execution']={k:v for k,v in settings['execution'].items() if k not in keys}
    identity['settings']=settings
    return identity


def _policy_neutral(identity):return _without(identity,POLICY_SETTINGS)


def _peak_rss(who):
    # ru_maxrss is in KiB on Linux (bytes on macOS); peak since process start.
    peak=resource.getrusage(who).ru_maxrss
    return peak if sys.platform=='darwin' else peak*1024


def upgrade_state(bundle):
    """Upgrade a v10 bundle in place: add the (empty) retired-violation index introduced by v11."""
    if bundle.manifest.state_contract in LEGACY_STATE_CONTRACTS:
        runtime=bundle.state.get('runtime')
        if isinstance(runtime,dict):
            rules=runtime.get('owners',{}).get('rules')
            if isinstance(rules,dict) and 'retired_violation_ids' not in rules:
                runtime['nodes'].append(['list',[]])
                rules['retired_violation_ids']=['ref',len(runtime['nodes'])-1]


def validate_resume(bundle, identity: dict[str, object], *, allow_compatible_build: bool,
                    allow_policy_change: bool=False) -> dict[str, object]:
    """Reject incompatible continuations; return the declared relaxations used.

    The state contract and Python/RNG runtime always have to match. A different
    writer build is accepted with allow_compatible_build (the state contract
    versions the checkpoint meaning); changed POLICY_SETTINGS values only with
    allow_policy_change. Both are recorded in continuation evidence.
    """
    manifest=bundle.manifest;relaxed={}
    if manifest.state_contract!=STATE_CONTRACT and manifest.state_contract not in LEGACY_STATE_CONTRACTS:
        raise CheckpointError('checkpoint state contract mismatch')
    if manifest.runtime_compatibility!=runtime_compatibility():raise CheckpointError('checkpoint Python/RNG mismatch')
    saved=manifest.physical_identity
    if encode_value(saved)!=encode_value(identity):
        if (not allow_policy_change or
                encode_value(_policy_neutral(saved))!=encode_value(_policy_neutral(identity))):
            raise CheckpointError('checkpoint physical input/policy mismatch')
        before=saved['settings']['execution'];after=identity['settings']['execution']
        relaxed['policy_changes']={k:{'checkpoint':before.get(k),'resume':after.get(k)}
            for k in sorted(POLICY_SETTINGS) if before.get(k)!=after.get(k)}
    current=build_identity()
    if manifest.writer_build!=current:
        pair=(manifest.writer_build,current)
        if not allow_compatible_build:
            raise CheckpointError('checkpoint build mismatch; set allow_compatible_build to resume across builds')
        if pair not in VALIDATED_BUILD_PAIRS:
            relaxed['cross_build']={'writer_build':manifest.writer_build,'resume_build':current}
    return relaxed


# A forked writer grows to about 1.2x the run's resident memory (Python reference
# counting copies almost every page it reads). Fork only when this much is free;
# otherwise that save runs in the foreground.
WRITER_MEMORY_FACTOR=1.3
WRITER_MEMORY_MARGIN_BYTES=512*1024*1024


def resident_bytes():
    """Current resident memory of this process, or None where /proc is unavailable."""
    try:
        with open('/proc/self/statm') as stream:return int(stream.read().split()[1])*os.sysconf('SC_PAGE_SIZE')
    except (OSError,ValueError,IndexError):return None


def available_memory_bytes():
    """Memory a new process could use now: MemAvailable, capped by the free room of
    every enclosing cgroup-v2 memory limit (systemd MemoryMax, containers). None if unknown."""
    available=None
    try:
        with open('/proc/meminfo') as stream:
            for line in stream:
                if line.startswith('MemAvailable:'):available=int(line.split()[1])*1024;break
    except (OSError,ValueError):pass
    try:
        with open('/proc/self/cgroup') as stream:entry=stream.readline().strip()
        if entry.startswith('0::'):
            root=Path('/sys/fs/cgroup');group=root/entry[3:].lstrip('/')
            while True:
                limit=group/'memory.max'
                if limit.exists() and limit.read_text().strip()!='max':
                    room=int(limit.read_text())-int((group/'memory.current').read_text())
                    available=room if available is None else min(available,room)
                if group==root:break
                group=group.parent
    except (OSError,ValueError):pass
    return available


def writer_memory_check():
    """(fits, needed_bytes, available_bytes) for a forked checkpoint writer. Unknown
    memory (no /proc, e.g. macOS) counts as fitting."""
    rss=resident_bytes();available=available_memory_bytes()
    if rss is None or available is None:return True,None,available
    needed=int(rss*WRITER_MEMORY_FACTOR)+WRITER_MEMORY_MARGIN_BYTES
    return available>=needed,needed,available


class CheckpointCoordinator:
    def __init__(self, settings, store, *, clock):
        self.settings,self.store,self.clock=settings,store,clock
        self.next_deadline=self._wall()+settings.interval_wall_seconds
        self.last_boundary=None;self.last_manifest=None;self.metrics=[]
        # Background mode: (pid, result pipe fd, boundary, saved_tick, settlements, start, stall seconds).
        self.child=None

    def _wall(self):
        value=self.clock()
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
            raise CheckpointWriteError('checkpoint observation clock is invalid')
        return value

    def maybe_save(self,capture,*,boundary,final=False,background=None):
        """Save at a deadline. With settings.background and background=(prepare, build),
        the parent runs prepare() (archive retirement and output watermark) and
        forks; the child runs build(prepared) and the store write, then exits.
        One child at a time; deadlines passing meanwhile are skipped. A child
        failure raises CheckpointWriteError at a later call. A final save waits
        for any child and saves synchronously."""
        if not self.settings.enabled:return None
        if self.child is not None:
            if not self._reap(block=final):return None
        if boundary==self.last_boundary:return None
        if not final and self._wall()<self.next_deadline:return None
        fallback=None
        if (background is not None and self.settings.background and not final
                and hasattr(os,'fork')):
            fits,needed,available=writer_memory_check()
            if fits:return self._fork(background,boundary)
            fallback={'reason':'insufficient_memory','needed_bytes':needed,'available_bytes':available}
        start=time.perf_counter();cpu=time.process_time()
        try:
            # capture() allocates tens of millions of containers; with the
            # collector on, generation passes rescan them repeatedly (about 4x
            # slower capture). Collection timing cannot change the captured state.
            collecting=gc.isenabled();gc.disable()
            try:bundle=capture()
            finally:
                if collecting:gc.enable()
            capture_end=time.perf_counter()
            path=self.store.write(bundle)
        except CheckpointWriteError:raise
        except (OSError,CheckpointError) as error:
            raise CheckpointWriteError('checkpoint capture failed: '+str(error)) from error
        self.last_boundary=boundary;self.last_manifest=path
        self.next_deadline=self._wall()+self.settings.interval_wall_seconds
        self.metrics.append({'saved_tick':bundle.manifest.saved_tick,'settlements':bundle.manifest.settlements,
            'manifest_path':str(path),'capture_wall_seconds':capture_end-start,
            'save_wall_seconds':time.perf_counter()-start,'save_cpu_seconds':time.process_time()-cpu,
            'payload_bytes':(path.parent/'state.json').stat().st_size,'peak_rss_bytes_so_far':_peak_rss(resource.RUSAGE_SELF),
            'background':False,**({'background_fallback':fallback} if fallback else {})})
        del self.metrics[:-32]
        return path

    def _fork(self,background,boundary):
        prepare,build=background
        start=time.perf_counter()
        try:prepared=prepare()
        except CheckpointWriteError:raise
        except (OSError,CheckpointError) as error:
            raise CheckpointWriteError('checkpoint capture failed: '+str(error)) from error
        read_fd,write_fd=os.pipe()
        pid=os.fork()
        if pid==0:
            # Child: no garbage collection (it would dirty shared pages), no
            # inherited cleanup (os._exit skips atexit, buffered files and
            # finalizers of the parent's open handles).
            status=1
            try:
                gc.disable();os.close(read_fd)
                bundle=build(prepared)
                child_start=time.perf_counter()
                path=self.store.write(bundle)
                result={'ok':True,'path':str(path),'saved_tick':bundle.manifest.saved_tick,
                    'settlements':bundle.manifest.settlements,'write_wall_seconds':time.perf_counter()-child_start,
                    'payload_bytes':(path.parent/'state.json').stat().st_size}
                status=0
            except BaseException as error:
                result={'ok':False,'error':type(error).__name__+': '+str(error)}
            try:
                os.write(write_fd,json.dumps(result).encode());os.close(write_fd)
            finally:
                os._exit(status)
        os.close(write_fd)
        self.child=(pid,read_fd,boundary,start,time.perf_counter()-start)
        self.last_boundary=boundary
        return None

    def _reap(self,*,block):
        pid,read_fd,boundary,start,stall=self.child
        done,status=os.waitpid(pid,0 if block else os.WNOHANG)
        if done==0:return False
        self.child=None
        try:
            chunks=[]
            while True:
                chunk=os.read(read_fd,65536)
                if not chunk:break
                chunks.append(chunk)
        finally:os.close(read_fd)
        try:result=json.loads(b''.join(chunks) or b'{}')
        except ValueError:result={}
        if not result.get('ok') or os.waitstatus_to_exitcode(status)!=0:
            raise CheckpointWriteError('background checkpoint failed: '+str(result.get('error')
                or 'child exit status '+str(os.waitstatus_to_exitcode(status))))
        path=Path(result['path']);self.last_manifest=path
        self.next_deadline=self._wall()+self.settings.interval_wall_seconds
        self.metrics.append({'saved_tick':result['saved_tick'],'settlements':result['settlements'],
            'manifest_path':str(path),'background':True,'parent_stall_wall_seconds':stall,
            'save_wall_seconds':time.perf_counter()-start,'write_wall_seconds':result['write_wall_seconds'],
            'payload_bytes':result['payload_bytes'],'peak_rss_bytes_so_far':_peak_rss(resource.RUSAGE_SELF),
            'writer_peak_rss_bytes_so_far':_peak_rss(resource.RUSAGE_CHILDREN)})
        del self.metrics[:-32]
        return True
