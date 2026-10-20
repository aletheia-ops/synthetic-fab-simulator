"""Bounded required publication and wall-clock pacing, independent of domain time.

Adapters implement publish(tuple_of_records, timeout_s=...) and must honor the
supplied timeout. Responses contain status (ok/retryable/permanent), accepted_ids,
and optional retry_after_s/message. TimeoutError means unknown acceptance;
other raised exceptions are terminal. No external adapter is selected here.
"""
from collections import deque
from contextlib import contextmanager
from decimal import Decimal
from fractions import Fraction
import json
import math
import time
from .json_stream import bounded_line


class PublicationFailure(RuntimeError):pass


def _number(value,*,positive=True):
    if isinstance(value,bool):raise ValueError('invalid wall-clock control')
    number=float(value)
    if not math.isfinite(number) or (number<=0 if positive else number<0):raise ValueError('invalid wall-clock control')
    return number


class Publisher:
    def __init__(self,adapter,*,max_records=128,max_bytes=131072,batch_records=64,batch_bytes=65536,
                 max_record_bytes=65536,flush_interval_s=.1,initial_delay_s=.1,cap_delay_s=2,
                 max_attempts=5,attempt_timeout_s=5,retry_budget_s=30,drain_timeout_s=30,
                 clock=time.monotonic,sleep=time.sleep):
        if (not callable(getattr(adapter,'publish',None)) or not callable(clock) or not callable(sleep) or
                any(type(n) is not int or n<=0 for n in (max_records,max_bytes,batch_records,batch_bytes,max_record_bytes,max_attempts)) or
                batch_records>max_records or not max_record_bytes<=batch_bytes<=max_bytes):
            raise ValueError('invalid publication adapter or bounds')
        self.max_records=max_records;self.max_bytes=max_bytes;self.batch_records=batch_records
        self.batch_bytes=batch_bytes;self.max_record_bytes=max_record_bytes;self.max_attempts=max_attempts
        for name,value in (('flush_interval_s',flush_interval_s),('initial_delay_s',initial_delay_s),('cap_delay_s',cap_delay_s),
                ('attempt_timeout_s',attempt_timeout_s),('retry_budget_s',retry_budget_s),('drain_timeout_s',drain_timeout_s)):
            setattr(self,name,_number(value))
        if self.initial_delay_s>self.cap_delay_s:raise ValueError('initial retry delay exceeds cap')
        self.adapter=adapter;self.clock=clock;self.sleep=sleep;self.queue=deque();self.bytes=0
        self.failed=None;self._busy=False;self._last_clock=None;self.started_at=self._now()
        self.run_id=None;self.last_sequence=0;self.accepted_count=0;self.attempt_count=0
        self.high_water_records=0;self.high_water_bytes=0;self.blocked_s=0.0
        self.max_commit_to_publication_s=0.0;self.total_commit_to_publication_s=0.0;self.pause_listeners=[]

    def _now(self):
        now=_number(self.clock(),positive=False)
        if self._last_clock is not None and now<self._last_clock:raise PublicationFailure('monotonic clock went backwards')
        self._last_clock=now;return now

    @contextmanager
    def _operation(self):
        if self.failed is not None:raise PublicationFailure(self.failed)
        if self._busy:raise PublicationFailure('reentrant publication is forbidden')
        self._busy=True
        try:yield
        except Exception as error:
            self.failed=str(error)
            raise PublicationFailure(self.failed) from error
        finally:self._busy=False

    @property
    def pending_ids(self):return tuple(r['id'] for r in self.queue)

    @property
    def next_flush_at(self):return self.queue[0]['queued_at']+self.flush_interval_s if self.queue else None

    def _sleep(self,seconds):
        if seconds<=0:return
        before=self._now();target=before+seconds
        while before<target:
            self.sleep(target-before);now=self._now()
            if now<=before:raise PublicationFailure('sleep did not advance monotonic time')
            before=now

    def submit(self,records,*,committed_at=None):
        """Admit immutable records, blocking at queue/batch/age thresholds."""
        with self._operation():
            entered=self._now();committed=entered if committed_at is None else _number(committed_at,positive=False)
            if committed>entered:raise ValueError('record commit time is in the future')
            self._due()
            for record in records:
                if not isinstance(record,dict):raise ValueError('publication record must be an object')
                run=record.get('run_id');sequence=record.get('event_sequence');rid=record.get('event_id')
                if (not isinstance(run,str) or not run or type(sequence) is not int or sequence!=self.last_sequence+1 or
                        rid!=f'{run}:{sequence}' or self.run_id not in (None,run)):
                    raise ValueError('publication requires contiguous committed event identities from one run')
                line=bounded_line(record,self.max_record_bytes)
                self._due()
                while self.queue and (len(self.queue)>=self.max_records or self.bytes+len(line)>self.max_bytes):self._flush_batch()
                self.queue.append({'id':rid,'line':line,'queued_at':self._now(),'committed_at':committed});self.bytes+=len(line)
                self.run_id=run;self.last_sequence=sequence
                self.high_water_records=max(self.high_water_records,len(self.queue));self.high_water_bytes=max(self.high_water_bytes,self.bytes)
                while len(self.queue)>=self.batch_records or self.bytes>=self.batch_bytes:self._flush_batch()

    def _due(self):
        while self.queue and self._now()>=self.next_flush_at:self._flush_batch()

    def flush_due(self):
        with self._operation():self._due()

    def flush(self):
        with self._operation():
            while self.queue:self._flush_batch()

    def drain(self):
        """One wall deadline covers all remaining required records and attempts."""
        with self._operation():
            deadline=self._now()+self.drain_timeout_s
            while self.queue:self._flush_batch(deadline)

    def wait_until(self,deadline):
        """Service flush deadlines while waiting for a wall-clock pacing target."""
        deadline=_number(deadline,positive=False)
        with self._operation():
            while True:
                self._due();now=self._now()
                if now>=deadline:return
                wake=min(deadline,self.next_flush_at) if self.queue else deadline
                self._sleep(wake-now)

    def _response(self,result,active_ids):
        if (not isinstance(result,dict) or set(result)-{'status','accepted_ids','retry_after_s','message'} or
                result.get('status') not in ('ok','retryable','permanent') or not isinstance(result.get('accepted_ids'),list)):
            raise ValueError('invalid publication acknowledgement')
        accepted=result['accepted_ids']
        if (len(accepted)>len(active_ids) or any(not isinstance(i,str) for i in accepted) or len(accepted)!=len(set(accepted)) or
                not set(accepted)<=active_ids):raise ValueError('acknowledgement IDs do not match unresolved batch')
        retry_after=result.get('retry_after_s')
        if retry_after is not None:retry_after=_number(retry_after,positive=False)
        return set(accepted),retry_after

    def _flush_batch(self,drain_deadline=None):
        start=self._now();deadline=start+self.retry_budget_s
        if drain_deadline is not None:deadline=min(deadline,drain_deadline)
        batch=[];size=0
        for record in self.queue:
            if len(batch)>=self.batch_records or size+len(record['line'])>self.batch_bytes:break
            batch.append(record);size+=len(record['line'])
        if not batch:raise PublicationFailure('record cannot fit publication batch')
        unresolved=batch;delay=self.initial_delay_s
        try:
            for attempt in range(1,self.max_attempts+1):
                call_start=self._now();remaining=deadline-call_start
                if remaining<=0:raise PublicationFailure('publication deadline or retry budget exhausted')
                timeout=min(self.attempt_timeout_s,remaining);self.attempt_count+=1
                try:
                    # Each attempt gets detached values; retained bytes never change.
                    result=self.adapter.publish(tuple(json.loads(r['line'],parse_float=Decimal) for r in unresolved),timeout_s=timeout)
                except TimeoutError:
                    result={'status':'retryable','accepted_ids':[]}
                now=self._now();accepted,retry_after=self._response(result,{r['id'] for r in unresolved})
                for r in unresolved:
                    if r['id'] in accepted:
                        self.queue.remove(r);self.bytes-=len(r['line']);self.accepted_count+=1
                        age=now-r['committed_at'];self.total_commit_to_publication_s+=age
                        self.max_commit_to_publication_s=max(self.max_commit_to_publication_s,age)
                unresolved=[r for r in unresolved if r['id'] not in accepted]
                if now>call_start+timeout:raise PublicationFailure('adapter exceeded supplied attempt timeout/deadline')
                if now>deadline:raise PublicationFailure('publication deadline or retry budget exhausted')
                if result['status']=='permanent':raise PublicationFailure('permanent publication failure: '+str(result.get('message','adapter rejected records')))
                if not unresolved:return
                if attempt==self.max_attempts:raise PublicationFailure('publication attempts exhausted')
                wait=max(delay,retry_after or 0)
                if wait>=deadline-self._now():raise PublicationFailure('retry wait cannot fit remaining publication budget')
                self._sleep(wait);delay=min(self.cap_delay_s,delay*2)
        finally:
            elapsed=self._now()-start;self.blocked_s+=elapsed
            for callback in self.pause_listeners:callback(elapsed)

    def metrics(self):
        now=self._now();elapsed=now-self.started_at
        return {'queue_records':len(self.queue),'queue_bytes':self.bytes,
                'oldest_age_s':now-self.queue[0]['queued_at'] if self.queue else 0.0,
                'high_water_records':self.high_water_records,'high_water_bytes':self.high_water_bytes,
                'accepted_count':self.accepted_count,'attempt_count':self.attempt_count,'blocked_s':self.blocked_s,
                'accepted_per_s':self.accepted_count/elapsed if elapsed else None,
                'max_commit_to_publication_s':self.max_commit_to_publication_s,
                'mean_commit_to_publication_s':self.total_commit_to_publication_s/self.accepted_count if self.accepted_count else None}

    def policy(self):
        names=('max_records','max_bytes','batch_records','batch_bytes','max_record_bytes','flush_interval_s',
               'initial_delay_s','cap_delay_s','max_attempts','attempt_timeout_s','retry_budget_s','drain_timeout_s')
        return {'version':'explicit-ack-v1','adapter_type':type(self.adapter).__name__,
                **{name:getattr(self,name) for name in names}}


