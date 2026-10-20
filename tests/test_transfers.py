"""Transfer assignment/handling milestones; travel permission belongs to Traffic."""
from copy import deepcopy
import pytest
from test_manufacturing_facade import factory as manufacturing_factory,place


def factory(steps=None, rack_count=1):
    from fab_sim.transfers import Transfers
    from test_routing import ring
    m,s,c,lots,events=manufacturing_factory(steps, rack_count=rack_count)
    for sid,kind,equipment,role in [('entry','entry',None,'OUT'),('port','port','t','BOTH'),('buffer','track_buffer',None,'BOTH'),('exit','exit',None,'IN')]:
        c.slots[sid]={'id':sid,'kind':kind,'equipment_id':equipment,'role':role,'carrier_id':None,'reservation':None}
    model=ring();model['policy'].update(pickup_us=10,drop_us=10)
    model['docks']=[{'id':sid,'equipment_id':c.slots[sid]['equipment_id'],'kind':c.slots[sid]['kind'],'role':c.slots[sid]['role'],'rail_id':'a','offset_um':offset}
                    for sid,offset in [('entry',100),('port',400),('buffer',600),('exit',900)]]
    vehicles={'v1':{'id':'v1','type_id':'v','rail_id':'a','offset_um':0,'carrier_slot_id':'vehicle:v1'}}
    c.slots['vehicle:v1']={'id':'vehicle:v1','kind':'vehicle','carrier_id':None,'reservation':None}
    t=Transfers(m,model,vehicles,lambda k,p:events.append((k,deepcopy(p))))
    return t,m,s,c,lots,vehicles,events


def arrive(t,vehicles,did,phase):
    r=t.demands[did];vehicles[r['vehicle_id']].update(r[phase+'_pose'])
    t.arrived(did,phase)
    t.scheduler.settle(t.scheduler.now_us)


def test_assignment_atomically_holds_origin_vehicle_port_and_rack_until_handling_completes():
    t,m,s,c,lots,vehicles,events=factory();vid=m.enter_visit('low','1');place(c,'clow','entry')
    snapshots=[];c.on_change=lambda:snapshots.append(tuple(c.slots[x]['reservation'] for x in ('entry','vehicle:v1','port','rack')))
    did=t.request('clow',vid,('port',));s.settle(0);r=t.demands[did]
    assert r['status']=='assigned' and snapshots==[(did,did,did,did)]
    assert c.locations['clow']=='entry' and vehicles['v1']['assignment_id']==did
    arrive(t,vehicles,did,'pickup');s.settle(9);assert c.locations['clow']=='entry'
    s.settle(10);assert c.locations['clow']=='vehicle:v1' and r['status']=='picked_up'
    assert c.slots['entry']['reservation'] is None and c.slots['port']['reservation']==did
    arrive(t,vehicles,did,'drop');s.settle(19);assert c.locations['clow']=='vehicle:v1'
    s.settle(20);assert c.locations['clow']=='port' and r['status']=='delivered'
    assert vehicles['v1']['assignment_id'] is None and c.slots['rack']['reservation']
    s.settle(1000020);assert c.locations['clow']=='rack' and m.executions


def test_competing_demands_allocate_one_vehicle_and_loser_retains_age_and_origin():
    t,m,s,c,lots,vehicles,events=factory();v=m.enter_visit('low','1');h=m.enter_visit('hot','1')
    place(c,'clow','entry');place(c,'chot','buffer')
    first=t.request('clow',v,('port',));second=t.request('chot',h,('port',));s.settle(0)
    assert t.demands[first]['status']=='assigned' and t.demands[second]['status']=='pending'
    assert c.locations['chot']=='buffer' and c.slots['buffer']['reservation'] is None
    assert t.demands[second]['original_request_time_us']==0


def test_unavailable_vehicle_does_not_divert_when_equipment_space_exists():
    t,m,s,c,lots,vehicles,events=factory();v=m.enter_visit('low','1');place(c,'clow','entry')
    vehicles['v1']['available']=False;did=t.request('clow',v,('port',));s.settle(0)
    assert t.demands[did]['status']=='pending' and c.locations['clow']=='entry'
    assert all(c.slots[x]['reservation'] is None for x in ('rack','port','buffer'))


