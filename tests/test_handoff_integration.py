"""Physical Traffic/exchange/processing regressions for committed handoffs."""
from copy import deepcopy
import pytest
from fab_sim.scheduler import Scheduler
from fab_sim.rng import Streams
from fab_sim.custody import Custody
from fab_sim.manufacturing import Manufacturing
from fab_sim.transfers import Transfers
from fab_sim.traffic import Traffic
from fab_sim.topology import compile_traffic
from fab_sim.workload import ReleaseSource
from fab_sim.flow import Flow
from fab_sim.campaigns import SetupCampaigns
from fab_sim.clearance import ClearancePool
from fab_sim.handoffs import HandoffPlans
from test_route_rules import step


def handoff_fixture(routes,counts=None,racks=2,protected=2,transitions=()):
    counts=counts or [1]*len(routes);groups=sorted({s['tool_group_id'] for steps in routes for s in steps})
    source={'model':{'routes':[{'id':f'r{i}','product_id':f'p{i}','steps':steps,'dedication_components':[]}
        for i,steps in enumerate(routes)],'tool_groups':[{'id':g,'initial_setup':None,'loading_us':2,'unloading_us':3} for g in groups],
        'setup_transitions':list(transitions),'releases':[{'id':f's{i}','product_id':f'p{i}','route_id':f'r{i}',
        'lots_per_release':n,'interval_seconds':'1','first_at_us':0,'due_allowance_us':100000000,'priority':10,
        'super_hot':False,'wafer_count':25} for i,n in enumerate(counts)]}}
    tools={g:{'id':g,'tool_group_id':g,'rack_slots':[f'{g}:rack:{i}' for i in range(racks)]} for g in groups}
    slots=[{'id':sid,'kind':'rack','equipment_id':g} for g,tool in tools.items() for sid in tool['rack_slots']]
    docks=[{'id':g+':port','kind':'ordinary','equipment_id':g,'role':'BOTH','rail_id':'a','offset_um':300+400*i}
        for i,g in enumerate(groups)]
    docks += [{'id':'entry','kind':'entry','equipment_id':'entry','role':'OUT','rail_id':'a','offset_um':100},
        {'id':'exit','kind':'exit','equipment_id':'exit','role':'IN','rail_id':'a','offset_um':3500}]
    docks += [{'id':f'buffer:{i}','kind':'track_buffer','role':'BOTH','rail_id':'a','offset_um':4000+300*i}
              for i in range(protected+1)]
    slots += [{'id':d['id'],'kind':'port' if d['kind']=='ordinary' else d['kind'],
        'equipment_id':d.get('equipment_id'),'role':d['role']} for d in docks]
    entry=[f'entry:{i}' for i in range(sum(counts))];exit=['exit:0']
    slots += [{'id':sid,'kind':'entry','equipment_id':'entry','dock_id':'entry'} for sid in entry]
    slots += [{'id':'exit:0','kind':'exit','equipment_id':'exit','dock_id':'exit'},{'id':'vehicle:v','kind':'vehicle'}]
    c=Custody(slots);s=Scheduler();lots={};events=[]
    emit=lambda k,p:events.append((k,deepcopy(p)))
    m=Manufacturing(source,s,Streams(7),lots,c,tools,emit)
    vehicles={'v':{'id':'v','type_id':'type','rail_id':'a','offset_um':10,'carrier_slot_id':'vehicle:v'}}
    model={'nodes':[{'id':n,'zcu_id':None,'zcu_type':'NONE'} for n in ('a','b')],
        'rails':[{'id':rid,'from_node':a,'to_node':b,'length_um':10000,'bay_id':'bay','curve':False,'speed_multiplier':'1'}
                 for rid,a,b in [('a','a','b'),('b','b','a')]],'zcus':[],'docks':docks,
        'vehicle_types':[{'id':'type','length_um':20,'gap_um':0,'speed_um_s':'100000000'}],
        'vehicles':list(vehicles.values()),'policy':{'holding_pitch_um':100,'raw_dock_clamp':False,
        'curve_cap_um_s':'100000000','pickup_us':10,'drop_us':10}}
    compile_traffic(model)
    t=Transfers(m,model,vehicles,emit,exchange_time_us={g:5 for g in groups})
    traffic=Traffic(model,s,vehicles,emit,transfers=t)
    release=ReleaseSource(source,{'count':sum(counts),'product_weights':{f'p{i}':str(n) for i,n in enumerate(counts)}})
    flow=Flow(release,m,t,entry,exit,emit)
    SetupCampaigns(m,t,flow.delivery_cost,emit)
    pool=ClearancePool(m,t,[f'buffer:{i}' for i in range(protected)],emit)
    HandoffPlans(t,pool,emit)
    return m,t,pool,c,s,events,flow,traffic


