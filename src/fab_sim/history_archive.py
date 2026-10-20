"""Immutable bounded archive segments; a checkpoint names an exact chain head."""
import bisect
import hashlib
import os
from pathlib import Path
import uuid
import json
import re
import zlib
from fab_sim.checkpoint_store import drop_cached_pages
from fab_sim.checkpoint_format import CheckpointError, encode_value, decode_value, decode_tagged, tagged_value, _json


def _segment_payload(data):
    """encode_value(data) for an archive segment, encoding each record once.

    A JSON list serializes as '[' + ','.join(items) + ']', so the segment is
    assembled from the per-record encodings, which the logical digest reuses.
    Returns (payload, [encode_value(record) for each record]).
    """
    encoded=[_json(tagged_value(record)) for record in data['records']]
    head=tagged_value({k:v for k,v in data.items() if k!='records'})
    pairs=[(pair[0][1],_json(pair)) for pair in head[1]]
    pairs.append(('records',b'[["str","records"],["list",['+b','.join(encoded)+b']]]'))
    # Same order encode_value gives plain string keys.
    pairs.sort(key=lambda pair:pair[0]+'"')
    return b'["dict",['+b','.join(part for _,part in pairs)+b']]',encoded


# Canonical version-1 segment framing (keys sorted; records last but 'version').
_SEGMENT_HEAD=re.compile(rb'\["dict",\[\[\["str","previous_head"\],(?:\["none"\]|\["str","([0-9a-f]{64})"\])\],'
    rb'\[\["str","previous_records"\],\["int","(0|[1-9][0-9]*)"\]\],'
    rb'\[\["str","previous_segments"\],\["int","(0|[1-9][0-9]*)"\]\],\[\["str","records"\],\["list",\[')
_SEGMENT_TAIL=b']]],[["str","version"],["int","1"]]]]'
# Start of one archived record: {'id':..., 'kind':..., 'record': {...}} in canonical order.
_RECORD_START=re.compile(rb'\["dict",\[\[\["str","id"\],\["str","((?:[^"\\]|\\.)+)"\]\],'
    rb'\[\["str","kind"\],\["str","((?:[^"\\]|\\.)+)"\]\],\[\["str","record"\],\["dict",')


def _tagged_fields(tree):
    """{key: tagged value} of a tagged dict with plain string keys, else None."""
    if type(tree) is not list or len(tree)!=2 or tree[0]!='dict' or type(tree[1]) is not list:return None
    fields={}
    for pair in tree[1]:
        if (type(pair) is not list or len(pair)!=2 or type(pair[0]) is not list or len(pair[0])!=2
                or pair[0][0]!='str' or type(pair[0][1]) is not str or pair[0][1] in fields):return None
        fields[pair[0][1]]=pair[1]
    return fields


def _ready(archive,count):
    """Whether a retirement pass publishes `count` eligible records now: a full
    segment, or anything when the archive is flushing (the default; a running
    simulation turns flushing off between checkpoints so small owners batch up
    instead of writing one tiny segment per pass)."""
    return count>0 and (count>=archive.max_records or archive.flush)