def test_precustody_cancel_releases_assignment_claims_and_keeps_vehicle_pose():
    t,m,s,c,lots,vehicles,events=factory();v=m.enter_visit('low','1');place(c,'clow','entry')
    did=t.request('clow',v,('port',));s.settle(0)
    pose=(vehicles['v1']['rail_id'],vehicles['v1']['offset_um']);t.cancel(did)
    assert c.locations['clow']=='entry' and all(c.slots[x]['reservation'] is None for x in ('entry','vehicle:v1','port','rack'))
    assert pose==(vehicles['v1']['rail_id'],vehicles['v1']['offset_um']) and vehicles['v1']['assignment_id'] is None
    assert t.demands[did]['status']=='cancelled'


def test_after_pickup_cancellation_cannot_revoke_delivery_space():
    t,m,s,c,lots,vehicles,events=factory();v=m.enter_visit('low','1');place(c,'clow','entry')
    did=t.request('clow',v,('port',));s.settle(0);arrive(t,vehicles,did,'pickup');s.settle(10)
    with pytest.raises(ValueError,match='pickup'):t.cancel(did)
    assert c.locations['clow']=='vehicle:v1' and c.slots['port']['reservation']==did


def test_duplicate_notifications_return_same_active_demand():
    t,m,s,c,lots,vehicles,events=factory();v=m.enter_visit('low','1');place(c,'clow','entry')
    did=t.request('clow',v,('port',));assert t.request('clow',v,('port',))==did
    assert len(t.demands)==1


def add_idle_replica(t,m,c):
    from fab_sim.manufacturing import SetupState
    m.tools['other']={**m.tools['t'],'id':'other','rack_slots':['other:rack'],
                      'active_execution_id':None}
    m.setups['other']=SetupState(None,[])
    m.causes['other']=set()
    c.slots['other:rack']={**c.slots['rack'],'id':'other:rack','equipment_id':'other'}
    c.slots['other:port']={**c.slots['port'],'id':'other:port','equipment_id':'other'}
    t.docks['other:port']={**t.docks['port'],'id':'other:port','equipment_id':'other','offset_um':800}


def test_serial_assignment_prefers_idle_replica_over_nearer_queued_tool():
    t,m,s,c,lots,vehicles,events=factory(rack_count=2)
    add_idle_replica(t,m,c)
    incoming=m.enter_visit('low','1');m.enter_visit('hot','1')
    place(c,'clow','entry');place(c,'chot','rack')
    did=t.request('clow',incoming,('port','other:port'))
    choices=t._choices(t.demands[did],'entry')
    assert choices[0][2]=='other:port'
    # With no competing work, restore directed free-flow proximity ranking.
    assert c.reserve_many([],'remove',origins={'rack':'chot'})
    c.remove('chot','rack','remove')
    assert t._choices(t.demands[did],'entry')[0][2]=='port'


def test_serial_assignment_counts_work_already_committed_to_inbound_transport():
    t,m,s,c,lots,vehicles,events=factory(rack_count=2)
    add_idle_replica(t,m,c)
    hot=m.enter_visit('hot','1');low=m.enter_visit('low','1')
    place(c,'chot','buffer');place(c,'clow','entry')
    hot_demand=t.request('chot',hot,('port',));t.dispatch()
    assert t.demands[hot_demand]['status']=='assigned'
    assert c.locations['chot']=='buffer'  # Still waiting for pickup.
    low_demand=t.request('clow',low,('port','other:port'))
    assert t._choices(t.demands[low_demand],'entry')[0][2]=='other:port'