def finish(state,limit=10000):
    m,t,pool,c,s,events,flow,traffic=state
    for _ in range(limit):
        s.settle(s.now_us)
        if m.lots and all(lot.get('exited_at_us') is not None for lot in m.lots.values()):return
        if s.next_time_us is None:break
        s.settle(s.next_time_us)
    pytest.fail(f'physical drain failed: locations={c.locations}, demands={[(r["id"],r["status"]) for r in t.demands.values()]}, leases={pool.describe()}')


@pytest.mark.parametrize('count',[2,6])
def test_opposite_and_six_tool_dependencies_physically_finish_with_filled_ordinary_buffers(count):
    names=['Implant_128','DE_FE_1','DE_FE_86','WE_FE_108','LithoMet_FE_19','Litho_REG_FE_64'][:count]
    routes=[[step(1,tool_group_id=g),step(2,tool_group_id=names[(i+1)%count])] for i,g in enumerate(names)]
    state=handoff_fixture(routes);m,t,pool,c,s,events,flow,traffic=state
    # Establish completed-source residents while preserving real subsequent
    # Traffic/exchange/processing/exit operations.
    t.vehicles['v']['available']=False
    for tool in m.tools.values():tool['available']=False
    s.settle(0)
    for lid,lot in m.lots.items():
        t.cancel(t.active[lot['carrier_id']]);origin=c.locations[lot['carrier_id']]
        group=m._step(m.rules.visits[lot['current_visit_id']])['tool_group_id'];destination=group+':rack:0'
        assert c.reserve_many([destination],'initial',origins={origin:lot['carrier_id']})
        c.move(lot['carrier_id'],origin,destination,'initial');m.delivered(lot['carrier_id'],destination)
    assert c.reserve('buffer:2','background');c.place('background','buffer:2','background')
    for tool in m.tools.values():tool['available']=True
    t.vehicles['v']['available']=True;m.dirty();t.dirty();finish(state)
    assert len(m.lots)==count and all(lot.get('exited_at_us') is not None for lot in m.lots.values())
    assert all(m.rules.visits[lot['route_history'][-1]]['status']=='unloaded' for lot in m.lots.values())
    assert len(c.locations)==len(set(c.locations.values()))
    assert len([k for k,p in events if k=='handoff_committed'])>=count


def test_finished_batch_clears_then_successor_batch_physically_stages_and_exits():
    routes=[[step(1,batch_min=125,batch_max=150,processing_unit='batch'),
             step(2,batch_min=125,batch_max=150,processing_unit='batch')]]
    state=handoff_fixture(routes,[5],racks=6,protected=6);finish(state)
    m,t,pool,c,s,events,flow,traffic=state
    assemblies=list(m.batches.assemblies.values())
    assert len(assemblies)==2 and all(a['status']=='cleared' for a in assemblies)
    assert all(x['departed'] for a in assemblies for x in a['members'])
    assert m.setups['A'].completed_lots==10 and all(l.get('exited_at_us') is not None for l in m.lots.values())
    assert any(r['kind']=='finished_batch' and r['status']=='completed' for r in pool.records.values())
    assert all(len(a['members'])==5 for a in assemblies)