class HistoryArchive:
    # Segments are stored zlib-compressed under a two-hex-digit shard directory
    # (<directory>/<id[:2]>/<id>.jsonz). The identity is the SHA-256 of the
    # uncompressed canonical payload. Older archives (flat <id>.json) stay readable.
    SUFFIX='.jsonz'

    def __init__(self,directory,*,max_records=64,max_bytes=1024*1024,deferred_sync=False):
        if any(type(v) is not int or v<1 for v in (max_records,max_bytes)):
            raise ValueError('positive archive batch bounds required')
        self.directory=Path(directory).resolve()
        self.max_records=max_records;self.max_bytes=max_bytes
        # With deferred_sync the owner must call sync() before any durable
        # reference (checkpoint, final manifest) names a head appended here.
        self.deferred_sync=deferred_sync;self._unsynced=[]
        self.flush=True

    def _store_path(self,identity,directory=None):
        return (directory or self.directory)/identity[:2]/(identity+self.SUFFIX)

    def _read(self,path):
        """Uncompressed payload of a stored segment (bounded by max_bytes)."""
        if path.is_symlink() or path.stat().st_size>self.max_bytes:raise CheckpointError('invalid archive segment')
        data=path.read_bytes()
        if path.suffix!=self.SUFFIX:return data
        inflater=zlib.decompressobj()
        try:payload=inflater.decompress(data,self.max_bytes+1)
        except zlib.error as error:raise CheckpointError('invalid archive segment compression') from error
        if len(payload)>self.max_bytes or inflater.unconsumed_tail or not inflater.eof:
            raise CheckpointError('invalid archive segment')
        return payload

    def append(self,head,records):
        if type(records) is not list or not 0<len(records)<=self.max_records:
            raise ValueError('invalid archive batch size')
        for record in records:
            if (type(record) is not dict or set(record)!={'kind','id','record'}
                    or any(type(record[k]) is not str or not record[k] for k in ('kind','id'))
                    or type(record['record']) is not dict):raise ValueError('invalid archive record')
        if len({(r['kind'],r['id']) for r in records})!=len(records):raise ValueError('duplicate archive batch identity')
        if head is not None:
            self._head(head,parent=True)
            if head['directory']!=str(self.directory):self._record_parent(head['directory'])
        data={'version':1,'previous_head':head['head'] if head else None,
            'previous_records':head['records'] if head else 0,
            'previous_segments':head['segments'] if head else 0,'records':records}
        payload,encoded=_segment_payload(data)
        if len(payload)>self.max_bytes:raise ValueError('archive batch exceeds byte bound')
        digest=hashlib.sha256(payload).hexdigest()
        target=self._store_path(digest)
        fresh_shard=not target.parent.exists()
        target.parent.mkdir(parents=True,exist_ok=True)
        temporary=target.parent/('.pending-'+uuid.uuid4().hex)
        try:
            with temporary.open('xb') as stream:
                stream.write(zlib.compress(payload,1));stream.flush()
                if not self.deferred_sync:os.fsync(stream.fileno());drop_cached_pages(stream.fileno())
            # Exclusive publication never overwrites another branch's segment.
            try:os.link(temporary,target)
            except FileExistsError:
                if self._read(target)!=payload:raise CheckpointError('archive content collision/corruption')
            if self.deferred_sync:self._unsynced.append(target)
            else:
                self._sync_directory(target.parent)
                if fresh_shard:self._sync_directory()
        finally:temporary.unlink(missing_ok=True)
        logical=int(head['logical_digest'],16) if head else 0
        for record_bytes in encoded:logical ^= int.from_bytes(hashlib.sha256(record_bytes).digest(),'big')
        return {'schema':'fab-history-archive-v1','directory':str(self.directory),'head':digest,
            'records':data['previous_records']+len(records),'segments':data['previous_segments']+1,
            'logical_digest':format(logical,'064x')}

    def append_all(self,head,records):
        """Append records as one or more segments within both bounds (a batch of
        large records is split in halves until each segment fits)."""
        for begin in range(0,len(records),self.max_records):
            head=self._append_fitting(head,records[begin:begin+self.max_records])
        return head

    def _append_fitting(self,head,records):
        try:return self.append(head,records)
        except ValueError as error:
            if 'byte bound' not in str(error) or len(records)<2:raise
        middle=len(records)//2
        return self._append_fitting(self._append_fitting(head,records[:middle]),records[middle:])

    def _record_parent(self,directory):
        """Note the directory holding this chain's older segments (parents.json);
        readers follow it when present. Restore and simulation never need it."""
        self.directory.mkdir(parents=True,exist_ok=True)
        path=self.directory/'parents.json'
        parents=json.loads(path.read_text()) if path.exists() else []
        if directory in parents:return
        temporary=self.directory/('.parents-'+uuid.uuid4().hex)
        temporary.write_text(json.dumps(parents+[directory]));os.replace(temporary,path)

    def _find(self,identity,directory):
        for candidate in (self._store_path(identity,directory),directory/(identity+'.json')):
            if candidate.exists():return candidate
        return None

    def _segment_path(self,identity):
        """This directory, then ancestors named by parents.json (transitively)."""
        found=self._find(identity,self.directory)
        if found:return found
        seen={str(self.directory)};pending=[self.directory]
        while pending:
            listing=pending.pop(0)/'parents.json'
            try:parents=json.loads(listing.read_text()) if listing.exists() else []
            except (OSError,ValueError):parents=[]
            for parent in parents:
                if parent in seen:continue
                seen.add(parent);found=self._find(identity,Path(parent))
                if found:return found
                pending.append(Path(parent))
        return self._store_path(identity)

    def _sync_directory(self,directory=None):
        fd=os.open(directory or self.directory,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)

    def sync(self):
        """Make every deferred segment and its directory entry durable."""
        if not self._unsynced:return
        # One filesystem-wide flush instead of an fsync per segment file (tens of
        # thousands per checkpoint); then fsync the touched directories.
        os.sync()
        for directory in {path.parent for path in self._unsynced}:self._sync_directory(directory)
        self._sync_directory()
        for path in self._unsynced:
            # Segments are written once and read only by audits, restores with
            # verify_history and offline readers; release their cached pages.
            try:fd=os.open(path,os.O_RDONLY)
            except OSError:continue
            try:drop_cached_pages(fd)
            finally:os.close(fd)
        self._unsynced.clear()

    def _head(self,head,*,parent=False):
        # A parent head (append) may live in an earlier attempt's directory.
        if (type(head) is not dict or set(head)!={'schema','directory','head','records','segments','logical_digest'}
                or head['schema']!='fab-history-archive-v1' or not parent and head['directory']!=str(self.directory)
                or type(head['directory']) is not str
                or type(head['head']) is not str or len(head['head'])!=64
                or any(c not in '0123456789abcdef' for c in head['head'])
                or type(head['logical_digest']) is not str or len(head['logical_digest'])!=64
                or any(c not in '0123456789abcdef' for c in head['logical_digest'])
                or any(type(head[k]) is not int or head[k]<1 for k in ('records','segments'))):
            raise CheckpointError('invalid archive head')

    def scan(self,head):
        """Newest segments first: (kind, id, tagged record) without decoding records.

        Same chain checks as records(): segment checksum, inventory, linkage,
        record shape and the logical digest, computed from each record's
        canonical tagged encoding (encode_value(record) == _json(tagged)).
        Keeps only one bounded segment in memory.
        """
        if head is None:return
        self._head(head)
        identity=head['head'];remaining=head['records'];segments=head['segments'];logical=0
        while identity is not None:
            if segments<1 or type(identity) is not str or len(identity)!=64 or any(c not in '0123456789abcdef' for c in identity):
                raise CheckpointError('invalid archive chain')
            path=self._segment_path(identity)
            try:
                payload=self._read(path)
                if hashlib.sha256(payload).hexdigest()!=identity:raise CheckpointError('archive checksum mismatch')
                try:tree=json.loads(payload)
                except ValueError as error:raise CheckpointError('invalid archive segment encoding') from error
                fields=_tagged_fields(tree)
                if fields is None or set(fields)!={'version','previous_head','previous_records','previous_segments','records'}:
                    raise CheckpointError('invalid archive segment inventory')
                scalars={k:decode_tagged(fields[k]) for k in ('version','previous_head','previous_records','previous_segments')}
                items=fields['records']
                if (type(items) is not list or len(items)!=2 or items[0]!='list' or type(items[1]) is not list
                        or scalars['version']!=1 or not 0<len(items[1])<=self.max_records
                        or type(scalars['previous_records']) is not int or type(scalars['previous_segments']) is not int
                        or scalars['previous_head'] is not None and type(scalars['previous_head']) is not str
                        or scalars['previous_records']!=remaining-len(items[1])
                        or scalars['previous_segments']!=segments-1):raise CheckpointError('invalid archive segment inventory')
                for item in items[1]:
                    record=_tagged_fields(item)
                    if (record is None or set(record)!={'kind','id','record'}
                            or any(type(record[k]) is not list or len(record[k])!=2 or record[k][0]!='str'
                                   or type(record[k][1]) is not str or not record[k][1] for k in ('kind','id'))
                            or type(record['record']) is not list or len(record['record'])!=2 or record['record'][0]!='dict'):
                        raise CheckpointError('invalid archived record')
                    logical ^= int.from_bytes(hashlib.sha256(_json(item)).digest(),'big')
                    yield record['kind'][1],record['id'][1],record['record']
                identity=scalars['previous_head'];remaining=scalars['previous_records'];segments-=1
            except OSError as error:raise CheckpointError('required history archive is unavailable') from error
        if remaining or segments:raise CheckpointError('incomplete archive chain')
        if format(logical,'064x')!=head['logical_digest']:raise CheckpointError('archive logical digest mismatch')

    def identities(self,head):
        """Newest segments first: (kind, id) of every record, checksum-verified.

        Every segment's SHA-256 pins its bytes (the head is named by the
        checkpoint, each segment names its predecessor), so identities are read
        from the canonical framing without parsing records. A segment whose
        framing or record count does not match exactly is parsed fully (scan).
        """
        if head is None:return
        self._head(head)
        identity=head['head'];remaining=head['records'];segments=head['segments']
        while identity is not None:
            if segments<1 or type(identity) is not str or len(identity)!=64 or any(c not in '0123456789abcdef' for c in identity):
                raise CheckpointError('invalid archive chain')
            path=self._segment_path(identity)
            try:
                payload=self._read(path)
            except OSError as error:raise CheckpointError('required history archive is unavailable') from error
            if hashlib.sha256(payload).hexdigest()!=identity:raise CheckpointError('archive checksum mismatch')
            framing=_SEGMENT_HEAD.match(payload)
            found=_RECORD_START.findall(payload) if framing and payload.endswith(_SEGMENT_TAIL) else None
            count=remaining-int(framing.group(2)) if framing else -1
            if (found is None or len(found)!=count or not 0<count<=self.max_records
                    or int(framing.group(3))!=segments-1):
                # Unusual framing (e.g. a record containing the record pattern):
                # parse this segment fully.
                try:tree=json.loads(payload)
                except ValueError as error:raise CheckpointError('invalid archive segment encoding') from error
                fields=_tagged_fields(tree)
                if fields is None:raise CheckpointError('invalid archive segment inventory')
                scalars={k:decode_tagged(fields[k]) for k in ('previous_head','previous_records','previous_segments')}
                records_field=fields.get('records')
                if (type(records_field) is not list or len(records_field)!=2 or records_field[0]!='list'
                        or scalars['previous_records']!=remaining-len(records_field[1]) or scalars['previous_segments']!=segments-1):
                    raise CheckpointError('invalid archive segment inventory')
                for item in records_field[1]:
                    record=_tagged_fields(item)
                    if record is None or set(record)!={'kind','id','record'}:raise CheckpointError('invalid archived record')
                    yield decode_tagged(record['kind']),decode_tagged(record['id'])
                previous=scalars['previous_head'];remaining=scalars['previous_records']
            else:
                for item_id,kind in found:
                    yield json.loads(b'"'+kind+b'"'),json.loads(b'"'+item_id+b'"')
                previous=framing.group(1).decode('ascii') if framing.group(1) else None
                remaining=int(framing.group(2))
            identity=previous;segments-=1
        if remaining or segments:raise CheckpointError('incomplete archive chain')

    def records(self,head):
        """Newest segments first, decoded. Keeps only one bounded segment in memory."""
        for kind,identity,record in self.scan(head):
            yield {'kind':kind,'id':identity,'record':decode_tagged(record)}

    def validate(self,head):
        for _ in self.scan(head):pass


