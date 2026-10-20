import json
from fractions import Fraction
import pytest
from fab_sim.output import Output,OutputFailure


def event(t=0,payload=None):
    return {'simulation_time_us':t,'event_type':'visit_started','entity_refs':{'lot':['l1']},'payload':payload or {}}


def read(path):return [json.loads(line) for line in path.read_text().splitlines()]


def test_streamed_commit_has_one_identity_owner_exact_time_and_bounded_chunks(tmp_path):
    out=Output(tmp_path/'run','r',{'sources':{'sha':'source'}},chunk_records=2,chunk_bytes=1024,max_record_bytes=512)
    def records():
        for i in range(10):
            if i==3:assert out.last_committed_sequence>=2
            yield event(i,{'pose':Fraction(1,3)})
    out.commit(records());rows=read(tmp_path/'run/events.jsonl')
    assert [r['event_id'] for r in rows]==[f'r:{i}' for i in range(1,11)]
    assert rows[3]['simulation_time_s']==0.000003 and rows[0]['payload']['pose']=={'numerator':1,'denominator':3}
    assert out.high_water_records<=2 and out.high_water_bytes<=1024
    assert out.last_committed_sequence==10 and out.last_committed_time_us==9
    result=out.finalize({'outcome':'Incomplete','constraint_status':'unassessed','reason':'operator_stop'})
    assert result['output_status']=='complete' and result['sources']=={'sha':'source'}
    assert result['record_counts']=={'events':10,'oht':0,'diagnostics':0}
    assert (tmp_path/'run/oht.jsonl').read_bytes()==b'' and (tmp_path/'run/diagnostics.jsonl').read_bytes()==b''
    assert json.loads((tmp_path/'run/manifest.json').read_text())==result
    with pytest.raises(OutputFailure):out.commit([event()])


def test_prior_attempt_is_never_overwritten(tmp_path):
    out=Output(tmp_path/'run','one',{});out.finalize({'outcome':'Incomplete'})
    before=(tmp_path/'run/manifest.json').read_bytes()
    with pytest.raises(FileExistsError):Output(tmp_path/'run','two',{})
    assert (tmp_path/'run/manifest.json').read_bytes()==before


@pytest.mark.parametrize('payload',[{'bad':float('nan')},{'bad':float('inf')},{1:'nonstring key'},{'oversize':'é'*3000}])
def test_bad_record_fails_output_but_flushes_prior_valid_records(tmp_path,payload):
    out=Output(tmp_path/'run','r',{},chunk_records=3,chunk_bytes=1024,max_record_bytes=512)
    with pytest.raises(OutputFailure):out.commit(iter([event(0),event(1,payload)]))
    assert len(read(tmp_path/'run/events.jsonl'))==1 and out.last_committed_sequence==1
    manifest=json.loads((tmp_path/'run/manifest.json').read_text())
    assert manifest['outcome']=='Failed' and manifest['output_status']=='failed'
    assert manifest['last_committed_sequence']==1
    with pytest.raises(OutputFailure):out.finalize({'outcome':'Completed'})


def test_iterator_failure_preserves_prior_commits_and_stops(tmp_path):
    out=Output(tmp_path/'run','r',{})
    def broken():
        yield event(0)
        raise RuntimeError('producer failed')
    with pytest.raises(OutputFailure,match='producer failed'):out.commit(broken())
    assert len(read(tmp_path/'run/events.jsonl'))==1


def test_partial_disk_write_retains_valid_prefix_and_truthful_manifest(tmp_path):
    out=Output(tmp_path/'run','r',{});out.commit([event()])
    underlying=out.files['events']
    class Broken:
        def write(self,data):underlying.write(data[:10]);raise OSError('disk full')
        def __getattr__(self,name):return getattr(underlying,name)
    out.files['events']=Broken()
    with pytest.raises(OutputFailure,match='disk full'):out.commit([event(1)])
    assert len(read(tmp_path/'run/events.jsonl'))==1 and out.last_committed_sequence==1
    assert json.loads((tmp_path/'run/manifest.json').read_text())['outcome']=='Failed'


def test_required_drain_failure_cannot_finalize_completed(tmp_path):
    out=Output(tmp_path/'run','r',{});out.commit([event()])
    with pytest.raises(OutputFailure,match='drain'):out.finalize({'outcome':'Completed'},drain_complete=False)
    assert json.loads((tmp_path/'run/manifest.json').read_text())['outcome']=='Failed'


def test_manifest_replace_failure_preserves_initial_manifest_and_reports_error(tmp_path,monkeypatch):
    out=Output(tmp_path/'run','r',{});out.commit([event()])
    def fail(*args):raise OSError('replace unavailable')
    monkeypatch.setattr('fab_sim.output.os.replace',fail)
    with pytest.raises(OutputFailure,match='replace unavailable'):out.finalize({'outcome':'Completed'})
    assert json.loads((tmp_path/'run/manifest.json').read_text())['outcome'] is None
    assert out.status=='failed' and out.last_committed_sequence==1