def test_positive_setup_minimum_earns_exact_credits_after_all_physical_arrivals():
    setup=lambda name:{'id':name,'when':'need'}
    transitions=[{'current':None,'new':name,'duration_us':4,'minimum_runs':7} for name in ('A','B')]
    state=handoff_fixture([[step(1,setup=setup('A')),step(2,setup=setup('B'))]],[7],racks=8,protected=8,transitions=transitions)
    finish(state);m,t,pool,c,s,events,flow,traffic=state
    assert m.setups['A'].current=='B' and m.setups['A'].completed_lots==7
    campaigns=list(m.campaigns.records.values())
    assert len(campaigns)==2 and all(len(r['members'])==7 and all(x['arrived'] and x['completed'] for x in r['members']) for r in campaigns)
    assert len([v for v in m.rules.visits.values() if v['status']=='unloaded'])==14


def test_competing_finished_batches_wait_for_whole_leases_then_both_physically_drain():
    routes=[[step(1,tool_group_id=g,batch_min=125,batch_max=150,processing_unit='batch'),
             step(2,tool_group_id=g,batch_min=125,batch_max=150,processing_unit='batch')] for g in ('A','B')]
    state=handoff_fixture(routes,[5,5],racks=6,protected=6);m,t,pool,c,s,events,flow,traffic=state
    saw_shortage=False
    for _ in range(10000):
        s.settle(s.now_us)
        saw_shortage=saw_shortage or any('conflicting_leases' in r and r['conflicting_leases'] for r in pool.describe())
        for record in pool.records.values():
            for member in record['members']:
                if member['status']=='buffered':
                    owner=c.slots[member['slot_id']]['reservation']
                    assert owner==record['id'] or owner==t.active.get(member['carrier_id'])
                    assert c.locations[member['carrier_id']]==member['slot_id']
        if m.lots and all(l.get('exited_at_us') is not None for l in m.lots.values()):break
        assert s.next_time_us is not None
        s.settle(s.next_time_us)
    assert saw_shortage and all(l.get('exited_at_us') is not None for l in m.lots.values())
    assert len(pool.records)==2 and all(r['status']=='completed' for r in pool.records.values())
    assert all(a['status']=='cleared' and len(a['members'])==5 for a in m.batches.assemblies.values())


@pytest.mark.parametrize('target_type,target',[('port','A:port'),('port','B:port'),('oht','v')])
def test_fault_during_committed_output_preserves_resources_and_resumes(target_type,target):
    state=handoff_fixture([[step(1),step(2,tool_group_id='B')]]);m,t,pool,c,s,events,flow,traffic=state
    s.settle(0)
    while not any(r['phase']=='output_active' for r in t.handoffs.records.values()):
        assert s.next_time_us is not None;s.settle(s.next_time_us)
    record=next(r for r in t.handoffs.records.values() if r['phase']=='output_active')
    destination=record['destination_id'];owner=c.slots[destination]['reservation']
    traffic.pause(target,'test-fault',target_type=target_type)
    if target_type=='port' and target=='A:port':
        eid=t.demands[record['demand_id']]['output_exchange_id'];timer=t.exchange.handlers['A'].active['timer']
        assert timer.causes and c.locations['s0:0:carrier']=='A:rack:0'
    tick=s.now_us+50
    s.register('recover-test',lambda p:traffic.recover(target,'test-fault',target_type=target_type))
    s.schedule(tick,2,'recover-test',{})
    assert c.slots[destination]['reservation']==owner
    finish(state)
    assert all(l.get('exited_at_us') is not None for l in m.lots.values())


def test_pending_pm_during_batch_staging_drains_commitment_before_next_admission():
    state=handoff_fixture([[step(1,batch_min=125,batch_max=150,processing_unit='batch'),
        step(2,batch_min=125,batch_max=150,processing_unit='batch')]],[5],racks=6,protected=6)
    m,t,pool,c,s,events,flow,traffic=state;s.settle(0)
    first=next(iter(m.batches.assemblies.values()));m.tools['A']['pm_pending']=True
    while first['status'] not in ('finished','cleared'):
        assert s.next_time_us is not None;s.settle(s.next_time_us)
    assert all(x['arrived'] for x in first['members']) and len(m.batches.assemblies)==1
    m.tools['A']['pm_pending']=False;m.dirty();t.dirty();flow.dirty();finish(state)
    assert len(m.batches.assemblies)==2 and all(l.get('exited_at_us') is not None for l in m.lots.values())