def retire_closed_clocks(rules, archive):
    """Closed clocks cannot change a future route decision.

    Keep unresolved obligations canonical. Completed clocks have already
    cancelled their no-op wakeup; no executable action addresses them. A closed
    violated clock is archived like any other, and its identity is kept in
    rules.retired_violation_ids for assessments, so the LOT it names can retire.
    """
    records=[]
    for identity,record in rules.clocks.items():
        if record['closed_at_us'] is not None:
            records.append({'kind':'clock','id':identity,'record':record})
            if len(records)==archive.max_records:break
    if not _ready(archive,len(records)):return 0
    head=archive.append_all(rules.clock_archive,records)
    rules.clock_archive=head
    for record in records:
        if record['record']['violated']:
            bisect.insort(rules.retired_violation_ids,[record['record']['event_id'],record['id']])
        del rules.clocks[record['id']]
    return len(records)


def logical_clocks(rules):
    result=dict(rules.clocks)
    head=rules.clock_archive
    if head is not None:
        for item in HistoryArchive(head['directory']).records(head):
            if item['kind']!='clock' or item['id'] in result:
                raise CheckpointError('duplicate or invalid archived clock')
            result[item['id']]=item['record']
    return result


def clock_ledger_digest(rules):
    """Explicit v2 logical map fingerprint, independent of archive partitioning.

    Every kind/ID and complete record body contributes a SHA-256 leaf. Count
    and commutative XOR bind membership without reading old history per save.
    Archive validation recomputes the same fingerprint from bounded segments.
    """
    head=rules.clock_archive
    count=head['records'] if head else 0
    logical=int(head['logical_digest'],16) if head else 0
    for identity,record in rules.clocks.items():
        leaf={'kind':'clock','id':identity,'record':record}
        logical ^= int.from_bytes(hashlib.sha256(encode_value(leaf)).digest(),'big');count+=1
    return {'representation':'fab-clock-ledger-digest-v1','records':count,'digest':format(logical,'064x')}