def test_overflow_only_when_equipment_space_blocked_and_retains_visit_age():
    t,m,s,c,lots,vehicles,events=factory();v=m.enter_visit('low','1');place(c,'clow','entry');c.reserve('rack','busy')
    did=t.request('clow',v,('port',));s.settle(0);r=t.demands[did]
    assert r['destination_id']=='buffer' and r['overflow']
    arrive(t,vehicles,did,'pickup');s.settle(10);arrive(t,vehicles,did,'drop');s.settle(20)
    next_id=t.active['clow'];next_r=t.demands[next_id]
    assert next_id!=did and next_r['status']=='pending' and next_r['original_request_time_us']==0
    assert m.rules.visits[v]['status']=='pending' and next_r['visit_id']==v
    assert c.locations['clow']=='buffer' and not next_r['overflow']
    c.release('rack','busy');s.settle(20);assert next_r['destination_id']=='port'


def test_input_delivery_keeps_single_active_demand_until_rack_exchange_completes():
    t,m,s,c,lots,vehicles,events=factory();v=m.enter_visit('low','1');place(c,'clow','entry')
    did=t.request('clow',v,('port',));s.settle(0)
    arrive(t,vehicles,did,'pickup');s.settle(10);arrive(t,vehicles,did,'drop');s.settle(20)
    assert t.request('clow',v,('port',))==did
    s.settle(1000020);assert 'clow' not in t.active


def test_same_tool_next_visit_reuses_rack_without_exchange_or_oht_assignment():
    t,m,s,c,lots,vehicles,events=factory();v=m.enter_visit('low','1');place(c,'clow','rack')
    did=t.request('clow',v,('port',));s.settle(0)
    assert t.demands[did]['status']=='reused' and c.locations['clow']=='rack'
    assert m.executions and not t.movements and not t.exchange.requests


def test_rack_origin_must_exchange_to_output_port_before_oht_assignment():
    t,m,s,c,lots,vehicles,events=factory();m.enter_visit('low','1');place(c,'clow','rack');m.delivered('clow','rack')
    s.settle(0);s.settle(2);s.settle(12);s.settle(15)
    did=t.request('clow',None,('exit',));s.settle(15)
    assert t.demands[did]['status']=='pending' and not t.movements and c.locations['clow']=='rack'
    s.settle(1000015)
    assert c.locations['clow']=='port' and t.demands[did]['status']=='assigned'
    assert t.demands[did]['origin_id']=='port' and t.demands[did]['destination_id']=='exit'


def test_cancelled_moving_empty_approach_retains_vehicle_until_actual_endpoint():
    t,m,s,c,lots,vehicles,events=factory();v=m.enter_visit('low','1');place(c,'clow','entry')
    did=t.request('clow',v,('port',));s.settle(0);mid=did+':pickup'
    t.movement_started(mid);vehicles['v1']['offset_um']=50;t.cancel(did)
    assert vehicles['v1']['movement_id']==mid and vehicles['v1']['offset_um']==50
    again=t.request('clow',v,('port',));s.settle(0);assert t.demands[again]['status']=='pending'
    arrive(t,vehicles,did,'pickup');s.settle(0)
    assert t.demands[again]['status']=='assigned' and c.locations['clow']=='entry'


def test_wrong_pose_and_lost_claim_cannot_complete_custody_transition():
    t,m,s,c,lots,vehicles,events=factory();v=m.enter_visit('low','1');place(c,'clow','entry')
    did=t.request('clow',v,('port',));s.settle(0)
    with pytest.raises(ValueError,match='endpoint'):t.arrived(did,'pickup')
    arrive(t,vehicles,did,'pickup');c.slots['rack']['reservation']='foreign'
    with pytest.raises(RuntimeError,match='ownership'):s.settle(10)
    assert c.locations['clow']=='entry' and c.slots['vehicle:v1']['carrier_id'] is None


def test_new_eligibility_invalidates_assignment_before_pickup_without_custody_change():
    t,m,s,c,lots,vehicles,events=factory();v=m.enter_visit('low','1');place(c,'clow','entry')
    did=t.request('clow',v,('port',));s.settle(0)
    m.tools['t']['tool_group_id']='wrong'
    arrive(t,vehicles,did,'pickup')
    assert t.demands[did]['status']=='cancelled' and c.locations['clow']=='entry'
    assert all(c.slots[x]['reservation'] is None for x in ('entry','vehicle:v1','port','rack'))


