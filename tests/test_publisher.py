"""Required publication uses bounded immutable batches and explicit acknowledgements."""
from copy import deepcopy
import pytest
from fab_sim.publisher import Publisher,PublicationFailure


class Clock:
    def __init__(self):self.now=0.0;self.sleeps=[]
    def __call__(self):return self.now
    def sleep(self,seconds):self.sleeps.append(seconds);self.now+=seconds


class Adapter:
    def __init__(self,responses=(),clock=None,cost=0):self.responses=iter(responses);self.calls=[];self.clock=clock;self.cost=cost
    def publish(self,batch,*,timeout_s):
        self.calls.append((deepcopy(batch),timeout_s))
        if self.clock:self.clock.now+=self.cost
        answer=next(self.responses,None)
        if isinstance(answer,Exception):raise answer
        return answer if answer is not None else {'status':'ok','accepted_ids':[r['event_id'] for r in batch]}


def record(sequence,payload=None):
    return {'schema_version':'test','run_id':'r','event_sequence':sequence,'event_id':f'r:{sequence}',
        'simulation_time_us':sequence,'event_type':'actual_transition','entity_refs':{},'payload':payload or {}}


def publisher(adapter=None,clock=None,**settings):
    clock=clock or Clock();adapter=adapter or Adapter()
    return Publisher(adapter,clock=clock,sleep=clock.sleep,**settings),adapter,clock


def ids(adapter):return [[r['event_id'] for r in batch] for batch,timeout in adapter.calls]


def test_partial_ack_retries_only_unresolved_and_never_overtakes_active_batch():
    a=Adapter([{'status':'retryable','accepted_ids':['r:1']},{'status':'ok','accepted_ids':['r:2']}])
    p,a,c=publisher(a,batch_records=2,max_records=3,initial_delay_s=.25,cap_delay_s=1)
    p.submit(record(i) for i in range(1,5));p.drain()
    assert ids(a)==[['r:1','r:2'],['r:2'],['r:3','r:4']] and c.sleeps==[.25]
    assert p.accepted_count==4 and p.pending_ids==() and p.high_water_records<=3


def test_unknown_acceptance_keeps_identity_and_factor_two_capped_backoff():
    a=Adapter([TimeoutError('unknown'),{'status':'retryable','accepted_ids':[]},{'status':'retryable','accepted_ids':[]}])
    p,a,c=publisher(a,batch_records=1,initial_delay_s=.25,cap_delay_s=.5,max_attempts=4)
    p.submit([record(1)])
    assert ids(a)==[['r:1']]*4 and c.sleeps==[.25,.5,.5] and p.accepted_count==1


def test_attempt_exhaustion_retains_unacknowledged_record_without_sending_next():
    a=Adapter([{'status':'ok','accepted_ids':[]}]*3)
    p,a,c=publisher(a,batch_records=1,max_attempts=2)
    with pytest.raises(PublicationFailure,match='attempt'):p.submit([record(1),record(2)])
    assert ids(a)==[['r:1'],['r:1']] and p.pending_ids==('r:1',)
    with pytest.raises(PublicationFailure):p.drain()


def test_retry_after_is_respected_and_never_shortened_to_fit_budget():
    a=Adapter([{'status':'retryable','accepted_ids':[],'retry_after_s':2}])
    p,a,c=publisher(a,batch_records=1,retry_budget_s=5,initial_delay_s=.25)
    p.submit([record(1)]);assert c.sleeps==[2]
    a=Adapter([{'status':'retryable','accepted_ids':[],'retry_after_s':20}])
    p,a,c=publisher(a,batch_records=1,retry_budget_s=5)
    with pytest.raises(PublicationFailure,match='budget'):p.submit([record(1)])
    assert len(a.calls)==1 and not c.sleeps and p.pending_ids==('r:1',)


def test_attempt_timeout_capped_by_remaining_retry_budget_including_calls_and_waits():
    c=Clock();a=Adapter([{'status':'retryable','accepted_ids':[]}],c,cost=1)
    p,a,c=publisher(a,c,batch_records=1,retry_budget_s=3,attempt_timeout_s=2,initial_delay_s=.5)
    p.submit([record(1)]);assert [timeout for batch,timeout in a.calls]==[2,1.5]
    assert c.now==2.5


def test_drain_deadline_caps_attempt_timeout_and_includes_call_time():
    c=Clock();a=Adapter(clock=c,cost=.75)
    p,a,c=publisher(a,c,batch_records=2,drain_timeout_s=1)
    p.submit([record(1)])
    # A queued record is drained with the remaining deadline passed to adapter.
    p.drain();assert a.calls[0][1]==1 and p.pending_ids==()


def test_adapter_overrun_fails_even_if_it_eventually_acknowledges():
    c=Clock();a=Adapter(clock=c,cost=2)
    p,a,c=publisher(a,c,batch_records=2,drain_timeout_s=1)
    p.submit([record(1)])
    with pytest.raises(PublicationFailure,match='timeout|deadline'):p.drain()
    assert p.accepted_count==1 and p.failed and c.now==2


def test_queue_byte_bound_record_limit_and_streaming_admission():
    p,a,c=publisher(max_records=4,max_bytes=1000,batch_records=3,batch_bytes=800,max_record_bytes=500)
    def records():
        for i in range(1,21):
            if i==6:assert a.calls
            yield record(i,{'text':'é'*60})
    p.submit(records());p.drain()
    assert p.accepted_count==20 and p.high_water_bytes<=1000 and p.high_water_records<=4
    p,a,c=publisher(max_bytes=500,batch_bytes=500,max_record_bytes=500)
    with pytest.raises(PublicationFailure,match='maximum'):p.submit([record(1,{'large':'x'*1000})])
    assert not a.calls and not p.pending_ids