def _maintenance_record(record):
    # The binding is a live shared object: its current counters belong to the
    # definitions ledger. Historical causes retain its stable identity, not a
    # frozen copy that would misrepresent later binding updates.
    return {**record,'binding':(record['tool_id'],record['kind'],record['definition_id'])}


def maintenance_ledger_digest(maintenance):
    head=maintenance.history_archive
    count=head['records'] if head else 0
    logical=int(head['logical_digest'],16) if head else 0
    for identity,record in maintenance.causes.items():
        leaf={'kind':'maintenance-cause','id':identity,'record':_maintenance_record(record)}
        logical ^= int.from_bytes(hashlib.sha256(encode_value(leaf)).digest(),'big');count+=1
    return {'representation':'fab-maintenance-ledger-digest-v1','records':count,'digest':format(logical,'064x')}


def retire_recovered_causes(maintenance,archive):
    # Native callbacks use cause IDs only while scheduled/active. Preserve even
    # cancelled heap references until the scheduler has discarded them.
    referenced={event[4].get('cause_id') for event in maintenance.scheduler._heap}
    records=[]
    for identity,record in maintenance.causes.items():
        if record['status']=='recovered' and identity not in referenced:
            records.append({'kind':'maintenance-cause','id':identity,'record':_maintenance_record(record)})
            if len(records)==archive.max_records:break
    if not _ready(archive,len(records)):return 0
    head=archive.append_all(maintenance.history_archive,records)
    maintenance.history_archive=head
    for record in records:del maintenance.causes[record['id']]
    return len(records)


def record_ledger_digest(owner,kind):
    """Bind every complete historical body without hydrating old segments."""
    head=owner.history_archive
    count=head['records'] if head else 0
    logical=int(head['logical_digest'],16) if head else 0
    for identity,record in owner.records.items():
        logical ^= int.from_bytes(hashlib.sha256(encode_value({'kind':kind,'id':identity,'record':record})).digest(),'big')
        count+=1
    return {'representation':'fab-'+kind+'-ledger-digest-v1','records':count,'digest':format(logical,'064x')}


def logical_records(owner,kind):
    """Explicit offline reconstruction, never part of dispatch or capture."""
    result=dict(owner.records);head=owner.history_archive
    if head is not None:
        for item in HistoryArchive(head['directory']).records(head):
            if item['kind']!=kind or item['id'] in result:raise CheckpointError('invalid archived '+kind)
            result[item['id']]=item['record']
    return result


