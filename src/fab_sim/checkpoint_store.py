"""Atomic checkpoint bundles; an incomplete bundle is never a resume source."""
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import uuid
from .checkpoint_format import CheckpointBundle, CheckpointManifest, CheckpointError, encode_value, decode_value, decode_consuming, decode_state_payload, write_encoded_value


class CheckpointWriteError(CheckpointError):
    pass


def drop_cached_pages(fd):
    """Advise the kernel to drop this file's clean cached pages (no effect on content).

    Checkpoints and logs are written once and read again only on a later resume or
    audit; without this, their pages stay in the page cache, which a WSL2 VM keeps
    (and the host counts) up to its memory limit. Call after fsync, when every page
    is clean. Best effort: platforms without posix_fadvise ignore it."""
    if hasattr(os, 'posix_fadvise'):
        try: os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except OSError: pass


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


DAY_US=86_400_000_000
# retain_daily keeps one bundle per simulated day for this many recent days, then one per week.
DAILY_DAYS=14


DAY_US = 86_400_000_000
# Bundles are named by simulated day (day-060.40-<id>); older runs used checkpoint-<tick>-<id>.
BUNDLE_PREFIXES = ('day-', 'checkpoint-')


def bundle_name(tick):
    """day-DDD.DD of a saved tick (hundredths of a simulated day, rounded down)."""
    return f'day-{tick // DAY_US:03d}.{tick % DAY_US * 100 // DAY_US:02d}'


def bundle_directories(directory):
    return [d for prefix in BUNDLE_PREFIXES for d in Path(directory).glob(prefix + '*')]


