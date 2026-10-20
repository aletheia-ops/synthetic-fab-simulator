"""Synchronous bounded local evidence. Failure is terminal, never a retry path."""
from copy import deepcopy
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
from .json_stream import json_chunks,bounded_line
from .publisher import Publisher

SCHEMA='fab-events-v3'


class OutputFailure(RuntimeError):pass


class Output:
    def __init__(self,directory,run_id,manifest,*,chunk_records=64,chunk_bytes=65536,max_record_bytes=65536,publisher=None,base_sequence=0,base_time_us=None):
        if (not isinstance(run_id,str) or not run_id or not isinstance(manifest,dict) or
                any(type(n) is not int or n<=0 for n in (chunk_records,chunk_bytes,max_record_bytes)) or
                max_record_bytes>chunk_bytes):raise ValueError('invalid output identity or count/byte limits')
        if publisher is not None and not isinstance(publisher,Publisher):
            raise ValueError('invalid required publisher')
        self.directory=Path(directory);self.run_id=run_id;self.manifest=deepcopy(manifest);self.publisher=publisher
        self.chunk_records=chunk_records;self.chunk_bytes=chunk_bytes;self.max_record_bytes=max_record_bytes
        if type(base_sequence) is not int or base_sequence<0 or (base_time_us is not None and (type(base_time_us) is not int or base_time_us<0)):
            raise ValueError('invalid continuation watermark')
        self.base_sequence=base_sequence
        self.sequence=base_sequence;self.last_committed_sequence=base_sequence;self.last_committed_time_us=base_time_us
        self.high_water_records=0;self.high_water_bytes=0;self.status='open';self.failure=None;self.files={}
        # events: factory/LOT/carrier events; oht: per-vehicle OHT sensor events.
        # Both share one event sequence so they merge into a total order.
        self.record_counts={'events':0,'oht':0,'diagnostics':0}
        self._writing=False;self._summary_source=None;self.summary={'outcome':None,'constraint_status':'unassessed'}
        # Exclusive directory creation protects every file in an earlier attempt.
        self.directory.mkdir(parents=True,exist_ok=False)
        try:
            for kind in self.record_counts:self.files[kind]=(self.directory/(kind+'.jsonl')).open('xb',buffering=0)
            self._manifest({'outcome':None,'constraint_status':'unassessed'},'pending')
        except Exception as error:self._fail(error)

    @property
    def summary(self):
        # A deferred source is evaluated on first read, at the latest at the
        # next failure, checkpoint watermark or explicit replacement.
        source=self._summary_source
        if source is not None:self._summary_source=None;self._summary=source()
        return self._summary

    @summary.setter
    def summary(self,value):
        self._summary_source=None;self._summary=value

    def defer_summary(self,source):
        """Record the summary as a callable; it must read only current state."""
        if not callable(source):raise ValueError('summary source must be callable')
        self._summary_source=source

    def _open(self):
        if self.status!='open':raise OutputFailure('output is '+self.status)

    def _envelope(self, record):
        if not isinstance(record,dict) or set(record)-{'simulation_time_us','event_type','entity_refs','payload'}:
            raise ValueError('invalid event envelope fields')
        tick=record.get('simulation_time_us');kind=record.get('event_type');refs=record.get('entity_refs',{})
        if (type(tick) is not int or tick<0 or not isinstance(kind,str) or not kind or
                not isinstance(record.get('payload'),dict) or not isinstance(refs,dict) or
                any(not isinstance(k,str) or not k or not isinstance(ids,(list,tuple)) or
                    any(not isinstance(i,str) or not i for i in ids) for k,ids in refs.items())):
            raise ValueError('invalid event envelope values')
        if tick<self._accepted_time:raise ValueError('event simulation time cannot go backwards')
        sequence=self.sequence+1
        return {'schema_version':SCHEMA,'run_id':self.run_id,'event_sequence':sequence,
                'event_id':f'{self.run_id}:{sequence}','simulation_time_us':tick,
                'simulation_time_s':Decimal((0,tuple(int(n) for n in str(tick)),-6)),'event_type':kind,
                'entity_refs':refs,'payload':record['payload']}

    def commit(self,records):self._records('events',records)
    def commit_oht(self,records):self._records('oht',records)
    def write_diagnostics(self,records):self._records('diagnostics',records)

    def _records(self,kind,records):
        self._open()
        if self._writing:raise OutputFailure('reentrant output commit is forbidden')
        self._writing=True;pending=[];size=0
        self._accepted_time=self.last_committed_time_us if self.last_committed_time_us is not None else 0
        try:
            for record in records:
                if not isinstance(record,dict):raise ValueError('output record must be an object')
                sequenced=kind in ('events','oht')
                value=self._envelope(record) if sequenced else {**record,'schema_version':SCHEMA,'run_id':self.run_id}
                line=bounded_line(value,self.max_record_bytes)
                if pending and (len(pending)>=self.chunk_records or size+len(line)>self.chunk_bytes):
                    self._flush(kind,pending);pending=[];size=0
                metadata=(value['event_sequence'],value['simulation_time_us']) if sequenced else None
                pending.append((line,metadata));size+=len(line)
                if metadata:self.sequence=metadata[0];self._accepted_time=metadata[1]
                self.high_water_records=max(self.high_water_records,len(pending))
                self.high_water_bytes=max(self.high_water_bytes,size)
                if len(pending)==self.chunk_records or size==self.chunk_bytes:
                    self._flush(kind,pending);pending=[];size=0
            self._flush(kind,pending)
        except Exception as error:
            # An invalid next record must not discard valid buffered predecessors.
            if self.status=='open':
                try:self._flush(kind,pending)
                except Exception as write_error:error=write_error
            self._fail(error)
        finally:self._writing=False

    def _flush(self,kind,pending):
        while pending:
            line,metadata=pending[0];stream=self.files[kind];start=stream.tell()
            try:
                remaining=memoryview(line)
                while remaining:
                    written=stream.write(remaining)
                    if type(written) is not int or written<=0 or written>len(remaining):raise OSError('invalid/short output write')
                    remaining=remaining[written:]
                stream.flush()
            except Exception:
                # Preserve complete JSONL prefix when storage still permits cleanup.
                try:stream.seek(start);stream.truncate();stream.flush()
                except Exception:pass
                self.status='failed';raise
            self.record_counts[kind]+=1
            if metadata:self.last_committed_sequence,self.last_committed_time_us=metadata
            pending.pop(0)
            if metadata and self.publisher is not None:
                try:
                    committed_at=self.publisher.clock()
                    self.publisher.submit((json.loads(line,parse_float=Decimal),),committed_at=committed_at)
                except Exception:
                    # Local identity is committed, but publication failure is terminal.
                    self.status='failed';raise

    def _manifest(self,summary,output_status):
        value={**self.manifest,**summary,'schema_version':SCHEMA,'run_id':self.run_id,
               'output_status':output_status,'last_committed_sequence':self.last_committed_sequence,
               'last_committed_time_us':self.last_committed_time_us,'record_counts':dict(self.record_counts),
               'output_policy':{'chunk_records':self.chunk_records,'chunk_bytes':self.chunk_bytes,
                                'max_record_bytes':self.max_record_bytes,'local_mode':'synchronous',
                                'required_publication':self.publisher is not None},
               'output_high_water':{'records':self.high_water_records,'bytes':self.high_water_bytes}}
        if self.base_sequence or 'continuation' in self.manifest:value['base_sequence']=self.base_sequence
        if self.publisher is not None:
            value['publication']={'failed':self.publisher.failed,'pending_ids':list(self.publisher.pending_ids),
                                  'last_admitted_sequence':self.publisher.last_sequence,'policy':self.publisher.policy()}
            try:value['publication'].update(self.publisher.metrics())
            except Exception:value['publication']['metrics_unavailable']=True
        temporary=self.directory/'manifest.pending'
        try:
            with temporary.open('wb') as stream:
                for part in json_chunks(value):stream.write(part)
                stream.write(b'\n');stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,self.directory/'manifest.json')
        finally:
            temporary.unlink(missing_ok=True)
        self.final_manifest=value;return value

    def _close(self):
        first=None
        for stream in self.files.values():
            try:stream.flush();stream.close()
            except Exception as error:
                if first is None:first=error
                try:stream.close()
                except Exception:pass
        if first:raise first

    def _fail(self,error):
        self.status='failed';self.failure=str(error)
        try:self._close()
        except Exception:pass
        try:self._manifest({**self.summary,'outcome':'Failed','reason':'required_output_failure','failure':self.failure},'failed')
        except Exception:
            try:sys.stderr.write(f'required output failed: {self.failure}; last commit {self.run_id}:{self.last_committed_sequence}\n')
            except Exception:pass
        raise OutputFailure(self.failure) from error

    def checkpoint_watermark(self) -> dict[str, object]:
        self._open()
        if self._writing:raise OutputFailure('checkpoint during output commit')
        from .checkpoint_store import drop_cached_pages
        for stream in self.files.values():
            stream.flush();os.fsync(stream.fileno())
            # The committed logs are append-only and not read back by the run.
            drop_cached_pages(stream.fileno())
        self._manifest(self.summary,'pending')
        return {'parent_run_id':self.run_id,'parent_output_directory':str(self.directory.resolve()),
            'last_committed_sequence':self.last_committed_sequence,'last_committed_time_us':self.last_committed_time_us,
            'record_counts':dict(self.record_counts),'base_sequence':self.base_sequence}

    def finalize(self,summary,*,drain_complete=True):
        self._open()
        if self._writing:raise OutputFailure('reentrant output finalization is forbidden')
        try:
            if not isinstance(summary,dict) or summary.get('outcome') not in ('Completed','Incomplete','Failed','Rejected'):
                raise ValueError('final outcome required')
            self.summary=deepcopy(summary)
            if drain_complete is not True:raise OutputFailure('required output drain did not complete')
            if self.publisher is not None:self.publisher.drain()
            self._close();result=self._manifest(summary,'complete');self.status='complete';return result
        except Exception as error:self._fail(error)