def _retire_records(owner,archive,kind,eligible):
    records=[]
    for identity,record in owner.records.items():
        if eligible(identity,record):
            records.append({'kind':kind,'id':identity,'record':record})
            if len(records)==archive.max_records:break
    if not _ready(archive,len(records)):return 0
    owner.history_archive=archive.append_all(owner.history_archive,records)
    for item in records:del owner.records[item['id']]
    return len(records)


def retire_clearance_records(pool,archive):
    # Only active handoffs may return a leased member to planned on cancel.
    handoffs=getattr(pool.transfers,'handoffs',None)
    referenced={record['lease_id'] for record in handoffs.current_records()} if handoffs else set()
    return _retire_records(pool,archive,'clearance-lease',lambda identity,record:
        record['status'] in ('completed','cancelled') and identity not in pool._active_records
        and identity not in referenced and all(member['status']=='departed' for member in record['members']))


def retire_campaign_records(campaigns,archive):
    referenced=set(campaigns.active_by_tool.values())|set(campaigns.visit_owners.values())
    return _retire_records(campaigns,archive,'setup-campaign',lambda identity,record:
        record['status']=='completed' and identity not in referenced
        and all(member['completed'] for member in record['members']))


def history_manifest(owner,kind):
    """Small explicit export; full bodies remain in the immutable archive."""
    return {'representation':'active-plus-archive-v1',
        'ledger':record_ledger_digest(owner,kind),'archive':owner.history_archive}


_ALWAYS_ENCODABLE=(type(None),bool,int,str)


def durable_record(value):
    """Complete terminal native body with declarative action/timer values."""
    # encode_value accepts every None/bool/int/str; skip its per-leaf check.
    if type(value) in _ALWAYS_ENCODABLE:return value
    from .actions import BoundAction,export_action
    from .service import Timer
    if isinstance(value,BoundAction):return {'action':export_action(value)}
    if isinstance(value,Timer):
        from .runtime_state import OBJECT_FIELDS
        return {'native_type':'Timer','fields':{name:durable_record(getattr(value,name))
            for name in OBJECT_FIELDS['Timer'][1].split()}}
    if isinstance(value,dict):return {durable_record(k):durable_record(v) for k,v in value.items()}
    if type(value) in (list,tuple,set,frozenset):return type(value)(durable_record(v) for v in value)
    if callable(value):raise CheckpointError('opaque callback cannot be archived')
    # Check the primitive codec explicitly; no unknown object is silently lost.
    encode_value(value)
    return value


def service_ledger_digest(handler):
    head=handler.history_archive;count=head['records'] if head else 0
    logical=int(head['logical_digest'],16) if head else 0
    for identity,record in handler.requests.items():
        leaf={'kind':'service:'+handler.id,'id':identity,'record':durable_record(record)}
        logical ^= int.from_bytes(hashlib.sha256(encode_value(leaf)).digest(),'big');count+=1
    return {'representation':'fab-service-ledger-digest-v1','records':count,'digest':format(logical,'064x')}


def retire_service_requests(handler,archive,heap_kinds=None):
    # Retain even cancelled scheduler heap references until discarded. Native
    # service timers are owned solely by their request; active/pending requests
    # and shared carrier claims remain untouched. Retirement schedules nothing,
    # so callers may pass one heap_kinds set for a whole retirement pass.
    if heap_kinds is None:heap_kinds={event[3] for event in handler.scheduler._heap}
    records=[]
    for identity,record in handler.requests.items():
        timer=record.get('timer')
        if (record['status'] not in ('complete','cancelled') or handler.active is record
                or identity in handler._pending or timer and (
                    not (timer.completed or timer.cancelled) or timer._kind in heap_kinds)):
            continue
        records.append({'kind':'service:'+handler.id,'id':identity,'record':durable_record(record)})
        if len(records)==archive.max_records:break
    if not _ready(archive,len(records)):return 0
    previous=handler.history_archive;head=archive.append_all(previous,records)
    handler.history_archive=head
    for item in records:del handler.requests[item['id']]
    return len(records)


def traffic_ledger_digest(traffic):
    head=traffic.history_archive;count=head['records'] if head else 0
    logical=int(head['logical_digest'],16) if head else 0
    for identity,record in traffic.requests.items():
        leaf={'kind':'traffic-request','id':identity,'record':durable_record(record)}
        logical ^= int.from_bytes(hashlib.sha256(encode_value(leaf)).digest(),'big');count+=1
    return {'representation':'fab-traffic-ledger-digest-v1','records':count,'digest':format(logical,'064x')}


def retire_idle_requests(traffic,archive):
    # Only autonomous terminal reposition work. Transfer permission evidence is
    # still consumed by arrived/cancelled_arrived and must remain available.
    active=set(traffic.active.values())
    active.update(v.get('movement_id') for v in traffic.vehicles.values())
    active.update(h['request_id'] for h in traffic.hops.values())
    movements=traffic.transfers.movements if traffic.transfers is not None else {}
    records=[]
    for identity,record in traffic._idle_requests.items():
        if (not record['reposition'] or record['status'] not in ('complete','cancelled')
                or identity in active or identity in movements):continue
        records.append({'kind':'traffic-request','id':identity,'record':durable_record(record)})
        if len(records)==archive.max_records:break
    if not _ready(archive,len(records)):return 0
    previous=traffic.history_archive;head=archive.append_all(previous,records)
    traffic.history_archive=head
    for item in records:
        del traffic.requests[item['id']];del traffic._idle_requests[item['id']]
    return len(records)


