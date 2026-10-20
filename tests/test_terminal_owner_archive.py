from copy import deepcopy
from types import SimpleNamespace
import pytest
from fab_sim.history_archive import HistoryArchive
from fab_sim.probes import state_projection,domain_digest

def test_terminal_clearance_keeps_active_and_referenced_leases(tmp_path):
    from test_clearance import lease_fixture
    from fab_sim.history_archive import retire_clearance_records,logical_records
    pool,t,m,*_=lease_fixture();pool.dispatch()
    active=next(iter(pool.records.values()))
    for i in range(130):
        identity='old:'+str(i)
        pool.records[identity]={**deepcopy(active),'id':identity,'status':'completed'}
        for member in pool.records[identity]['members']:member['status']='departed'
    t.handoffs=SimpleNamespace(current_records=lambda:({'lease_id':'old:0'},))
    original=deepcopy(pool.records);before=domain_digest(state_projection(pool))
    archive=HistoryArchive(tmp_path)
    while retire_clearance_records(pool,archive):pass
    assert set(pool.records)=={active['id'],'old:0'}
    assert logical_records(pool,'clearance-lease')==original
    assert domain_digest(state_projection(pool))==before
    assert pool.admission(active['members'][0]['visit_id'])
    t.handoffs.current_records=lambda:()
    assert retire_clearance_records(pool,archive)==1
    assert logical_records(pool,'clearance-lease')==original

def test_completed_campaign_archive_preserves_all_fields_and_active_membership(tmp_path):
    from fab_sim.campaigns import SetupCampaigns
    from fab_sim.history_archive import retire_campaign_records,logical_records
    owner=SetupCampaigns.__new__(SetupCampaigns)
    owner.records={};owner.history_archive=None;owner.active_by_tool={};owner.visit_owners={}
    for i in range(130):
        identity='campaign:'+str(i)
        owner.records[identity]={'id':identity,'tool_id':'tool','setup_id':'recipe',
            'status':'completed','members':[{'visit_id':str(i),'carrier_id':'carrier',
                'rack_slot_id':'rack','arrived':True,'completed':True}],
            'committed_at_us':i,'completed_at_us':i+10}
    owner.records['active']={**deepcopy(owner.records['campaign:0']),'id':'active','status':'serving'}
    owner.active_by_tool['tool']='active';owner.visit_owners['0']='active'
    original=deepcopy(owner.records);before=domain_digest(state_projection(owner))
    archive=HistoryArchive(tmp_path)
    while retire_campaign_records(owner,archive):pass
    assert set(owner.records)=={'active'} and owner.active_by_tool=={'tool':'active'}
    assert logical_records(owner,'setup-campaign')==original
    assert domain_digest(state_projection(owner))==before

def test_lease_archive_failure_preserves_canonical_records(tmp_path,monkeypatch):
    from test_clearance import lease_fixture
    from fab_sim.history_archive import retire_clearance_records
    pool,*_=lease_fixture();pool.dispatch();record=next(iter(pool.records.values()))
    record['status']='completed'
    for member in record['members']:member['status']='departed'
    pool._retire_record(record);before=deepcopy(pool.records)
    archive=HistoryArchive(tmp_path)
    monkeypatch.setattr(archive,'append',lambda *a: (_ for _ in ()).throw(OSError('disk failure')))
    with pytest.raises(OSError):retire_clearance_records(pool,archive)
    assert pool.records==before and pool.history_archive is None

def test_native_terminal_owner_restore_does_not_hydrate_history(tmp_path):
    from test_runner import fixture
    from test_checkpoint_runtime_state import advance
    from fab_sim.runner import build_runtime,load_config
    from fab_sim.runtime_state import capture_runtime,restore_runtime,runtime_digest
    from fab_sim.history_archive import retire_campaign_records,retire_clearance_records
    config=load_config(fixture(tmp_path));events=[];restored=[]
    owner=build_runtime(config,lambda k,p:events.append((owner.scheduler.now_us,k,deepcopy(p))),initialize=True)
    owner.scheduler.settle(owner.scheduler.next_time_us)
    owner.clearance.records['past']={'id':'past','tool_id':'tool','kind':'setup_change',
        'members':[],'slots':[],'status':'completed'}
    owner.campaigns.records['past']={'id':'past','tool_id':'tool','setup_id':'S',
        'members':[],'status':'completed','committed_at_us':0,'completed_at_us':0}
    before=runtime_digest(owner)
    assert retire_campaign_records(owner.campaigns,HistoryArchive(tmp_path/'history'))==1
    assert retire_clearance_records(owner.clearance,HistoryArchive(tmp_path/'history'))==1
    assert runtime_digest(owner)==before
    state=capture_runtime(owner);events.clear()
    clone=build_runtime(config,lambda k,p:restored.append((clone.scheduler.now_us,k,deepcopy(p))),initialize=False)
    restore_runtime(clone,state,clone.actions)
    assert not clone.clearance.records and not clone.campaigns.records
    advance(owner);advance(clone)
    assert events==restored and runtime_digest(owner)==runtime_digest(clone)
    assert owner.streams.export_state()==clone.streams.export_state()


def test_final_summary_explicitly_exposes_archived_clearance(tmp_path,monkeypatch):
    from test_runner import fixture
    import fab_sim.runner as runner
    from fab_sim.checkpoint import STATE_CONTRACT
    original=runner.build_runtime;expected={}
    def seeded(*args,**kwargs):
        owner=original(*args,**kwargs)
        for i in range(130):
            identity=f'clearance:{i+1:08}'
            record={'id':identity,'tool_id':'tool','kind':'setup_change',
                'members':[],'slots':[],'status':'completed'}
            expected[identity]=deepcopy(record);owner.clearance.records[identity]=record
        owner.clearance.sequence=130
        return owner
    monkeypatch.setattr(runner,'build_runtime',seeded)
    result=runner.run(fixture(tmp_path))
    assert result['outcome']=='Completed'
    assert result['domain_digest_version']=='full-physical-state-archive-v9'
    assert STATE_CONTRACT=='fab-native-checkpoint-archive-v11'
    history=result['clearance_history']
    assert history['representation']=='active-plus-archive-v1'
    assert history['ledger']['records']==130 and history['archive']['records']==130
    assert result['clearance_leases']==[]
    actual={item['id']:item['record'] for item in HistoryArchive(history['archive']['directory']).records(history['archive'])}
    assert actual==expected
