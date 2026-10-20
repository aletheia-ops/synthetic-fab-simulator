import json
from copy import deepcopy
from pathlib import Path
import pytest
from test_runner import fixture
from test_preflight import material


def test_pending_demands_are_never_reported_as_loaded():
    from test_transfers import factory
    t,m,s,c,lots,vehicles,events=factory()
    t.demands={f'd:{i}':{'id':f'd:{i}','status':'pending'} for i in range(104)}
    t.active={f'carrier:{i}':f'd:{i}' for i in range(104)}
    assert t.transfer_counts()=={'pending_transfer_count':104,'committed_transfer_count':0,'loaded_transfer_count':0}


def test_loaded_count_tracks_actual_pickup_and_drop():
    from test_handoffs import exit_fixture
    from test_transfers import arrive
    plans,t,m,s,c,lots,vehicles,events,did=exit_fixture();s.settle(s.now_us);s.settle(s.now_us+1000000)
    assert [r['demand_id'] for r in plans.describe(active_only=True)]==[did]
    assert t.transfer_counts()['committed_transfer_count']==1 and t.transfer_counts()['loaded_transfer_count']==0
    arrive(t,vehicles,did,'pickup');s.settle(s.now_us+10)
    assert t.transfer_counts()['loaded_transfer_count']==1
    assert [r['demand_id'] for r in plans.describe(active_only=True)]==[did]
    arrive(t,vehicles,did,'drop');s.settle(s.now_us+10)
    assert t.transfer_counts()=={'pending_transfer_count':0,'committed_transfer_count':0,'loaded_transfer_count':0}
    assert plans.describe(active_only=True)==[]
    assert plans.describe()[0]['phase']=='completed'


def test_canonical_policy_projection_and_observer_on_off_are_identical(tmp_path):
    from fab_sim.runner import run
    path=fixture(tmp_path);observations=[]
    first=run(path,progress=observations.append)
    assert first['transport_policy']=='reserved_handoff_clearance_v2'
    assert first['clearance_pool']['required_capacity']==0
    assert first['loaded_transfer_count']==first['committed_transfer_count']==first['pending_transfer_count']==0
    assert observations and all('pending_transfer_count' in r and 'active_handoffs' in r for r in observations)
    first_events=(tmp_path/'result/events.jsonl').read_bytes()
    config=json.loads(path.read_text());config['output']['directory']='unobserved';path.write_text(json.dumps(config))
    second=run(path)
    # Event envelopes include output namespaces; compare ordered domain payloads.
    ordered=lambda data:[(r['simulation_time_us'],r['event_type'],r['payload']) for r in map(json.loads,data.splitlines())]
    assert ordered(first_events)==ordered((tmp_path/'unobserved/events.jsonl').read_bytes())
    assert first['domain_digest']==second['domain_digest']


@pytest.mark.parametrize('incompatible',[False,True])
def test_required_clearance_rejection_precedes_output_creation(tmp_path,incompatible):
    from fab_sim.preflight import assess
    path,config,m,top=material(tmp_path)
    m['model']['routes'][0]['steps'][0].update(batch_min=25,batch_max=25,processing_unit='batch')
    buffers=[d for d in top['model']['docks'] if d['kind']=='track_buffer']
    if incompatible:
        for d in buffers:d['compatible_products']=['other']
    else:top['model']['docks'].remove(buffers[0])
    result=assess(config,m,top)
    assert result['status']=='rejected'
    assert any(f['code']=='clearance_capacity_unavailable' for f in result['findings'])
    from fab_sim.inputs import bind_validation,canonical_text
    from fab_sim.sources.smt import VALIDATOR as MV
    from fab_sim.sources.smat import VALIDATOR as TV,traffic_requirements
    from fab_sim.runner import run
    m['model']['tool_groups'][0]['batching']=True
    m=bind_validation(m,{'tool_groups':['A']},MV)
    top=bind_validation(top,traffic_requirements(m),TV)
    (tmp_path/'manufacturing.json').write_text(canonical_text(m))
    (tmp_path/'topology.json').write_text(canonical_text(top))
    with pytest.raises(ValueError,match='clearance_capacity_unavailable'):run(path)
    assert not (tmp_path/'result').exists()