def test_clearance_continuation_preserves_visit_age_and_dedicated_tool():
    state=handoff_fixture([[step(1,batch_min=125,batch_max=150,processing_unit='batch'),
        step(2,batch_min=125,batch_max=150,processing_unit='batch')]],[5],racks=6,protected=6)
    m,t,pool,c,s,events,flow,traffic=state
    m.rules.routes['r0']['dedication_components']=[{'id':'same-tool','tool_group_id':'A','source_step_ids':['1','2']}]
    finish(state)
    for lot in m.lots.values():
        requests=[r for r in t.demands.values() if r['carrier_id']==lot['carrier_id'] and r['visit_id']==lot['route_history'][1]]
        assert len(requests)>=2 and len({r['original_request_time_us'] for r in requests})==1
        assert all(r['rack_slot_id'] is None or c.slots[r['rack_slot_id']]['equipment_id']=='A' for r in requests)
    assert all(r['status']=='completed' for r in pool.records.values())


def test_cancel_onward_pickup_restores_buffer_lease_without_moving_carrier():
    state=handoff_fixture([[step(1,batch_min=125,batch_max=150,processing_unit='batch'),
        step(2,batch_min=125,batch_max=150,processing_unit='batch')]],[5],racks=6,protected=6)
    m,t,pool,c,s,events,flow,traffic=state;s.settle(0)
    for _ in range(10000):
        candidate=next((r for r in t.demands.values() if r['status']=='assigned'
            and r['origin_id'] in pool.protected_slot_ids),None)
        if candidate:break
        assert s.next_time_us is not None;s.settle(s.next_time_us)
    assert candidate is not None
    carrier=candidate['carrier_id'];origin=candidate['origin_id'];owner=pool.buffer_owner(carrier,origin)
    t.cancel(candidate['id'])
    assert c.locations[carrier]==origin and c.slots[origin]['reservation']==owner
    finish(state)
    assert all(l.get('exited_at_us') is not None for l in m.lots.values())


def test_pending_pm_blocks_new_rack_admission_without_revoking_existing_commitments():
    state=handoff_fixture([[step(1)]]);m,t,pool,c,s,events,flow,traffic=state
    m.tools['A']['pm_pending']=True;s.settle(0)
    assert all(c.slots[sid]['reservation'] is None for sid in m.tools['A']['rack_slots'])
    assert not any(r['rack_slot_id'] is not None for r in t.demands.values())
    m.tools['A']['pm_pending']=False;m.dirty();t.dirty();flow.dirty();finish(state)


def test_equipment_fault_pauses_active_output_exchange_and_retains_complete_plan():
    from fab_sim.maintenance import Maintenance
    state=handoff_fixture([[step(1),step(2,tool_group_id='B')]]);m,t,pool,c,s,events,flow,traffic=state
    s.settle(0)
    while not any(r['phase']=='output_active' for r in t.handoffs.records.values()):
        assert s.next_time_us is not None;s.settle(s.next_time_us)
    record=next(r for r in t.handoffs.records.values() if r['phase']=='output_active')
    maintenance=Maintenance(m,lambda k,p:events.append((k,deepcopy(p))),exchange_handlers=t.exchange.handlers)
    maintenance.initialize({'model':{'equipment':[{'id':g,'kind':'ordinary','tool_group_id':g} for g in ('A','B')]}},
        {'model':{'tool_groups':[{'id':g,'area':'area'} for g in ('A','B')],'maintenance':[],'breakdowns':[]}},
        injected_faults=[{'id':'source-fault','target_id':'A','target_type':'equipment','onset_us':s.now_us,'duration_us':50}])
    s.settle(s.now_us)
    assert t.exchange.handlers['A'].active['timer'].causes
    assert c.locations['s0:0:carrier']=='A:rack:0'
    assert c.slots[record['destination_id']]['reservation']==record['demand_id']
    finish(state)