def test_interval_flush_is_serviced_during_wall_clock_wait_without_domain_events():
    p,a,c=publisher(batch_records=10,flush_interval_s=.5)
    p.submit([record(1)]);assert not a.calls and p.next_flush_at==.5
    p.wait_until(2);assert len(a.calls)==1 and c.sleeps==[.5,1.5] and p.pending_ids==()


def test_adapter_mutation_cannot_rewrite_retained_retry_payload():
    class Mutating:
        def __init__(self):self.payloads=[]
        def publish(self,batch,*,timeout_s):
            self.payloads.append(deepcopy(batch[0]['payload']));batch[0]['payload']['x']='corrupt'
            return {'status':'retryable' if len(self.payloads)==1 else 'ok','accepted_ids':[] if len(self.payloads)==1 else ['r:1']}
    a=Mutating();p,a,c=publisher(a,batch_records=1);source=record(1,{'x':'original'});p.submit([source])
    assert a.payloads==[{'x':'original'},{'x':'original'}] and source['payload']['x']=='original'


@pytest.mark.parametrize('answer',[{'status':'ok','accepted_ids':['other']},{'status':'ok','accepted_ids':['r:1','r:1']},
    {'status':'permanent','accepted_ids':[],'message':'denied'},{'status':'retryable','accepted_ids':[],'retry_after_s':float('nan')}])
def test_invalid_or_permanent_response_is_terminal(answer):
    p,a,c=publisher(Adapter([answer]),batch_records=1)
    with pytest.raises(PublicationFailure):p.submit([record(1)])
    assert p.pending_ids==('r:1',) and len(a.calls)==1


def test_new_submissions_cannot_reuse_or_skip_committed_event_ids():
    p,a,c=publisher(batch_records=1);p.submit([record(1)])
    with pytest.raises(PublicationFailure):p.submit([record(1)])
    assert p.accepted_count==1 and len(a.calls)==1


def test_local_output_completion_requires_acknowledged_drain_and_retains_same_ids(tmp_path):
    import json
    from fab_sim.output import Output
    from test_output import event
    p,a,c=publisher(batch_records=10)
    out=Output(tmp_path/'run','r',{},publisher=p)
    out.commit([event(0),event(1)])
    assert not a.calls and p.pending_ids==('r:1','r:2')
    manifest=out.finalize({'outcome':'Completed','constraint_status':'satisfied'})
    assert ids(a)==[['r:1','r:2']] and manifest['publication']['accepted_count']==2
    assert [json.loads(x)['event_id'] for x in (tmp_path/'run/events.jsonl').read_text().splitlines()]==['r:1','r:2']
    assert manifest['output_status']=='complete'


def test_publication_failure_keeps_last_local_commit_and_never_writes_later_buffered_records(tmp_path):
    import json
    from fab_sim.output import Output,OutputFailure
    from test_output import event
    p,a,c=publisher(Adapter([{'status':'permanent','accepted_ids':[]}]),batch_records=1)
    out=Output(tmp_path/'run','r',{},publisher=p)
    with pytest.raises(OutputFailure):out.commit([event(0),event(1)])
    rows=[json.loads(x) for x in (tmp_path/'run/events.jsonl').read_text().splitlines()]
    assert [r['event_id'] for r in rows]==['r:1'] and out.last_committed_sequence==1
    manifest=json.loads((tmp_path/'run/manifest.json').read_text())
    assert manifest['outcome']=='Failed' and manifest['publication']['pending_ids']==['r:1']
    with pytest.raises(OutputFailure):out.finalize({'outcome':'Completed'})


def test_failed_required_publication_drain_keeps_known_constraint_evidence(tmp_path):
    import json
    from fab_sim.output import Output,OutputFailure
    from test_output import event
    p,a,c=publisher(Adapter([{'status':'retryable','accepted_ids':[]}]),batch_records=10,max_attempts=1)
    out=Output(tmp_path/'run','r',{},publisher=p);out.commit([event()])
    with pytest.raises(OutputFailure):out.finalize({'outcome':'Completed','constraint_status':'violated','violation_ids':['q1']})
    manifest=json.loads((tmp_path/'run/manifest.json').read_text())
    assert manifest['outcome']=='Failed' and manifest['constraint_status']=='violated' and manifest['violation_ids']==['q1']


def test_exact_attempt_deadline_at_nonzero_clock_is_not_a_rounding_overrun():
    c=Clock();c.now=.2;a=Adapter(clock=c,cost=.1)
    p,a,c=publisher(a,c,batch_records=1,attempt_timeout_s=.1)
    p.submit([record(1)]);assert p.accepted_count==1 and not p.failed


def test_early_sleep_return_cannot_shorten_retry_after():
    c=Clock();a=Adapter([{'status':'retryable','accepted_ids':[],'retry_after_s':.25}])
    p,a,c=publisher(a,c,batch_records=1)
    def early(seconds):c.now+=min(seconds,.1)
    p.sleep=early;p.submit([record(1)])
    assert c.now>=.25 and len(a.calls)==2


def test_commit_to_publication_delay_includes_blocked_queue_admission():
    c=Clock();a=Adapter(clock=c,cost=1.2)
    p,a,c=publisher(a,c,batch_records=10,flush_interval_s=.1)
    p.submit([record(1)]);c.now=.1
    p.submit([record(2)]);p.drain()
    assert p.metrics()['max_commit_to_publication_s']==pytest.approx(2.4)
