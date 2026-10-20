"""Automatic transfer/traffic coupling and event-driven safe idle clearing."""
from copy import deepcopy
from fractions import Fraction
from test_traffic import model, factory as traffic_factory,drain
from fab_sim.topology import compile_traffic
from fab_sim.geometry import overlaps


def factory(initial=10, extra_ports=()):
    from test_transfers import factory as transfer_factory
    from fab_sim.traffic import Traffic
    transfers,m,s,c,lots,vehicles,events=transfer_factory();top=transfers.model
    for pid,offset in extra_ports:
        c.slots[pid]={**c.slots['port'],'id':pid}
        dock={**transfers.docks['port'],'id':pid,'offset_um':offset}
        top['docks'].append(dock);transfers.docks[pid]=dock
    vehicles['v1']['offset_um']=initial
    top.update(nodes=[{'id':n,'zcu_id':None,'zcu_type':'NONE'} for n in ('n1','n2')],zcus=[],vehicles=list(vehicles.values()))
    top['vehicle_types'][0].update(length_um=20,gap_um=0)
    for rail in top['rails']:rail['bay_id']='bay'
    top['policy'].update(holding_pitch_um=100,raw_dock_clamp=False);compile_traffic(top)
    traffic=Traffic(top,s,vehicles,lambda k,p:events.append((k,deepcopy(p))),transfers=transfers)
    return traffic,transfers,m,s,c,lots,vehicles,events


def until(s,tick):
    s.settle(s.now_us)
    while s.next_time_us is not None and s.next_time_us<=tick:s.settle(s.next_time_us)
    if s.now_us<tick:s.settle(tick)


def test_transfer_moves_and_hoists_automatically_then_empty_oht_clears_dock():
    traffic,t,m,s,c,lots,v,e=factory();c.reserve('entry','admit');c.place('clow','entry','admit');vid=m.enter_visit('low','1')
    did=t.request('clow',vid,('port',));drain(s)
    assert lots['low']['route_complete_at_us']==1130035 and c.locations['clow']=='rack'
    assert t.demands[did]['status']=='delivered' and v['v1']['assignment_id'] is None
    assert v['v1']['offset_um']==510 and v['v1']['movement_id'] is None
    assert len([r for r in traffic.requests.values() if r['reposition']])==1
    assert not traffic.active and not traffic.hops and not traffic.targets


def test_useful_assignment_precedes_time_zero_service_dock_clearing():
    traffic,t,m,s,c,lots,v,e=factory(initial=100);c.reserve('entry','admit');c.place('clow','entry','admit');vid=m.enter_visit('low','1')
    did=t.request('clow',vid,('port',));s.settle(0)
    assert t.demands[did]['status']=='pickup_handling' and v['v1']['assignment_id']==did
    assert not [r for r in traffic.requests.values() if r['reposition']]
    assert traffic.occupancy['v1']=={'a':[(90,110)]}


def test_unassigned_transient_service_pose_clears_without_carrier_handling():
    traffic,t,m,s,c,lots,v,e=factory(initial=100);drain(s)
    assert v['v1']['offset_um']==210 and s.now_us==36667
    assert not t.demands and not t.movements and not m.executions and not c.locations
    assert len([r for r in traffic.requests.values() if r['reposition']])==1


def test_cancelled_empty_approach_stops_at_safe_holding_then_releases_movement():
    traffic,t,m,s,c,lots,v,e=factory();c.reserve('entry','admit');c.place('clow','entry','admit');vid=m.enter_visit('low','1')
    did=t.request('clow',vid,('port',));s.settle(0);until(s,10000);t.cancel(did)
    assert traffic.hops['v1']['claims']=={'a':[(0,110)]} and v['v1']['movement_id']==did+':pickup'
    until(s,30000)
    assert c.locations['clow']=='entry' and v['v1']['assignment_id'] is None
    # This hop terminates at the service dock; it clears forward without hoisting.
    drain(s);assert v['v1']['offset_um']==210 and not t.active and not traffic.active
    assert t.movements[did+':pickup']['status']=='cancelled'