def test_changed_dedication_invalidates_lease_admission_before_output_claims():
    from test_clearance import lease_fixture
    from fab_sim.handoffs import HandoffPlans
    pool,t,m,s,c,lots=lease_fixture();assert pool.dispatch();HandoffPlans(t,pool,lambda k,p:None)
    m.rules.routes['r']['dedication_components']=[{'id':'dedicated','tool_group_id':'A','source_step_ids':['2']}]
    lots['low']['dedication']={'dedicated':'unavailable-tool'}
    before=deepcopy(c.slots);did=t.active['clow']
    assert not t.handoffs.prepare(did,'rack')
    assert c.slots==before and not t.exchange.requests


def test_mixed_direct_and_buffered_clearance_releases_lease_and_repeated_batches_exit():
    routes=[[step(1,batch_min=50,batch_max=50,processing_unit='batch'),
        step(2,tool_group_id='B'),
        step(3,batch_min=50,batch_max=50,processing_unit='batch'),
        step(4,batch_min=50,batch_max=50,processing_unit='batch')]]
    state=handoff_fixture(routes,[2],racks=2,protected=3)
    m,t,pool,c,s,events,flow,traffic=state
    for sid in m.tools['B']['rack_slots']:assert c.reserve(sid,'blocked-B')
    for _ in range(10000):
        s.settle(s.now_us)
        if any(x['status']=='in_transit' for r in pool.records.values() for x in r['members']):break
        assert s.next_time_us is not None;s.settle(s.next_time_us)
    else:pytest.fail('clearance never began')
    first=next(iter(pool.records.values()))
    for sid in m.tools['B']['rack_slots']:c.release(sid,'blocked-B')
    m.dirty();t.dirty();flow.dirty();finish(state)
    first_handoffs=[r for r in t.handoffs.records.values() if r['source_rack_id'] in m.tools['A']['rack_slots']
        and t.demands[r['demand_id']]['visit_id'] in {x['visit_id'] for x in first['members']}]
    assert any(r['lease_id'] for r in first_handoffs) and any(not r['lease_id'] for r in first_handoffs)
    assert all(r['status'] in ('completed','cancelled') for r in pool.records.values())
    assert all(c.slots[sid]['reservation'] is None and c.slots[sid]['carrier_id'] is None for sid in pool.protected_slot_ids)
    assert len(m.lots)==2 and all(l.get('exited_at_us') is not None for l in m.lots.values())


@pytest.mark.parametrize('change',['dedication','campaign','batch'])
def test_lease_admission_revalidates_membership_after_reservation_before_exchange(change):
    from test_clearance import lease_fixture
    pool,t,m,s,c,lots=lease_fixture();assert pool.dispatch();HandoffPlans(t,pool,lambda k,p:None)
    did=t.active['clow'];vid=lots['low']['current_visit_id']
    assert t.handoffs.prepare(did,'rack')
    assert t.demands[did]['overflow']
    if change=='dedication':
        m.rules.routes['r']['dedication_components']=[{'id':'dedicated','tool_group_id':'A','source_step_ids':['2']}]
        lots['low']['dedication']={'dedicated':'unavailable-tool'}
    elif change=='campaign':
        campaigns=SetupCampaigns(m,t,lambda *args:0,lambda k,p:None)
        campaigns.visit_owners[vid]='new-campaign'
        campaigns.records['new-campaign']={'tool_id':'t','status':'staging',
            'members':[{'visit_id':vid,'rack_slot_id':'rack','arrived':False}]}
    else:m.rules.visits[vid]['assembly_id']='new-batch'
    assert not t._admission_valid(t.demands[did])
    s.settle(0)
    assert c.locations['clow']=='rack'
    assert t.handoffs.records[did]['phase']=='cancelled'
    assert t.exchange.requests[did+':output']['status']=='cancelled'