def test_batch_assignment_and_cancellation_preserve_precommitted_rack_owner():
    from fab_sim.transfers import Transfers
    from test_batch_service import factory as batch_factory
    from test_routing import ring
    m,s,c,lots,events=batch_factory();model=ring();model['policy'].update(pickup_us=10,drop_us=10)
    model['docks']=[]
    for sid,slot in c.slots.items():
        if slot['kind'] in ('entry','port'):
            slot.update(role='OUT' if slot['kind']=='entry' else 'BOTH',equipment_id=None if slot['kind']=='entry' else 'tool-a')
            model['docks'].append({'id':sid,'kind':slot['kind'],'role':slot['role'],'equipment_id':slot['equipment_id'],'rail_id':'a','offset_um':100 if slot['kind']=='entry' else 400})
    c.slots['vehicle:v1']={'id':'vehicle:v1','kind':'vehicle','carrier_id':None,'reservation':None}
    vehicles={'v1':{'id':'v1','type_id':'v','rail_id':'a','offset_um':0,'carrier_slot_id':'vehicle:v1'}}
    t=Transfers(m,model,vehicles,lambda k,p:None,exchange_time_us={'tool-a':5});s.settle(0)
    a=next(iter(m.batches.assemblies.values()));member=a['members'][0]
    did=t.request(member['carrier_id'],member['visit_id'],('port:0',));s.settle(0)
    assert c.slots[member['rack_slot_id']]['reservation']==a['id']
    t.cancel(did);assert c.slots[member['rack_slot_id']]['reservation']==a['id']
    did=t.request(member['carrier_id'],member['visit_id'],('port:0',));s.settle(0)
    arrive(t,vehicles,did,'pickup');s.settle(10);arrive(t,vehicles,did,'drop');s.settle(20);s.settle(25)
    assert member['arrived'] and c.locations[member['carrier_id']]==member['rack_slot_id']
    assert a['status']=='assembling' and not m.executions


def test_buffer_origin_never_diverts_to_another_buffer():
    t,m,s,c,lots,vehicles,events=factory();v=m.enter_visit('low','1');place(c,'clow','buffer');c.reserve('rack','busy')
    c.slots['buffer2']={'id':'buffer2','kind':'track_buffer','carrier_id':None,'reservation':None}
    t.docks['buffer2']={**t.docks['buffer'],'id':'buffer2','offset_um':700}
    did=t.request('clow',v,('port',));s.settle(0)
    assert t.demands[did]['status']=='pending' and c.locations['clow']=='buffer'
    assert c.slots['buffer2']['reservation'] is None


def test_invalid_port_direction_demand_rejects_instead_of_overflowing():
    t,m,s,c,lots,vehicles,events=factory();v=m.enter_visit('low','1');place(c,'clow','entry');t.docks['port']['role']='OUT'
    with pytest.raises(ValueError,match='destination'):t.request('clow',v,('port',))
    assert not t.demands and not t.active


def test_cancel_demand_during_outbound_exchange_keeps_carrier_in_rack():
    t,m,s,c,lots,vehicles,events=factory();m.enter_visit('low','1');place(c,'clow','rack');m.delivered('clow','rack')
    s.settle(0);s.settle(2);s.settle(12);s.settle(15)
    did=t.request('clow',None,('exit',));s.settle(15);t.cancel(did);s.settle(1000015)
    assert c.locations['clow']=='rack' and not m.claims
    assert c.slots['rack']['reservation'] is None and c.slots['port']['reservation'] is None
    assert not t.movements


