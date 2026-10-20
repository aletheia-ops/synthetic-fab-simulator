from copy import deepcopy
from test_runner import fixture
from test_maintenance import duration
from test_checkpoint_runtime_state import advance
from fab_sim.runner import build_runtime,load_config
from fab_sim.runtime_state import capture_runtime,restore_runtime,runtime_digest
from fab_sim.history_archive import HistoryArchive

def test_recovered_cause_archive_preserves_future_binding_and_resume(tmp_path):
    from fab_sim.history_archive import retire_recovered_causes
    definition={'id':'pm','scope':'toolgroup','scope_id':'A','type':'calendar','interval':1000000000,
        'first':duration(0),'duration':duration(10)}
    config=load_config(fixture(tmp_path,model_updates={'maintenance':[definition]}))
    before=[];after=[]
    owner=build_runtime(config,lambda k,p:before.append((owner.scheduler.now_us,k,deepcopy(p))),initialize=True)
    for _ in range(1000):
        owner.scheduler.settle(owner.scheduler.next_time_us)
        if any(c['status']=='recovered' for c in owner.maintenance.causes.values()):break
    maintenance=owner.maintenance
    digest=runtime_digest(owner);all_before=deepcopy(maintenance.causes)
    assert retire_recovered_causes(maintenance,HistoryArchive(tmp_path/'archive'))==1
    assert all(c['status']!='recovered' for c in maintenance.causes.values())
    assert runtime_digest(owner)==digest
    archived=list(HistoryArchive(tmp_path/'archive').records(maintenance.history_archive))
    source=next(c for c in all_before.values() if c['status']=='recovered')
    body=archived[0]['record'];assert body.keys()==source.keys()
    assert body['binding']==(source['tool_id'],source['kind'],source['definition_id'])
    assert {k:v for k,v in body.items() if k!='binding'}=={k:v for k,v in source.items() if k!='binding'}
    state=capture_runtime(owner);before.clear()
    clone=build_runtime(config,lambda k,p:after.append((clone.scheduler.now_us,k,deepcopy(p))),initialize=False)
    restore_runtime(clone,state,clone.actions)
    # Compare a fixed future window, including a new periodic cause. Neither
    # copy needs to hydrate the already recovered cause to start/recover it.
    for _ in range(100):
        owner.scheduler.settle(owner.scheduler.next_time_us)
        clone.scheduler.settle(clone.scheduler.next_time_us)
    assert before==after and runtime_digest(owner)==runtime_digest(clone)
    assert owner.streams.export_state()==clone.streams.export_state()