@pytest.mark.parametrize('change',['batch','campaign'])
@pytest.mark.parametrize('phase',['before_selection','before_reservation','after_reservation'])
def test_owned_visit_cannot_enter_clearance_or_recommit_cancelled_overflow(change,phase):
    from test_clearance import lease_fixture
    pool,t,m,s,c,lots=lease_fixture();vid=lots['low']['current_visit_id'];did=t.active['clow']
    for sid,dock in t.docks.items():
        if dock['kind']=='track_buffer' and sid not in pool.protected_slot_ids:
            assert c.reserve(sid,'foreign-overflow')
    if phase!='before_selection':assert pool.dispatch()
    HandoffPlans(t,pool,lambda k,p:None)
    if phase=='after_reservation':assert t.handoffs.prepare(did,'rack')
    if change=='batch':m.rules.visits[vid]['assembly_id']='next-batch'
    else:
        campaigns=SetupCampaigns(m,t,lambda *args:0,lambda k,p:None)
        campaigns.visit_owners[vid]='next-campaign'
        campaigns.records['next-campaign']={'tool_id':'t','status':'staging',
            'members':[{'visit_id':vid,'rack_slot_id':'rack','arrived':False}]}
    if phase=='before_selection':
        pool.dispatch()
        assert pool.admission(vid) is None
    elif phase=='before_reservation':
        before=deepcopy(c.slots)
        assert not t.handoffs.prepare(did,'rack')
        assert c.slots==before and not t.exchange.requests
        pool.dispatch()
        assert pool.admission(vid) is None
    else:
        assert not t._admission_valid(t.demands[did]);t.cancel(did)
        for _ in range(3):
            retry=t.request('clow',vid,('port',))
            assert not t.handoffs.prepare(retry,'rack')
            assert s.now_us==0
        pool.dispatch()
        assert pool.admission(vid) is None
        assert sum(r['phase']=='cancelled' for r in t.handoffs.records.values())==1
    assert c.locations['clow']=='rack'
    assert pool.admission(lots['hot']['current_visit_id']) is not None
    assert all(c.slots[sid]['reservation']=='foreign-overflow' for sid,dock in t.docks.items()
        if dock['kind']=='track_buffer' and sid not in pool.protected_slot_ids)


def test_downstream_batch_owned_carriers_take_direct_handoffs_and_physically_exit():
    routes=[[step(1,batch_min=50,batch_max=50,processing_unit='batch'),
             step(2,tool_group_id='B',batch_min=50,batch_max=50,processing_unit='batch')]]
    state=handoff_fixture(routes,[2],racks=2,protected=3);finish(state)
    m,t,pool,c,s,events,flow,traffic=state
    successor=next(a for a in m.batches.assemblies.values() if a['tool_id']=='B')
    handoffs=[r for r in t.handoffs.records.values() if r['rack_owner']==successor['id']]
    assert len(handoffs)==2
    assert all(r['lease_id'] is None and r['phase']=='completed' for r in handoffs)
    assert successor['status']=='cleared' and all(x['departed'] for x in successor['members'])
    assert all(l.get('exited_at_us') is not None for l in m.lots.values())
    assert all(c.slots[sid]['reservation'] is None for sid in pool.protected_slot_ids)


@pytest.mark.parametrize('down_tool_output',[False,True])
def test_down_tool_output_lets_a_finished_lot_leave_a_broken_tool(down_tool_output):
    from fab_sim.maintenance import Maintenance
    state=handoff_fixture([[step(1),step(2,tool_group_id='B')]]);m,t,pool,c,s,events,flow,traffic=state
    s.settle(0)
    def finished_at_a():
        lot=m.lots.get('s0:0')
        return (lot is not None and c.locations.get(lot['carrier_id'],'').startswith('A:rack')
                and m.rules.visits[lot['current_visit_id']]['source_step_id']=='2')
    while not finished_at_a():
        assert s.next_time_us is not None;s.settle(s.next_time_us)
    maintenance=Maintenance(m,lambda k,p:events.append((k,deepcopy(p))),exchange_handlers=t.exchange.handlers)
    if down_tool_output:t.exchange.down_tool_output=True;maintenance.pause_exchange=False
    maintenance.initialize({'model':{'equipment':[{'id':g,'kind':'ordinary','tool_group_id':g} for g in ('A','B')]}},
        {'model':{'tool_groups':[{'id':g,'area':'area'} for g in ('A','B')],'maintenance':[],'breakdowns':[]}},
        injected_faults=[{'id':'long-fault','target_id':'A','target_type':'equipment','onset_us':s.now_us,'duration_us':10**12}])
    recovery=s.now_us+10**12
    started_b=lambda:any(k=='processing_started' and p.get('tool_id')=='B' for k,p in events)
    while not started_b() and s.next_time_us is not None and s.next_time_us<recovery:s.settle(s.next_time_us)
    assert started_b()==down_tool_output
    if down_tool_output:
        assert m.causes['A'] and not t.exchange.handlers['A'].causes