def native_ledger_digest(records,head,kind):
    count=head['records'] if head else 0;logical=int(head['logical_digest'],16) if head else 0
    for identity,record in records.items():
        logical ^= int.from_bytes(hashlib.sha256(encode_value({'kind':kind,'id':identity,'record':durable_record(record)})).digest(),'big')
        count+=1
    return {'representation':'fab-'+kind+'-ledger-digest-v1','records':count,'digest':format(logical,'064x')}


def retire_transfer_groups(t,archive):
    # Demand completion includes rack exchange and late cancellation arrival.
    # All six ledgers publish before any canonical head or body is changed.
    traffic=getattr(t,'traffic',None);handoffs=t.handoffs
    ledgers=[(t,'history_archive','transfer-demand',t.demands,[]),
        (t,'movements_archive','transfer-movement',t.movements,[]),
        (t.exchange,'history_archive','equipment-exchange',t.exchange.requests,[])]
    if traffic is not None:ledgers.append((traffic,'history_archive','traffic-request',traffic.requests,[]))
    if handoffs is not None:ledgers.append((handoffs,'history_archive','handoff-plan',handoffs.records,[]))
    heap_kinds={event[3] for event in t.scheduler._heap}
    active=set(t.active.values())|{v.get('assignment_id') for v in t.vehicles.values()}
    movement_refs={v.get('movement_id') for v in t.vehicles.values()}
    if traffic is not None:movement_refs.update(traffic.active.values())
    reservations={slot['reservation'] for slot in t.custody.slots.values()}
    selected=0
    for did,demand in t.demands.items():
        if (demand['status'] not in ('delivered','cancelled','reused') or did in active
                or did in reservations or handoffs is not None and did in handoffs._active_records):continue
        timer=demand.get('timer')
        if timer and (not(timer.completed or timer.cancelled) or timer._kind in heap_kinds):continue
        mids=[mid for mid in (did+':pickup',did+':drop') if mid in t.movements]
        if any(mid in movement_refs or t.movements[mid]['status'] not in ('complete','cancelled') for mid in mids):continue
        if traffic is not None and any(mid in traffic.requests and traffic.requests[mid]['status'] not in ('complete','cancelled') for mid in mids):continue
        # Terminal exchanges still need their service callback body until its
        # native timer/heap references have been retired by service archival.
        eids=[eid for eid in (did+':input',did+':output') if eid in t.exchange.requests]
        if any(t.exchange.requests[eid]['status'] not in ('complete','cancelled')
                or eid in t.exchange.handlers[t.exchange.requests[eid]['tool_id']].requests for eid in eids):continue
        plan=handoffs.records.get(did) if handoffs is not None else None
        if plan and plan['phase'] not in ('completed','cancelled'):continue
        identities={'transfer-demand':[did],'transfer-movement':mids,'equipment-exchange':eids,
            'traffic-request':[mid for mid in mids if traffic is not None and mid in traffic.requests],
            'handoff-plan':[did] if plan else []}
        for owner,attribute,kind,warm,records in ledgers:
            records.extend({'kind':kind,'id':identity,'record':durable_record(warm[identity])} for identity in identities[kind])
        selected+=1
        if selected==archive.max_records:break
    if not _ready(archive,selected):return 0
    publications=[]
    for owner,attribute,kind,warm,records in ledgers:
        if not records:continue
        previous=getattr(owner,attribute);head=previous
        # A restored compound group may span existing parent chains and new
        # child chains. Resolve each ledger separately before publishing.
        target=archive  # Continues a parent chain from an earlier attempt's directory if needed.
        head=target.append_all(head,records)
        publications.append((owner,attribute,kind,warm,records,previous,head))
    for owner,attribute,kind,warm,records,previous,head in publications:
        setattr(owner,attribute,head)
        for item in records:del warm[item['id']]
    if handoffs is not None:
        for item in ledgers[0][-1]:handoffs.waits.pop(item['id'],None)
    return selected


def logical_native_records(warm,head,kind):
    """Explicit offline reconstruction for exports/coverage tests, never dispatch."""
    result=dict(warm)
    if head is not None:
        for item in HistoryArchive(head['directory']).records(head):
            if item['kind']!=kind or item['id'] in result:raise CheckpointError('invalid archived '+kind)
            result[item['id']]=item['record']
    return result