def test_idle_blocker_moves_forward_until_requested_holding_is_reachable():
    from fab_sim.traffic import Traffic
    from fab_sim.scheduler import Scheduler
    top=model([{'id':'v1','type_id':'type','rail_id':'a','offset_um':500},
               {'id':'v2','type_id':'type','rail_id':'a','offset_um':1100}])
    s=Scheduler();v={r['id']:deepcopy(r) for r in top['vehicles']};e=[]
    traffic=Traffic(top,s,v,lambda k,p:e.append((k,p)),manage_idle=True)
    target=next(h['id'] for h in top['holding_points'] if h['rail_id']=='a' and h['offset_um']==4100)
    traffic.request('v1',target,'work');drain(s)
    assert traffic.requests['work']['status']=='complete' and v['v1']['offset_um']==4100 and v['v2']['offset_um']>4100
    assert not traffic.active
    assert not overlaps(traffic.occupancy['v1']['a'][0],traffic.occupancy['v2']['a'][0])


def test_unobstructing_idle_holding_vehicle_stays_and_has_no_events():
    from fab_sim.traffic import Traffic
    from fab_sim.scheduler import Scheduler
    top=model();s=Scheduler();v={r['id']:deepcopy(r) for r in top['vehicles']}
    traffic=Traffic(top,s,v,lambda k,p:None,manage_idle=True);s.settle(0)
    assert v['v1']['offset_um']==500 and not traffic.requests and s.next_time_us is None


def test_idle_clearing_does_not_reserve_a_target_beyond_a_blocking_leader():
    from fab_sim.traffic import Traffic
    from fab_sim.scheduler import Scheduler
    top=model([{'id':'v1','type_id':'type','rail_id':'a','offset_um':5000},
               {'id':'v2','type_id':'type','rail_id':'a','offset_um':6100,'assignment_id':'useful'}])
    top['docks'].append({'id':'destination','rail_id':'a','offset_um':9000})
    top=compile_traffic(top);s=Scheduler();v={r['id']:deepcopy(r) for r in top['vehicles']}
    traffic=Traffic(top,s,v,lambda k,p:None,manage_idle=True)
    s.settle(0)
    calls=s.pass_calls
    s.settle(100)
    assert s.pass_calls==calls and s.next_time_us is None
    # The idle follower must not claim the leader's next safe endpoint.
    traffic.request('v2','destination','useful');drain(s)
    assert traffic.requests['useful']['status']=='complete'
    assert v['v1']['offset_um']!=5000
    for rid,intervals in traffic.occupancy['v1'].items():
        assert not any(overlaps(a,b) for a in intervals for b in traffic.occupancy['v2'].get(rid,[]))


def test_no_holding_destination_retains_stationary_occupancy_without_polling():
    from fab_sim.traffic import Traffic
    from fab_sim.scheduler import Scheduler
    top=model([{'id':'v1','type_id':'type','rail_id':'a','offset_um':5000}]);top['holding_points']=[]
    s=Scheduler();v={r['id']:deepcopy(r) for r in top['vehicles']};events=[]
    traffic=Traffic(top,s,v,lambda k,p:events.append((k,p)),manage_idle=True);s.settle(0)
    assert traffic.idle_wait=={'v1':[]} and traffic.occupancy['v1']=={'a':[(4900,5100)]}
    assert not traffic.requests and s.next_time_us is None
    calls=s.pass_calls;s.settle(1000000);assert s.pass_calls==calls and len(events)==1


