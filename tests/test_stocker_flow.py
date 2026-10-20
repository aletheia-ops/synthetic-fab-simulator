"""Physical stocker capacity and entry-to-exit flow, without a CLI wrapper."""
from copy import deepcopy
import pytest
from test_transfers import arrive
from test_manufacturing_facade import place


def stocker_factory(steps=None):
    from fab_sim.transfers import Transfers
    from test_manufacturing_facade import factory as manufacturing_factory
    from test_routing import ring
    m,s,c,lots,e=manufacturing_factory(steps)
    for sid,kind,equipment,role in [('entry','entry','entry','OUT'),('port','port','t','BOTH'),
                                  ('buffer','track_buffer',None,'BOTH'),('exit','exit','exit','IN')]:
        c.slots[sid]={'id':sid,'kind':kind,'equipment_id':equipment,'role':role,'carrier_id':None,'reservation':None}
    top=ring();top['policy'].update(pickup_us=10,drop_us=10)
    top['docks']=[{'id':sid,'equipment_id':c.slots[sid]['equipment_id'],'kind':c.slots[sid]['kind'],
                  'role':c.slots[sid]['role'],'rail_id':'a','offset_um':offset}
                 for sid,offset in [('entry',100),('port',400),('buffer',600),('exit',900)]]
    for sid,dock in [('entry:0','entry'),('entry:1','entry'),('exit:0','exit')]:
        c.slots[sid]={'id':sid,'kind':dock,'equipment_id':dock,'dock_id':dock,'carrier_id':None,'reservation':None}
    v={}
    for vid in ('v1','v2'):
        v[vid]={'id':vid,'type_id':'v','rail_id':'a','offset_um':0,'carrier_slot_id':'vehicle:'+vid}
        c.slots['vehicle:'+vid]={'id':'vehicle:'+vid,'kind':'vehicle','carrier_id':None,'reservation':None}
    t=Transfers(m,top,v,lambda k,p:e.append((k,deepcopy(p))))
    return t,m,s,c,lots,v,e


def test_stocker_pickup_holds_storage_and_exclusive_dock_until_timed_custody_change():
    t,m,s,c,lots,v,e=stocker_factory()
    low=m.enter_visit('low','1');hot=m.enter_visit('hot','1')
    place(c,'clow','entry:0');place(c,'chot','entry:1')
    first=t.request('clow',low,('port',))
    s.settle(0);r=t.demands[first]
    assert r['status']=='assigned' and c.slots['entry']['reservation']==first
    assert c.locations['clow']=='entry:0' and c.slots['entry']['carrier_id'] is None
    arrive(t,v,first,'pickup');s.settle(9)
    assert c.locations['clow']=='entry:0' and c.slots['entry']['reservation']==first
    s.settle(10)
    assert c.locations['clow']==v[r['vehicle_id']]['carrier_slot_id']
    assert c.available('entry:0') and c.available('entry')


def test_stocker_precustody_cancellation_releases_dock_and_internal_slots():
    t,m,s,c,lots,v,e=stocker_factory();visit=m.enter_visit('low','1');place(c,'clow','entry:0')
    did=t.request('clow',visit,('port',));s.settle(0);t.cancel(did)
    assert c.locations['clow']=='entry:0'
    assert all(c.slots[sid]['reservation'] is None for sid in ('entry','entry:0','port','rack'))


def test_exit_delivery_reserves_internal_slot_and_dock_without_teleportation():
    t,m,s,c,lots,v,e=stocker_factory();lots['low'].update(current_visit_id=None,route_complete_at_us=0)
    place(c,'clow','port');did=t.request('clow',None,('exit:0',));s.settle(0)
    assert c.slots['exit:0']['reservation']==did and c.slots['exit']['reservation']==did
    arrive(t,v,did,'pickup');s.settle(10);arrive(t,v,did,'drop');s.settle(19)
    assert c.locations['clow']=='vehicle:v1'
    s.settle(20)
    assert c.locations['clow']=='exit:0' and c.available('exit')
    assert not lots['low'].get('exited_at_us')