def test_nearest_loaded_destination_then_nearest_empty_vehicle_and_stable_ties():
    t,m,s,c,lots,vehicles,events=factory();v=m.enter_visit('low','1');place(c,'clow','entry')
    c.slots['port2']={**c.slots['port'],'id':'port2'}
    t.docks['port2']={**t.docks['port'],'id':'port2','offset_um':300}
    c.slots['vehicle:v2']={**c.slots['vehicle:v1'],'id':'vehicle:v2'}
    vehicles['v2']={**vehicles['v1'],'id':'v2','offset_um':90,'carrier_slot_id':'vehicle:v2'}
    did=t.request('clow',v,('port','port2'));s.settle(0)
    assert t.demands[did]['destination_id']=='port2' and t.demands[did]['vehicle_id']=='v2'


def test_cancel_during_pickup_handling_cancels_timer_and_keeps_origin():
    t,m,s,c,lots,vehicles,events=factory();v=m.enter_visit('low','1');place(c,'clow','entry')
    did=t.request('clow',v,('port',));s.settle(0);arrive(t,vehicles,did,'pickup');s.settle(4);t.cancel(did);s.settle(10)
    assert c.locations['clow']=='entry' and not t.active and not t.movements[did+':pickup']['status']=='moving'
    assert all(c.slots[x]['reservation'] is None for x in ('entry','port','rack','vehicle:v1'))


def test_coincident_docks_have_no_zero_distance_hop_but_require_positive_hoisting():
    t,m,s,c,lots,vehicles,events=factory();t.docks['port']['offset_um']=100;vehicles['v1']['offset_um']=100
    v=m.enter_visit('low','1');place(c,'clow','entry');did=t.request('clow',v,('port',));s.settle(0)
    assert t.movements[did+':pickup']['path']['sublinks']==[] and s.next_time_us is None
    arrive(t,vehicles,did,'pickup');s.settle(10)
    assert t.movements[did+':drop']['path']['sublinks']==[] and c.locations['clow']=='vehicle:v1'
    arrive(t,vehicles,did,'drop');s.settle(19);assert c.locations['clow']=='vehicle:v1'
    s.settle(20);assert c.locations['clow']=='port'


@pytest.mark.parametrize('phase,value',[('pickup',0),('drop',-1),('pickup',True)])
def test_handling_duration_rejects_nonpositive_or_noninteger(phase,value):
    from fab_sim.transfers import Transfers
    from test_routing import ring
    m,s,c,lots,events=manufacturing_factory();model=ring();model['docks']=[];model['policy'].update(pickup_us=10,drop_us=10)
    model['policy'][phase+'_us']=value
    with pytest.raises(ValueError,match='handling'):Transfers(m,model,{},lambda k,p:None)


def test_stale_pending_visit_cancels_without_blocking_next_visit_demand():
    t,m,s,c,lots,vehicles,events=factory();v=m.enter_visit('low','1');place(c,'clow','entry')
    vehicles['v1']['available']=False;did=t.request('clow',v,('port',));s.settle(0)
    lots['low']['current_visit_id']=None;t.dirty();s.settle(0)
    assert t.demands[did]['status']=='cancelled' and 'clow' not in t.active
    assert c.locations['clow']=='entry'


def test_drop_rejects_stale_visit_before_moving_from_vehicle():
    t,m,s,c,lots,vehicles,events=factory();v=m.enter_visit('low','1');place(c,'clow','entry')
    did=t.request('clow',v,('port',));s.settle(0);arrive(t,vehicles,did,'pickup');s.settle(10)
    arrive(t,vehicles,did,'drop');lots['low']['current_visit_id']=None
    with pytest.raises(RuntimeError,match='eligibility'):s.settle(20)
    assert c.locations['clow']=='vehicle:v1' and c.slots['port']['reservation']==did


@pytest.mark.parametrize('minimum',[25,50])
def test_batch_members_wait_at_origin_without_overflow_when_direct_transfer_not_feasible(minimum):
    from fractions import Fraction
    t,m,s,c,lots,vehicles,events=factory()
    m.rules.steps['r']['1'].update(batch_min=minimum,batch_max=minimum,processing_unit='batch')
    lots['low']['product_id']='p';m.batches.delivery_cost=lambda carrier,tool:Fraction(1)
    place(c,'clow','entry');v=m.enter_visit('low','1');c.reserve('port','busy')
    did=t.request('clow',v,('port',));s.settle(0)
    assert t.demands[did]['status']=='pending' and c.locations['clow']=='entry'
    assert c.slots['buffer']['reservation'] is None
    if minimum==25:
        a=next(iter(m.batches.assemblies.values()));assert c.slots['rack']['reservation']==a['id']
    else:assert not m.batches.assemblies