def retire_manufacturing_records(m,archive):
    heap_kinds={event[3] for event in m.scheduler._heap}
    owned={tool.get('active_execution_id') for tool in m.tools.values()}|set(m.claims.values())
    owned.update(eid for values in m.cascade_executions.values() for eid in values)
    plans=[];executions=[];assemblies=[]
    for eid,e in m.executions.items():
        if e['status']!='unloaded' or eid in owned:continue
        timer=e.get('timer')
        if timer and (not(timer.completed or timer.cancelled) or timer._kind in heap_kinds):continue
        handler=m.handlers[e['tool_id']]
        if any(eid+':'+phase in handler.requests for phase in ('setup','load','unload')):continue
        assembly=m.batches.assemblies.get(e.get('assembly_id'))
        if assembly and (assembly['status']!='cleared' or not all(member['departed'] for member in assembly['members'])
                or m.tools[e['tool_id']].get('assembly_id')==assembly['id']):continue
        pipe=m.pipelines.get(e['tool_id'])
        if pipe and eid in pipe.executions:
            units=[u for u in pipe.units.values() if u['execution_id']==eid]
            if (pipe.executions[eid]['remaining']!=0 or pipe.active_execution==eid
                    or any(u['status']!='complete' or u['id'] in pipe.pending or u['id'] in pipe.slots
                        or not u['timer'].completed or u['timer']._kind in heap_kinds for u in units)):continue
            plans.extend(((pipe,'units_archive','cascade-unit:'+pipe.id,pipe.units,[{'kind':'cascade-unit:'+pipe.id,'id':u['id'],'record':durable_record(u)} for u in units]),
                (pipe,'history_archive','cascade-execution:'+pipe.id,pipe.executions,[{'kind':'cascade-execution:'+pipe.id,'id':eid,'record':durable_record(pipe.executions[eid])}])))
        executions.append({'kind':'manufacturing-execution','id':eid,'record':durable_record(e)})
        if assembly:assemblies.append({'kind':'batch-assembly','id':assembly['id'],'record':durable_record(assembly)})
        if len(executions)==archive.max_records:break
    if not _ready(archive,len(executions)):return 0
    plans.extend(((m,'executions_archive','manufacturing-execution',m.executions,executions),
        (m.batches,'history_archive','batch-assembly',m.batches.assemblies,assemblies)))
    # Multiple completed executions may share a pipeline. Combine them before
    # publication so its one head includes every selected unit/execution.
    combined={}
    for owner,attribute,kind,warm,records in plans:
        key=(id(owner),attribute)
        if key not in combined:combined[key]=[owner,attribute,kind,warm,[]]
        combined[key][-1].extend(records)
    publications=[]
    for owner,attribute,kind,warm,records in combined.values():
        if not records:continue
        previous=getattr(owner,attribute);head=previous
        # A restored compound group may span existing parent chains and new
        # child chains. Resolve each ledger separately before publishing.
        target=archive  # Continues a parent chain from an earlier attempt's directory if needed.
        head=target.append_all(head,records)
        publications.append((owner,attribute,kind,warm,records,previous,head))
    for owner,attribute,kind,warm,records,previous,head in publications:
        setattr(owner,attribute,head)
        for item in records:del warm[item['id']]
    return len(executions)


def empty_retired_facts():
    return {'total':0,'products':{},'root_consumed':{}}


def wire_lot_history(flow):
    m=flow.manufacturing
    m._lot_history_owner=m.rules._lot_history_owner=m.streams._lot_history_owner=flow
    flow.actions=m.actions


def _lot_bundle(flow,lid):
    m=flow.manufacturing;lot=flow.lots[lid]
    state=m.streams._streams.get(('processing',lid))
    return {'lot':lot,'visits':{vid:m.rules.visits[vid] for vid in lot.get('route_history',())},
        'processing_rng_state':state.getstate() if state is not None else None,
        'overflow_visits':{pair for pair in flow.transfers.overflow_visits if pair[0]==lot['carrier_id']}}


def lot_ledger_digest(flow):
    head=flow.history_archive;count=head['records'] if head else 0
    logical=int(head['logical_digest'],16) if head else 0
    seen=0
    for lid in flow.lots:
        record=_lot_bundle(flow,lid);seen+=len(record['visits'])
        leaf={'kind':'lot-bundle','id':lid,'record':record}
        logical ^= int.from_bytes(hashlib.sha256(encode_value(leaf)).digest(),'big');count+=1
    if seen!=len(flow.manufacturing.rules.visits):raise CheckpointError('visit history is not fully owned by LOTs')
    return {'representation':'fab-lot-visit-rng-ledger-v1','records':count,'digest':format(logical,'064x')}