class Pacer:
    """Wall delivery schedule; output pauses shift every subsequent target."""
    def __init__(self,*,rate=None,publisher=None,clock=None,sleep=None):
        if publisher is not None and ((clock is not None and clock!=publisher.clock) or
                                      (sleep is not None and sleep!=publisher.sleep)):
            raise ValueError('pacer and publisher must share clock and sleep owners')
        if rate is not None:
            _number(rate)
            self.rate=Fraction(Decimal(str(rate)))
        else:self.rate=None
        self.publisher=publisher;self.clock=clock or (publisher.clock if publisher is not None else time.monotonic)
        self.sleep=sleep or (publisher.sleep if publisher is not None else time.sleep)
        if not callable(self.clock) or not callable(self.sleep):raise ValueError('invalid pacing clock')
        self.origin=_number(self.clock(),positive=False);self.pause_s=0.0;self.last_tick=0;self.max_lateness_s=0.0
        if publisher is not None:publisher.pause_listeners.append(self.add_pause)

    def add_pause(self,seconds):self.pause_s+=_number(seconds,positive=False)

    def wait_for(self,simulation_time_us):
        if type(simulation_time_us) is not int or simulation_time_us<self.last_tick:raise ValueError('pacing ticks must be nondecreasing')
        self.last_tick=simulation_time_us
        if self.publisher is not None:self.publisher.flush_due()
        if self.rate is None:return 0.0
        while True:
            target=self.origin+float(Fraction(simulation_time_us,1000000)/self.rate)+self.pause_s
            now=_number(self.clock(),positive=False)
            if now>=target:
                late=now-target;self.max_lateness_s=max(self.max_lateness_s,late);return late
            if self.publisher is not None:self.publisher.wait_until(target)
            else:
                self.sleep(target-now)
                if self.clock()<=now:raise PublicationFailure('pacing sleep did not advance monotonic time')
