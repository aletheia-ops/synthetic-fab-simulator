import json
from copy import deepcopy
import pytest
from fab_sim.output import Output
from fab_sim.progress import Progress
from tools.measure_run import summarize_evidence


def event(tick):
    return {'simulation_time_us':tick,'event_type':'test','payload':{},'entity_refs':{}}


def test_resume_segment_continues_global_sequence(tmp_path):
    parent=Output(tmp_path/'parent','parent',{})
    parent.commit([event(10),event(11)]);watermark=parent.checkpoint_watermark()
    parent.commit([event(12)]);parent.finalize({'outcome':'Incomplete'})
    child=Output(tmp_path/'child','child',{'continuation':watermark},base_sequence=2,base_time_us=11)
    child.commit([event(11),event(15)]);manifest=child.finalize({'outcome':'Completed'})
    rows=[json.loads(line) for line in (tmp_path/'child/events.jsonl').read_text().splitlines()]
    assert [r['event_sequence'] for r in rows]==[3,4]
    assert manifest['record_counts']['events']==2
    assert manifest['base_sequence']==2
    assert watermark['last_committed_sequence']==2
    assert summarize_evidence(tmp_path/'child',4,base_sequence=2)['event_count']==2
    with pytest.raises(FileExistsError):Output(tmp_path/'child','other',{})


def test_first_exit_history_is_cumulative():
    lots={'l':{'product_id':'p','admitted_at_us':0,'exited_at_us':None}}
    observer=Progress(lots,{},['p']);lots['l']['exited_at_us']=15;observer.observe('lot_exited',{'lot_id':'l'},15)
    clone=Progress(lots,{},['p']);clone.restore_state(observer.export_state())
    assert clone.snapshot(20)['first_exit_at_us']=={'p':15}
    assert clone.snapshot(20)['products']['p']['exited']==1
    assert clone.exited_count==1


def test_watermark_durability_failure_never_returns_prefix(tmp_path,monkeypatch):
    import os
    output=Output(tmp_path/'parent','parent',{});output.commit([event(10)])
    monkeypatch.setattr(os,'fsync',lambda fd:(_ for _ in ()).throw(OSError('injected durability failure')))
    with pytest.raises(OSError):output.checkpoint_watermark()
    for stream in output.files.values():stream.close()