def test_enabling_down_tool_output_on_a_paused_tool_releases_its_port_handler():
    from fab_sim.maintenance import Maintenance
    state=handoff_fixture([[step(1),step(2,tool_group_id='B')]]);m,t,pool,c,s,events,flow,traffic=state
    s.settle(0)
    while not (m.lots.get('s0:0') and c.locations.get(m.lots['s0:0']['carrier_id'],'').startswith('A:rack')
               and m.rules.visits[m.lots['s0:0']['current_visit_id']]['source_step_id']=='2'):
        assert s.next_time_us is not None;s.settle(s.next_time_us)
    maintenance=Maintenance(m,lambda k,p:events.append((k,deepcopy(p))),exchange_handlers=t.exchange.handlers)
    maintenance.initialize({'model':{'equipment':[{'id':g,'kind':'ordinary','tool_group_id':g} for g in ('A','B')]}},
        {'model':{'tool_groups':[{'id':g,'area':'area'} for g in ('A','B')],'maintenance':[],'breakdowns':[]}},
        injected_faults=[{'id':'long-fault','target_id':'A','target_type':'equipment','onset_us':s.now_us,'duration_us':10**12}])
    s.settle(s.now_us)
    assert t.exchange.handlers['A'].causes  # paused under the old policy
    # Resume under down-tool output (as runner does after a policy-change restore).
    t.exchange.down_tool_output=True;maintenance.pause_exchange=False;maintenance.release_exchange_pauses()
    assert not t.exchange.handlers['A'].causes and m.causes['A']
    recovery=s.now_us+10**12
    started_b=lambda:any(k=='processing_started' and p.get('tool_id')=='B' for k,p in events)
    while not started_b() and s.next_time_us is not None and s.next_time_us<recovery:s.settle(s.next_time_us)
    assert started_b()
    while s.next_time_us is not None and s.next_time_us<=recovery:s.settle(s.next_time_us)
    assert not m.causes['A']  # recovery after the release does not fail on the resumed handler


@pytest.mark.parametrize('avoid_s',[None,1000])
def test_down_tool_avoid_holds_delivery_to_a_tool_in_a_long_downtime(avoid_s):
    from fab_sim.maintenance import Maintenance
    state=handoff_fixture([[step(1),step(2,tool_group_id='B')]]);m,t,pool,c,s,events,flow,traffic=state
    maintenance=Maintenance(m,lambda k,p:events.append((k,deepcopy(p))),exchange_handlers=t.exchange.handlers)
    t.exchange.down_tool_output=True;maintenance.pause_exchange=False  # B's ports keep working
    if avoid_s:t.down_tool_avoid_us=avoid_s*1_000_000;t.long_downtime=maintenance.long_downtime
    # B is down for 10^12 us from the start: longer than the threshold.
    maintenance.initialize({'model':{'equipment':[{'id':g,'kind':'ordinary','tool_group_id':g} for g in ('A','B')]}},
        {'model':{'tool_groups':[{'id':g,'area':'area'} for g in ('A','B')],'maintenance':[],'breakdowns':[]}},
        injected_faults=[{'id':'long-fault','target_id':'B','target_type':'equipment','onset_us':0,'duration_us':10**12}])
    s.settle(0)
    at_b=lambda:any(c.locations.get(m.lots[l]['carrier_id'],'').startswith('B:') for l in m.lots)
    while not at_b() and s.next_time_us is not None and s.next_time_us<10**11:s.settle(s.next_time_us)
    assert at_b()==(avoid_s is None)
    assert m.causes['B']