def test_two_internal_slots_cannot_assign_two_ohts_to_one_dock():
    t,m,s,c,lots,v,e=stocker_factory()
    c.slots['rack2']={**c.slots['rack'],'id':'rack2'};m.tools['t']['rack_slots'].append('rack2')
    c.slots['port2']={**c.slots['port'],'id':'port2'};t.docks['port2']={**t.docks['port'],'id':'port2'}
    a=m.enter_visit('low','1');b=m.enter_visit('hot','1')
    place(c,'clow','entry:0');place(c,'chot','entry:1')
    first=t.request('clow',a,('port',));second=t.request('chot',b,('port2',));s.settle(0)
    assert t.demands[first]['status']=='assigned' and t.demands[second]['status']=='pending'
    assert c.slots['entry:1']['reservation'] is None and v['v2']['assignment_id'] is None
    t.cancel(first);s.settle(0)
    assert t.demands[second]['status']=='assigned' and c.slots['entry']['reservation']==second


def flow_factory(count=3,steps=None):
    from fab_sim.flow import Flow
    from fab_sim.workload import ReleaseSource
    from fab_sim.traffic import Traffic
    from fab_sim.topology import compile_traffic
    t,m,s,c,lots,v,e=stocker_factory(steps);lots.clear();del v['v2']
    top=t.model;v['v1']['offset_um']=10
    top.update(nodes=[{'id':n,'zcu_id':None,'zcu_type':'NONE'} for n in ('n1','n2')],zcus=[],vehicles=list(v.values()))
    top['vehicle_types'][0].update(length_um=20,gap_um=0)
    for rail in top['rails']:rail['bay_id']='bay'
    top['policy'].update(holding_pitch_um=100,raw_dock_clamp=False);compile_traffic(top)
    traffic=Traffic(top,s,v,lambda k,p:e.append((k,deepcopy(p))),transfers=t)
    source=ReleaseSource({'model':{'releases':[{'id':'a','product_id':'P','route_id':'r','priority':1,
        'super_hot':False,'wafer_count':25,'first_at_us':0,'interval_seconds':'0.000001',
        'lots_per_release':1,'due_allowance_us':100}]}},{'count':count})
    flow=Flow(source,m,t,('entry:0','entry:1'),('exit:0',),lambda k,p:e.append((k,deepcopy(p))))
    return flow,traffic,t,m,s,c,lots,v,e


def test_finite_backlog_waits_for_real_entry_space_then_all_carriers_physically_exit():
    flow,traffic,t,m,s,c,lots,v,e=flow_factory()
    s.settle(0);s.settle(1);s.settle(2)
    assert len(lots)==2 and len(flow.pending)==1 and flow.source_exhausted
    assert 'a:2:carrier' not in c.locations
    for _ in range(3000):
        if all(lot.get('exited_at_us') is not None for lot in lots.values()) and len(lots)==3:break
        assert s.next_time_us is not None
        s.settle(s.next_time_us)
    else:pytest.fail('finite cohort did not exit')
    assert lots['a:2']['admission_delay_us']>0
    assert all(lot['exited_at_us']>lot['route_complete_at_us'] for lot in lots.values())
    assert not c.locations and not t.active
    assert len([x for x in e if x[0]=='lot_admitted'])==3
    assert len([x for x in e if x[0]=='lot_exited'])==3
    assert all(c.slots[x]['reservation'] is None for x in ('entry','exit','entry:0','entry:1','exit:0'))


def test_exact_batch_population_includes_future_releases_but_excludes_committed_members():
    from test_route_rules import step
    flow,traffic,t,m,s,c,lots,v,e=flow_factory(count=30,steps=[step(1,batch_min=125,batch_max=150)])
    for i in range(2,6):
        sid=f'entry:{i}'
        c.slots[sid]={**c.slots['entry:0'],'id':sid}
    flow.entry_slots=tuple(f'entry:{i}' for i in range(6))
    for i in range(2,7):
        sid=f'rack{i}';c.slots[sid]={**c.slots['rack'],'id':sid}
        m.tools['t']['rack_slots'].append(sid)
    s.settle(0)
    lot=next(iter(lots.values()));source_step=m.rules.steps['r']['1']
    assert m.batches.exact_population(lot,source_step)==30
    m.rules.visits[lot['current_visit_id']]['assembly_id']='committed'
    assert m.batches.exact_population(lot,source_step)==29
    m.tools['narrow']={'tool_group_id':'A','rack_slots':['small']}
    assert m.batches.exact_population(lot,source_step) is None