def _add_lot_facts(flow,facts,lid,bundle):
    lot=bundle['lot'];visits=bundle['visits'];rng=bundle['processing_rng_state']
    if (set(bundle)!={'lot','visits','processing_rng_state','overflow_visits'} or lot['lot_id']!=lid
            or lot.get('exited_at_us') is None or lot.get('current_visit_id') is not None
            or len(lot['route_history'])!=len(set(lot['route_history'])) or set(lot['route_history'])!=set(visits)
            or any(type(pair) is not tuple or len(pair)!=2 or pair[0]!=lot['carrier_id'] or pair[1] is not None and pair[1] not in visits for pair in bundle['overflow_visits'])
            or any(vid!=v['id'] or v['lot_id']!=lid or v['status']!='unloaded' for vid,v in visits.items())):
        raise CheckpointError('invalid retired LOT history')
    if rng is not None:
        from .rng import rng_for
        rng_for(flow.manufacturing.streams.master_seed,'processing',lid).setstate(rng)
    p=lot['product_id'];rid=lot['route_id'];root=flow.manufacturing.rules.routes[rid]['steps'][0]['source_step_id']
    item=facts['products'].setdefault(p,{'admitted':0,'exited':0,'max_entered_step':None,'first_exit_at_us':None})
    item['admitted']+=lot.get('admitted_at_us') is not None;item['exited']+=1;facts['total']+=1
    first=lot['exited_at_us'];item['first_exit_at_us']=min(item['first_exit_at_us'],first) if item['first_exit_at_us'] is not None else first
    steps=[int(v['source_step_id']) for v in visits.values() if str(v['source_step_id']).isdigit()]
    if steps:item['max_entered_step']=max([item['max_entered_step'] or 0,*steps])
    if any(v.get('assembly_id') and v['source_step_id']==root for v in visits.values()):
        key=(p,rid);facts['root_consumed'][key]=facts['root_consumed'].get(key,0)+1


def validate_lot_history(flow):
    facts=empty_retired_facts();head=flow.history_archive
    if head is not None:
        ids=[]
        for item in HistoryArchive(head['directory']).records(head):
            if item['kind']!='lot-bundle':raise CheckpointError('invalid retired LOT archive kind')
            _add_lot_facts(flow,facts,item['id'],item['record']);ids.append(item['id'])
        # A disposable disk-side uniqueness check avoids a growing Python ID set;
        # index the identities from this pass instead of re-reading the chain.
        lookup=flow._archive_lookup();lookup.prime(head,'lot-bundle',ids)
        for lid in flow.lots:
            if lookup.contains(head,'lot-bundle',lid):raise CheckpointError('retired LOT remains live')
        if not flow.lots:lookup.contains(head,'lot-bundle','__validate_absent__')
    if facts!=flow.retired_facts:raise CheckpointError('retired LOT progress facts differ from archive')


def retire_exited_lots(flow,archive):
    from copy import deepcopy
    m=flow.manufacturing;t=flow.transfers
    blocked={r['lot_id'] for r in t.demands.values()}
    blocked.update(c['lot_id'] for c in m.rules.clocks.values())
    for e in m.executions.values():
        blocked.update(m.rules.visits[vid]['lot_id'] for vid in ([e['visit_id']] if not e.get('members') else [member['visit_id'] for member in e['members']]))
    for owner in (t.clearance,m.campaigns):
        if owner:
            for record in owner.records.values():
                blocked.update(m.rules.visits[member['visit_id']]['lot_id'] for member in record['members'] if member['visit_id'] in m.rules.visits)
    for lid,lot in flow.lots.items():
        carrier=lot['carrier_id']
        if (lot.get('exited_at_us') is None or lid in blocked or lid in flow._active_lots
                or lot.get('current_visit_id') is not None or carrier in flow.custody.locations
                or carrier in m.claims or carrier in t.active):continue
        bundle=_lot_bundle(flow,lid);leaf={'kind':'lot-bundle','id':lid,'record':bundle}
        size=len(encode_value(leaf));flow.retirement_metrics['largest_record_bytes']=max(flow.retirement_metrics['largest_record_bytes'],size)
        if size>archive.max_bytes-2048:
            # Preserve uncommon oversized histories rather than fail a running
            # model or silently truncate fields. Metrics make this visible.
            flow.retirement_metrics['oversize_attempts']+=1;continue
        facts=deepcopy(flow.retired_facts);_add_lot_facts(flow,facts,lid,bundle)
        previous=flow.history_archive;head=archive.append(previous,[leaf])
        flow.history_archive=head;flow.retired_facts=facts
        for vid in bundle['visits']:del m.rules.visits[vid]
        t.overflow_visits.difference_update(bundle['overflow_visits'])
        m.streams.discard('processing',lid);del flow.lots[lid]
        return 1
    return 0


def logical_lot_histories(flow):
    """Explicit offline full bodies; normal continuation never hydrates them."""
    lots=dict(flow.lots);visits=dict(flow.manufacturing.rules.visits);head=flow.history_archive
    if head is not None:
        for item in HistoryArchive(head['directory']).records(head):
            if item['kind']!='lot-bundle' or item['id'] in lots:raise CheckpointError('invalid retired LOT archive')
            bundle=item['record'];lots[item['id']]=bundle['lot']
            for vid,v in bundle['visits'].items():
                if vid in visits:raise CheckpointError('duplicate retired visit')
                visits[vid]=v
    return lots,visits


def logical_rng_states(flow):
    result=flow.manufacturing.streams.export_state()['states'];head=flow.history_archive
    if head is not None:
        for item in HistoryArchive(head['directory']).records(head):
            if item['kind']!='lot-bundle':raise CheckpointError('invalid retired LOT RNG archive')
            state=item['record']['processing_rng_state']
            if state is not None:
                key=('processing',item['id'])
                if key in result:raise CheckpointError('retired RNG remains live')
                result[key]=state
    return result