def test_idle_reassignment_occurs_at_holding_boundary_and_releases_old_target():
    traffic,t,m,s,c,lots,v,e=factory(initial=10)
    # A longer idle leg has a future target but must yield at its first safe holding.
    target=next(h['id'] for h in traffic.holdings.values() if h['rail_id']=='a' and h['offset_um']==710)
    traffic.request('v1',target,'idle-work',reposition=True);s.settle(0)
    c.reserve('entry','admit');c.place('clow','entry','admit');vid=m.enter_visit('low','1')
    did=t.request('clow',vid,('port',));s.settle(0)
    assert t.demands[did]['status']=='pending' and 'v1' in traffic.targets
    until(s,66667) # first permitted holding is 210, beyond the service dock
    assert traffic.requests['idle-work']['status']=='cancelled' and 'v1' not in traffic.targets
    assert v['v1']['assignment_id']==did and v['v1']['movement_id']==did+':pickup'
    assert t.demands[did]['status']=='assigned'
    t.cancel(did);drain(s)
    assert c.locations['clow']=='entry' and not traffic.active


def test_pending_coupled_approach_cancellation_keeps_actual_stationary_occupancy():
    traffic,t,m,s,c,lots,v,e=factory();traffic.pause('a','blocked',target_type='rail')
    c.reserve('entry','admit');c.place('clow','entry','admit');vid=m.enter_visit('low','1')
    did=t.request('clow',vid,('port',));s.settle(0);t.cancel(did);s.settle(0)
    assert traffic.requests[did+':pickup']['status']=='cancelled' and not traffic.active
    assert v['v1']['movement_id'] is None and traffic.occupancy['v1']=={'a':[(0,20)]}


def test_coupled_cancel_preflights_traffic_owner_before_releasing_any_slot():
    import pytest
    traffic,t,m,s,c,lots,v,e=factory();c.reserve('entry','admit');c.place('clow','entry','admit');vid=m.enter_visit('low','1')
    did=t.request('clow',vid,('port',));s.settle(0);v['v1']['movement_id']='corrupted'
    slots=deepcopy(c.slots);demand=deepcopy(t.demands[did]);movement=deepcopy(t.movements[did+':pickup'])
    with pytest.raises(RuntimeError,match='ownership'):t.cancel(did)
    assert c.slots==slots and t.demands[did]==demand and t.movements[did+':pickup']==movement
    assert v['v1']['assignment_id']==did and traffic.requests[did+':pickup']['cancel_requested'] is False


def test_bound_transfer_rejects_manual_arrival_before_traffic_completion():
    import pytest
    traffic,t,m,s,c,lots,v,e=factory();c.reserve('entry','admit');c.place('clow','entry','admit');vid=m.enter_visit('low','1')
    did=t.request('clow',vid,('port',));s.settle(0);v['v1'].update(t.demands[did]['pickup_pose'])
    with pytest.raises(ValueError,match='traffic'):t.arrived(did,'pickup')
    assert t.demands[did]['status']=='assigned' and c.locations['clow']=='entry'


def test_public_traffic_cancel_cannot_revoke_loaded_transfer_delivery():
    import pytest
    traffic,t,m,s,c,lots,v,e=factory();c.reserve('entry','admit');c.place('clow','entry','admit');vid=m.enter_visit('low','1')
    did=t.request('clow',vid,('port',));until(s,30010)
    assert t.demands[did]['status']=='picked_up'
    request=traffic.requests[did+':drop'];slots=deepcopy(c.slots);hop=deepcopy(traffic.hops['v1']['claims'])
    with pytest.raises(ValueError,match='pickup'):traffic.cancel(did+':drop')
    assert request['cancel_requested'] is False and c.slots==slots and traffic.hops['v1']['claims']==hop
    drain(s);assert c.locations['clow']=='rack' and t.demands[did]['status']=='delivered'


def test_public_traffic_cancel_coordinates_empty_transfer_claim_release():
    traffic,t,m,s,c,lots,v,e=factory();c.reserve('entry','admit');c.place('clow','entry','admit');vid=m.enter_visit('low','1')
    did=t.request('clow',vid,('port',));until(s,10000);traffic.cancel(did+':pickup')
    assert t.demands[did]['status']=='cancelled' and v['v1']['assignment_id'] is None
    assert all(c.slots[sid]['reservation'] is None for sid in ('entry','port','rack','vehicle:v1'))
    assert traffic.hops['v1']['claims']=={'a':[(0,110)]}
    drain(s);assert c.locations['clow']=='entry' and v['v1']['offset_um']==210