def test_projection_includes_plan_and_lease_state_once_and_ignores_callbacks():
    from test_handoffs import exit_fixture
    from fab_sim.probes import state_projection,domain_digest
    plans,t,m,s,c,lots,vehicles,events,did=exit_fixture();assert plans.prepare(did,'rack')
    state={'transfers':t,'manufacturing':m}
    digest=lambda:domain_digest(state_projection(state))
    initial=digest();plans.records[did]['cancel_requested']=True;assert digest()!=initial
    before=digest();t.emit=lambda k,p:None;m.buffer_owner=lambda carrier,slot:None
    assert digest()==before
    lease={'id':'lease','tool_id':'t','kind':'setup_change','members':[],'slots':[],'status':'active'}
    t.clearance.records['lease']=lease;assert digest()!=before
    projected=json.dumps(state_projection(state),default=str)
    assert projected.count('ClearancePool')==1 and projected.count('HandoffPlans')==1


def test_committed_unloaded_handoff_cannot_be_ignored_by_blockage_proof():
    from fab_sim.outcomes import classify_stop
    from test_outcomes import state
    value=state();value.update(now_us=5,exited_count=0,wip_count=2,committed_transfer_count=1,
        remaining_lot_ids=['a','b'],relevant_future_events=[],blockage_proofs=[{
            'kind':'resource_unavailable','assessment_complete':True,'covered_lot_ids':['a','b'],
            'required_condition':'delivery','possible_producers':[]}])
    assert not classify_stop(value)['stop_domain']


def test_no_vehicle_handoff_reports_resource_wait_without_claims():
    from test_handoffs import exit_fixture
    plans,t,m,s,c,lots,vehicles,events,did=exit_fixture();vehicles['v1']['available']=False
    assert not plans.prepare(did,'rack')
    assert plans.waits[did]['reason']=='handoff_resource_unavailable'
    assert c.locations['clow']=='rack' and c.slots['exit']['reservation'] is None


def test_clearance_rejects_buffers_on_disconnected_directed_rails(tmp_path):
    from fab_sim.preflight import assess
    path,config,m,top=material(tmp_path)
    m['model']['routes'][0]['steps'][0].update(batch_min=25,batch_max=25,processing_unit='batch')
    rail=dict(top['model']['rails'][0],id='isolated',from_node='isolated-a',to_node='isolated-b')
    top['model']['rails'].append(rail)
    for d in top['model']['docks']:
        if d['kind']=='track_buffer':d.update(rail_id='isolated',offset_um=100)
    result=assess(config,m,top)
    assert result['status']=='rejected' and any(f['code']=='clearance_capacity_unavailable' for f in result['findings'])


def test_arrived_members_of_a_committed_campaign_are_never_clearance_candidates():
    from test_campaigns import factory
    from test_batch import move
    from fab_sim.clearance import ClearancePool
    campaigns,t,m,s,c,lots,vehicles,events=factory();assert campaigns.dispatch()
    record=campaigns.records[campaigns.active_by_tool['t']]
    for member in record['members'][:5]:
        move(c,member['carrier_id'],member['rack_slot_id'],record['id'],True)
        m.delivered(member['carrier_id'],member['rack_slot_id'])
    pool=ClearancePool(m,t,['buffer'],lambda k,p:None)
    assert not pool.dispatch() and pool.describe()==[]
    assert record['status']=='staging' and not m.executions


def test_unset_recipe_work_before_positive_setup_requires_clearance(tmp_path):
    from fab_sim.preflight import assess
    from test_route_rules import step
    path,config,m,top=material(tmp_path)
    m['model']['routes'][0]['steps'].append(step(2,setup={'id':'B','when':'need'}))
    m['model']['setup_transitions']=[{'current':None,'new':'B','duration_us':4,'minimum_runs':2}]
    result=assess(config,m,top)
    assert result['clearance_pool']['setup_bound']==2 and result['clearance_pool']['required_capacity']==3