def test_batch_member_reuses_its_occupied_rack_only_after_new_assembly_commits():
    from fractions import Fraction
    t,m,s,c,lots,vehicles,events=factory()
    m.rules.steps['r']['1'].update(batch_min=25,batch_max=25,processing_unit='batch')
    lots['low']['product_id']='p';m.batches.delivery_cost=lambda carrier,tool:Fraction(1)
    place(c,'clow','rack');v=m.enter_visit('low','1');did=t.request('clow',v,('port',));s.settle(0)
    a=next(iter(m.batches.assemblies.values()))
    assert a['members'][0]['arrived'] and t.demands[did]['status']=='reused'
    assert c.locations['clow']=='rack' and not t.movements and not t.exchange.requests


def test_changed_batch_visit_assembly_pointer_cancels_before_pickup():
    from fractions import Fraction
    t,m,s,c,lots,vehicles,events=factory()
    m.rules.steps['r']['1'].update(batch_min=25,batch_max=25,processing_unit='batch')
    lots['low']['product_id']='p';m.batches.delivery_cost=lambda carrier,tool:Fraction(1)
    place(c,'clow','entry');v=m.enter_visit('low','1');did=t.request('clow',v,('port',));s.settle(0)
    aid=m.rules.visits[v]['assembly_id'];m.rules.visits[v]['assembly_id']='foreign'
    arrive(t,vehicles,did,'pickup')
    assert t.demands[did]['status']=='cancelled' and c.locations['clow']=='entry'
    assert c.slots['rack']['reservation']==aid and c.slots['vehicle:v1']['reservation'] is None


def test_existing_input_port_delivers_directly_to_rack_without_oht_assignment():
    t,m,s,c,lots,vehicles,events=factory();v=m.enter_visit('low','1');place(c,'clow','port')
    t.docks['port']['role']=c.slots['port']['role']='IN'
    did=t.request('clow',v,('port',));s.settle(0)
    assert t.demands[did]['status']=='delivered' and not t.movements
    assert vehicles['v1']['assignment_id'] is None and c.locations['clow']=='port'
    s.settle(1000000);assert c.locations['clow']=='rack' and m.executions


def test_consecutive_same_tool_batches_require_actual_old_rack_clearance_before_new_assembly():
    from fractions import Fraction
    from test_route_rules import step
    t,m,s,c,lots,vehicles,events=factory([step(i,batch_min=25,batch_max=25,processing_unit='batch') for i in (1,2)])
    lots['low']['product_id']='p';m.batches.delivery_cost=lambda carrier,tool:Fraction(1)
    place(c,'clow','rack');m.enter_visit('low','1');s.settle(0);s.settle(2);s.settle(12);s.settle(15)
    old=next(iter(m.batches.assemblies.values()));v=lots['low']['current_visit_id']
    did=t.request('clow',v,('port',));s.settle(15)
    assert old['status']=='finished' and len(m.batches.assemblies)==1 and t.demands[did]['status']=='pending'
    s.settle(1000015)
    assert old['status']=='cleared' and len(m.batches.assemblies)==2 and c.locations['clow']=='port'
    assert t.demands[did]['status']=='delivered' and not t.movements
    s.settle(2000015);assert c.locations['clow']=='rack'
    new=[a for a in m.batches.assemblies.values() if a['id']!=old['id']][0]
    assert new['members'][0]['arrived'] and 'clow' not in t.active
    s.settle(2000017);s.settle(2000027);s.settle(2000030)
    assert lots['low']['route_complete_at_us']==2000030 and m.tools['t']['processed_wafers']==50