@pytest.mark.parametrize('record',[{'simulation_time_us':True,'event_type':'x','payload':{}},
    {'simulation_time_us':0,'event_type':'x','payload':{},'event_id':'forged'},
    {'simulation_time_us':0,'event_type':'x','payload':{},'entity_refs':{'lot':[None]}}])
def test_invalid_event_envelope_fails_without_assigning_identity(tmp_path,record):
    out=Output(tmp_path/'run','r',{})
    with pytest.raises(OutputFailure):out.commit([record])
    assert out.last_committed_sequence==0


def test_time_cannot_go_backwards_between_commits(tmp_path):
    out=Output(tmp_path/'run','r',{});out.commit([event(2)])
    with pytest.raises(OutputFailure):out.commit([event(1)])
    assert len(read(tmp_path/'run/events.jsonl'))==1


def test_final_output_failure_keeps_known_constraints_and_domain_progress(tmp_path):
    out=Output(tmp_path/'run','r',{});out.commit([event(10)])
    with pytest.raises(OutputFailure):out.finalize({'outcome':'Completed','constraint_status':'violated',
        'violation_ids':['clock:1'],'unresolved_obligations':['clock:2'],'exited_count':2},drain_complete=False)
    manifest=json.loads((tmp_path/'run/manifest.json').read_text())
    assert manifest['outcome']=='Failed' and manifest['constraint_status']=='violated'
    assert manifest['violation_ids']==['clock:1'] and manifest['exited_count']==2


def test_reentrant_commit_cannot_duplicate_ids_or_reorder_committed_events(tmp_path):
    out=Output(tmp_path/'run','r',{})
    def nested():
        yield event(1)
        out.commit([event(2)])
        yield event(3)
    with pytest.raises(OutputFailure,match='reentrant'):out.commit(nested())
    rows=read(tmp_path/'run/events.jsonl')
    assert [r['event_id'] for r in rows]==['r:1'] and rows[0]['simulation_time_us']==1


def test_diagnostic_writes_do_not_consume_event_identity(tmp_path):
    out=Output(tmp_path/'run','r',{});out.commit([event()])
    out.write_diagnostics([{'diagnostic_type':'runtime_failure'}])
    out.commit([event(1)]);assert [r['event_id'] for r in read(tmp_path/'run/events.jsonl')]==['r:1','r:2']
    out.finalize({'outcome':'Incomplete'})


def test_oht_records_share_the_event_sequence_and_time_order(tmp_path):
    out=Output(tmp_path/'run','r',{});out.commit([event(1)])
    out.commit_oht([event(1)]);out.commit([event(2)]);out.commit_oht([event(3)])
    events=read(tmp_path/'run/events.jsonl');oht=read(tmp_path/'run/oht.jsonl')
    assert [r['event_sequence'] for r in events]==[1,3] and [r['event_sequence'] for r in oht]==[2,4]
    assert out.last_committed_sequence==4 and out.last_committed_time_us==3
    with pytest.raises(OutputFailure):out.commit_oht([event(2)])


def test_reentrant_finalization_never_closes_or_completes_a_pending_commit(tmp_path):
    out=Output(tmp_path/'run','r',{});returned=[]
    def nested():
        yield event(1)
        returned.append(out.finalize({'outcome':'Completed'}))
    with pytest.raises(OutputFailure,match='reentrant'):out.commit(nested())
    assert returned==[] and [r['event_id'] for r in read(tmp_path/'run/events.jsonl')]==['r:1']
    assert json.loads((tmp_path/'run/manifest.json').read_text())['outcome']=='Failed'


def test_actual_manufacturing_transfer_traffic_events_export_without_losing_identity(tmp_path):
    from test_amhs_integration import factory
    traffic,transfers,m,s,c,lots,vehicles,events=factory()
    out=Output(tmp_path/'run','physical',{},chunk_records=3,chunk_bytes=4096,max_record_bytes=4096)
    c.reserve('entry','admit');c.place('clow','entry','admit');visit=m.enter_visit('low','1')
    transfers.request('clow',visit,('port',))
    while True:
        s.settle(s.now_us)
        out.commit({'simulation_time_us':s.now_us,'event_type':kind,'entity_refs':{},'payload':payload} for kind,payload in events)
        events.clear()
        if s.next_time_us is None:break
        s.settle(s.next_time_us)
    assert lots['low']['route_complete_at_us']==1130035
    out.finalize({'outcome':'Incomplete','reason':'fixture_has_no_exit_adapter'})
    rows=read(tmp_path/'run/events.jsonl');kinds={r['event_type'] for r in rows}
    assert {'traffic_hop_started','transfer_handling_started','carrier_exchange_completed'}<=kinds
    assert [r['event_sequence'] for r in rows]==list(range(1,len(rows)+1))
    assert out.high_water_bytes<=4096 and out.high_water_records<=3