def test_shared_entry_staging_bound_includes_both_root_batch_cohorts():
    from fab_sim.flow import root_batch_staging_requirement
    from fab_sim.workload import ReleaseSource
    from test_route_rules import step
    source=ReleaseSource({'model':{'releases':[{
        'id':product,'product_id':product,'route_id':product,'priority':1,
        'super_hot':False,'wafer_count':25,'first_at_us':0,'interval_seconds':'1',
        'lots_per_release':1,'due_allowance_us':100} for product in ('P','Q')]}},{'count':60})
    routes={'P':{'steps':[step(1,batch_min=125,batch_max=150)]},
            'Q':{'steps':[step(1,batch_min=100,batch_max=125)]}}
    assert root_batch_staging_requirement(routes,source)==10
    source=ReleaseSource({'model':{'releases':list(source.streams.values())}},
                        {'count':7,'product_weights':{'P':'1','Q':'6'}})
    routes['Q']['steps'][0].update(batch_min=125,batch_max=150)
    assert root_batch_staging_requirement(routes,source)==7


def test_root_remainder_guard_does_not_deadlock_a_smaller_entry_stocker():
    from test_route_rules import step
    flow,traffic,t,m,s,c,lots,v,e=flow_factory(count=3,steps=[step(1,batch_min=50,batch_max=75)])
    for i in (2,3):
        sid=f'rack{i}';c.slots[sid]={**c.slots['rack'],'id':sid}
        m.tools['t']['rack_slots'].append(sid)
    s.settle(0);s.settle(1);s.settle(2)
    # Entry holds only two. Waiting for a third prevents that LOT's admission.
    assert len(m.batches.assemblies)==1
    assert len(next(iter(m.batches.assemblies.values()))['members'])==2


@pytest.mark.parametrize('overrides',[{'sampling_probability':'0.5'},
    {'rework':{'probability':'0.5','target_step_id':'1'}}])
def test_uncertain_batch_population_is_not_treated_as_exact(overrides):
    from test_route_rules import step
    flow,traffic,t,m,s,c,lots,v,e=flow_factory(count=30,steps=[step(1,batch_min=125,batch_max=150,**overrides)])
    assert m.batches.exact_population({'product_id':'P','route_id':'r'},m.rules.steps['r']['1']) is None


def test_full_entry_has_no_polling_events_after_release_exhaustion():
    flow,traffic,t,m,s,c,lots,v,e=flow_factory()
    v['v1']['available']=False
    s.settle(0);s.settle(1);s.settle(2)
    assert len(lots)==2 and len(flow.pending)==1 and s.next_time_us is None


def test_flow_assembles_whole_batch_before_delivery_and_leaves_finite_tail_waiting():
    from test_route_rules import step
    flow,traffic,t,m,s,c,lots,v,e=flow_factory(3,[step(1,batch_min=50,batch_max=50,processing_unit='batch')])
    c.slots['rack2']={**c.slots['rack'],'id':'rack2'};m.tools['t']['rack_slots'].append('rack2')
    s.settle(0);s.settle(1)
    assert len(m.batches.assemblies)==1
    for _ in range(3000):
        if s.next_time_us is None:break
        s.settle(s.next_time_us)
    else:pytest.fail('batch flow did not settle')
    assert sum(lot.get('exited_at_us') is not None for lot in lots.values())==2
    assert lots['a:2']['route_complete_at_us'] is None
    assert len(m.batches.assemblies)==1 and not t.active


def test_shared_dock_fault_pauses_internal_slot_pickup_without_repeating_service():
    from fab_sim.amhs_faults import AMHSFaults
    flow,traffic,t,m,s,c,lots,v,e=flow_factory(1)
    AMHSFaults(traffic,[{'id':'dock-fault','target_type':'port','target_id':'entry','onset_us':30005,'duration_us':100}])
    s.settle(0)
    while s.next_time_us is not None and s.next_time_us<=30109:s.settle(s.next_time_us)
    assert c.locations['a:0:carrier']=='entry:0'
    s.settle(30110)
    assert c.locations['a:0:carrier']=='vehicle:v1' and c.available('entry')