class CheckpointStore:
    def __init__(self, directory: Path, *, retain_completed=3, retain_daily=False):
        if type(retain_completed) is not int or retain_completed<1:
            raise ValueError('positive checkpoint retention required')
        if type(retain_daily) is not bool:raise ValueError('retain_daily must be boolean')
        self.directory = Path(directory)
        self.retain_completed=retain_completed
        # Also keep the first verified bundle of every simulated day (of every
        # simulated week once older than DAILY_DAYS), so a run that degrades
        # (e.g. a gridlock) always leaves an earlier clean state.
        self.retain_daily=retain_daily
        # Bundles already verified by this process: name -> (size, mtime_ns).
        # Every save prunes; re-hashing every retained bundle each time made the
        # cost grow with the number of daily keepers.
        self._verified={}

    def _prune(self, current, manifest):
        # Only recognized complete bundles from this attempt are eligible.
        # Archive segments (when present) are independent, never pruned here.
        candidates=[]
        for directory in bundle_directories(self.directory):
            if directory.is_symlink() or not directory.is_dir():continue
            try:
                fields=json.loads((directory/'manifest.json').read_bytes())
                if (fields['parent_run_id']!=manifest.parent_run_id or
                    fields['writer_build']!=manifest.writer_build or
                    set(fields['payload_checksums'])!={'state.json'} or
                    set(x.name for x in directory.iterdir())!={'state.json','manifest.json'}):continue
                # A malformed bundle cannot count toward successful retention.
                stat=(directory/'state.json').stat();seen=(stat.st_size,stat.st_mtime_ns)
                if self._verified.get(directory.name)!=seen:
                    checksum=hashlib.sha256()
                    with (directory/'state.json').open('rb') as stream:
                        for chunk in iter(lambda:stream.read(1024*1024),b''):checksum.update(chunk)
                        drop_cached_pages(stream.fileno())
                    if checksum.hexdigest()!=fields['payload_checksums']['state.json']:continue
                    self._verified[directory.name]=seen
                progress=tuple(fields[k] for k in ('settlements','saved_tick','last_committed_sequence'))
                if any(type(v) is not int or v<0 for v in progress):continue
                # Filesystem wall time may move backwards. Same-attempt
                # simulation counters define which verified bundles are newer.
                candidates.append((progress,directory.stat().st_mtime_ns,directory.name,directory))
            except (OSError,ValueError,TypeError,KeyError):continue
        previous=[x for x in sorted(candidates,reverse=True) if x[3]!=current]
        daily=set()
        if self.retain_daily and candidates:
            newest=max(item[0][1] for item in candidates)//DAY_US
            first={}
            for item in sorted(candidates):
                day=item[0][1]//DAY_US
                first.setdefault(('day',day) if newest-day<DAILY_DAYS else ('week',day//7),item[3])
            daily=set(first.values())
        for _,_,_,directory in previous[max(0,self.retain_completed-1):]:
            if directory in daily:continue
            # Exact known files only; never recursively delete a computed tree.
            (directory/'state.json').unlink()
            (directory/'manifest.json').unlink()
            directory.rmdir()
            self._verified.pop(directory.name,None)
        _sync_directory(self.directory)

    def _write_file(self, path, data):
        with path.open('xb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())

    def write(self, bundle: CheckpointBundle) -> Path:
        token = uuid.uuid4().hex
        temporary = self.directory / ('.pending-' + token)
        final = self.directory / (bundle_name(bundle.manifest.saved_tick) + '-' + token[:8])
        pointer = self.directory / ('.latest-' + token)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary.mkdir()
            # Stream the payload: no tagged tree, JSON text or byte copy of the
            # whole state in memory (the bytes equal encode_value(bundle.state)).
            digest = hashlib.sha256()
            with (temporary / 'state.json').open('xb') as stream:
                def write(chunk):
                    digest.update(chunk); stream.write(chunk)
                write_encoded_value(bundle.state, write)
                stream.flush(); os.fsync(stream.fileno()); drop_cached_pages(stream.fileno())
            manifest = replace(bundle.manifest, payload_checksums={'state.json': digest.hexdigest()})
            fields=asdict(manifest)
            fields['physical_identity']=json.loads(encode_value(manifest.physical_identity))
            metadata = json.dumps(fields, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
            self._write_file(temporary / 'manifest.json', metadata)
            _sync_directory(temporary)
            os.replace(temporary, final)
            _sync_directory(self.directory)
            self._write_file(pointer, json.dumps({'manifest': str((final / 'manifest.json').relative_to(self.directory))}).encode())
            os.replace(pointer, self.directory / 'latest.json')
            _sync_directory(self.directory)
            # The payload was hashed while it was written.
            stat=(final/'state.json').stat();self._verified[final.name]=(stat.st_size,stat.st_mtime_ns)
            # Retention starts only after durable latest publication. Failure
            # leaves extra bundles, never invalidates a successfully saved one.
            try:self._prune(final,manifest)
            except OSError:pass
            return final / 'manifest.json'
        except (OSError, ValueError, TypeError, CheckpointError) as error:
            raise CheckpointWriteError('checkpoint write failed: ' + str(error)) from error
        finally:
            # Completed bundles remain available even if the latest pointer failed.
            pointer.unlink(missing_ok=True)

    def read(self, manifest_path: Path) -> CheckpointBundle:
        path = Path(manifest_path)
        try:
            if path.name != 'manifest.json' or not path.parent.name.startswith(BUNDLE_PREFIXES):
                raise ValueError('not a completed checkpoint bundle')
            fields=json.loads(path.read_bytes())
            fields['physical_identity']=decode_value(json.dumps(fields['physical_identity']).encode())
            manifest = CheckpointManifest(**fields)
            if type(manifest.format_version) is not int or manifest.format_version != 1:
                raise ValueError('unsupported checkpoint format')
            if any(type(v) is not int or v < 0 for v in (manifest.saved_tick, manifest.settlements, manifest.last_committed_sequence)):
                raise ValueError('invalid checkpoint counters')
            if (not isinstance(manifest.physical_identity,dict) or not isinstance(manifest.runtime_compatibility,dict)
                    or any(not isinstance(v,str) or not v for v in (manifest.state_contract,manifest.writer_build,manifest.parent_run_id,manifest.physical_digest))
                    or manifest.parent_checkpoint is not None and not isinstance(manifest.parent_checkpoint,str)):
                raise ValueError('invalid checkpoint manifest identities')
            if set(manifest.payload_checksums) != {'state.json'}: raise ValueError('invalid checkpoint payload inventory')
            payload = (path.parent / 'state.json').read_bytes()
            if hashlib.sha256(payload).hexdigest() != manifest.payload_checksums['state.json']:
                raise ValueError('checkpoint checksum mismatch')
            state = decode_state_payload(payload); del payload
            if type(state) is not dict: raise ValueError('invalid runtime state')
            return CheckpointBundle(manifest, state)
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise CheckpointError('checkpoint read failed: ' + str(error)) from error