def test_lost_shared_dock_claim_fails_before_custody_is_moved():
    t,m,s,c,lots,v,e=stocker_factory();visit=m.enter_visit('low','1');place(c,'clow','entry:0')
    did=t.request('clow',visit,('port',));s.settle(0);arrive(t,v,did,'pickup')
    c.slots['entry']['reservation']='foreign'
    with pytest.raises(RuntimeError,match='ownership'):s.settle(10)
    assert c.locations['clow']=='entry:0'


@pytest.mark.parametrize('following_batch',[True,False])
def test_finished_batch_clears_racks_before_following_same_tool_visits(following_batch):
    from test_route_rules import step
    batch={'batch_min':50,'batch_max':50,'processing_unit':'batch'}
    flow,traffic,t,m,s,c,lots,v,e=flow_factory(2,[step(1,**batch),step(2,**(batch if following_batch else {}))])
    c.slots['rack2']={**c.slots['rack'],'id':'rack2'};m.tools['t']['rack_slots'].append('rack2')
    # Two external ports allow both completed members to clear before reassembly.
    c.slots['port2']={**c.slots['port'],'id':'port2'}
    dock={**t.docks['port'],'id':'port2','offset_um':700}
    t.docks['port2']=dock;t.model['docks'].append(dock)
    traffic.docks['port2']=dock;traffic.poses['port2']=dock;traffic.causes['port']['port2']=set()
    for _ in range(3000):
        s.settle(s.now_us if not lots else s.next_time_us if s.next_time_us is not None else s.now_us)
        if len(lots)==2 and all(lot.get('exited_at_us') is not None for lot in lots.values()):break
        if s.next_time_us is None:break
    assert sum(lot.get('exited_at_us') is not None for lot in lots.values())==2
    old=m.batches.assemblies['batch:1']
    assert old['status']=='cleared'
    assert all(m.rules.visits[lot['route_history'][1]]['processing_started_at_us']>=old['cleared_at_us'] for lot in lots.values())


def test_ordinary_same_tool_next_visit_reuses_rack_without_extra_transfer():
    from test_route_rules import step
    flow,traffic,t,m,s,c,lots,v,e=flow_factory(1,[step(1),step(2)])
    s.settle(0)
    while s.next_time_us is not None and not lots['a:0'].get('exited_at_us'):s.settle(s.next_time_us)
    assert lots['a:0']['exited_at_us'] is not None
    assert len(lots['a:0']['route_history'])==2 and len(t.demands)==2
    assert len(t.exchange.requests)==2


def test_internal_stocker_slots_cannot_target_another_internal_slot():
    from fab_sim.transfers import Transfers
    t,m,s,c,lots,v,e=stocker_factory()
    c.slots['entry:1']['dock_id']='entry:0'
    with pytest.raises(ValueError,match='mapping'):Transfers(m,t.model,v,lambda k,p:None)


def test_faulted_pending_output_exchange_does_not_block_another_free_port():
    t,m,s,c,lots,v,e=stocker_factory()
    c.slots['rack2']={**c.slots['rack'],'id':'rack2'};m.tools['t']['rack_slots'].append('rack2')
    c.slots['port2']={**c.slots['port'],'id':'port2'};t.docks['port2']={**t.docks['port'],'id':'port2'}
    for lid,rack in [('low','rack'),('hot','rack2')]:
        lots[lid].update(current_visit_id=None,route_complete_at_us=0);place(c,lots[lid]['carrier_id'],rack)
    t.exchange.port_causes={'port':{'fault'}}
    t.exchange.request('blocked','clow','rack','port')
    did=t.request('chot',None,('exit:0',));s.settle(0)
    assert t.exchange.requests['blocked']['status']=='pending'
    assert t.demands[did]['output_exchange_id'] is not None
    exchange=t.exchange.requests[t.demands[did]['output_exchange_id']]
    assert exchange['status']=='active' and exchange['destination_id']=='port2'
    s.settle(1000000)
    assert c.locations['chot']=='port2' and c.locations['clow']=='rack'
