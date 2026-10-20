"""Independent fault causes retain physical claims and sampled service clocks."""
from copy import deepcopy
from fractions import Fraction
import pytest
from test_traffic import factory as motion_factory,drain
from test_amhs_integration import factory,until


def admitted(initial=10):
    traffic,t,m,s,c,lots,v,e=factory(initial)
    c.reserve('entry','admit');c.place('clow','entry','admit');vid=m.enter_visit('low','1')
    did=t.request('clow',vid,('port',));s.settle(0)
    return traffic,t,m,s,c,lots,v,e,did


def test_oht_and_rail_pause_exact_pose_and_whole_hop_until_all_causes_clear():
    t,s,v,e=motion_factory();t.request('v1','dock','work');s.settle(0);until(s,300000)
    claims=deepcopy(t.hops['v1']['claims']);t.pause('v1','same',target_type='oht')
    until(s,400000);t.pause('a','same',target_type='rail');until(s,600000)
    assert t.pose('v1')=={'rail_id':'a','offset_um':Fraction(800)} and t.hops['v1']['claims']==claims
    t.recover('v1','same',target_type='oht');until(s,700000)
    assert t.pose('v1')['offset_um']==800 and t.hops['v1']['timer'].remaining_us==300000
    t.recover('a','same',target_type='rail');drain(s)
    assert t.requests['work']['completed_at_us']==4900000 and v['v1']['offset_um']==5000


def test_faulted_rail_denies_hop_without_acquiring_sweep_then_recovers():
    t,s,v,e=motion_factory();t.pause('a','blocked',target_type='rail');t.request('v1','dock','work');s.settle(0)
    assert t.requests['work']['status']=='pending' and not t.hops and not t.resources
    assert t.requests['work']['blockers']==[{'kind':'fault','id':'rail:a','owner':'blocked'}]
    t.recover('a','blocked',target_type='rail');drain(s);assert t.requests['work']['status']=='complete'


def test_overlapping_oht_and_port_faults_pause_pickup_but_rail_fault_does_not():
    traffic,t,m,s,c,lots,v,e,did=admitted(100);until(s,3)
    traffic.pause('a','rail',target_type='rail');traffic.pause('v1','oht',target_type='oht')
    traffic.pause('entry','port',target_type='port');until(s,20)
    assert t.demands[did]['timer'].remaining_us==7 and c.locations['clow']=='entry'
    assert c.slots['rack']['reservation']==did and traffic.occupancy['v1']=={'a':[(90,110)]}
    traffic.recover('v1','oht',target_type='oht');until(s,30);assert c.locations['clow']=='entry'
    traffic.recover('entry','port',target_type='port');until(s,37)
    assert c.locations['clow']=='vehicle:v1' and not traffic.hops
    traffic.recover('a','rail',target_type='rail');drain(s);assert c.locations['clow']=='rack'


def test_rail_fault_alone_never_pauses_stationary_pickup():
    traffic,t,m,s,c,lots,v,e,did=admitted(100);until(s,3);traffic.pause('a','rail',target_type='rail');until(s,10)
    assert c.locations['clow']=='vehicle:v1' and t.demands[did]['status']=='picked_up'
    assert not traffic.hops


def test_port_fault_before_arrival_allows_approach_but_blocks_new_handling():
    traffic,t,m,s,c,lots,v,e,did=admitted();traffic.pause('entry','port',target_type='port');until(s,30000)
    assert t.demands[did]['status']=='pickup_wait' and c.locations['clow']=='entry'
    assert v['v1']['offset_um']==100 and not traffic.hops
    traffic.recover('entry','port',target_type='port');until(s,30010)
    assert c.locations['clow']=='vehicle:v1'


def test_cancel_waiting_pickup_releases_custody_but_does_not_move_faulted_oht():
    traffic,t,m,s,c,lots,v,e,did=admitted(100);traffic.pause('v1','oht',target_type='oht');t.cancel(did);s.settle(0)
    assert c.locations['clow']=='entry' and not traffic.hops and v['v1']['offset_um']==100
    assert all(c.slots[s]['reservation'] is None for s in ('entry','port','rack','vehicle:v1'))
    traffic.recover('v1','oht',target_type='oht');drain(s);assert v['v1']['offset_um']==210


def test_port_fault_pauses_only_its_carrier_exchange_without_processing_pause():
    traffic,t,m,s,c,lots,v,e,did=admitted();until(s,130020)
    assert t.exchange.requests[did+':input']['status']=='active' and c.locations['clow']=='port'
    traffic.pause('port','port',target_type='port');until(s,1130020)
    assert c.locations['clow']=='port' and not m.causes['t']
    traffic.recover('port','port',target_type='port');until(s,2130020)
    assert c.locations['clow']=='rack' and m.executions
    traffic.pause('port','later',target_type='port');assert not m.causes['t']
    until(s,2130035);assert lots['low']['route_complete_at_us']==2130035


def test_injected_faults_settle_recovery_then_new_fault_before_dispatch():
    from fab_sim.amhs_faults import AMHSFaults
    t,s,v,e=motion_factory();faults=AMHSFaults(t,[
        {'id':'first','target_type':'oht','target_id':'v1','onset_us':100000,'duration_us':100000},
        {'id':'second','target_type':'oht','target_id':'v1','onset_us':200000,'duration_us':100000}])
    t.request('v1','dock','work');until(s,250000)
    assert t.pose('v1')['offset_um']==600 and t.causes['oht']['v1']=={'second'}
    drain(s);assert t.requests['work']['completed_at_us']==4700000
    assert all(r['status']=='recovered' for r in faults.records.values())


@pytest.mark.parametrize('bad',[
    {'target_type':'equipment'}, {'target_id':'missing'}, {'duration_us':0}, {'onset_us':-1},
    {'duration_us':True}, {'extra':0}])
def test_fault_list_is_validated_atomically_before_scheduling(bad):
    from fab_sim.amhs_faults import AMHSFaults
    t,s,v,e=motion_factory();valid={'id':'first','target_type':'oht','target_id':'v1','onset_us':0,'duration_us':10}
    invalid={**valid,'id':'second',**bad}
    with pytest.raises(ValueError):AMHSFaults(t,[valid,invalid])
    assert s.next_time_us is None and not t.causes['oht']['v1']


def test_recovery_rejects_unknown_cause_without_mutation_and_preserves_unavailable_vehicle():
    t,s,v,e=motion_factory();v['v1']['available']=False;t.pause('v1','a',target_type='oht')
    with pytest.raises(ValueError):t.recover('v1','unknown',target_type='oht')
    assert t.causes['oht']['v1']=={'a'}
    t.recover('v1','a',target_type='oht');assert v['v1']['available'] is False


@pytest.mark.parametrize('kind,target',[('oht','v1'),('port','entry')])
def test_arrival_and_new_fault_at_same_tick_do_not_start_handling_before_settlement(kind,target):
    from fab_sim.amhs_faults import AMHSFaults
    traffic,t,m,s,c,lots,v,e,did=admitted()
    AMHSFaults(traffic,[{'id':'coincident','target_type':kind,'target_id':target,'onset_us':30000,'duration_us':100}])
    until(s,30000)
    assert t.demands[did]['status']=='pickup_wait' and c.locations['clow']=='entry'
    assert not [p for k,p in e if k=='transfer_handling_started']
    until(s,30100);assert t.demands[did]['status']=='pickup_handling'
    assert t.demands[did]['timer'].remaining_us==10
    until(s,30110);assert c.locations['clow']=='vehicle:v1'


def test_drop_fault_retains_loaded_custody_and_delivery_reservations():
    traffic,t,m,s,c,lots,v,e,did=admitted();until(s,130015)
    assert t.demands[did]['status']=='drop_handling'
    traffic.pause('port','port',target_type='port');until(s,130030)
    traffic.pause('v1','oht',target_type='oht');traffic.recover('port','port',target_type='port');until(s,130040)
    assert c.locations['clow']=='vehicle:v1' and t.demands[did]['timer'].remaining_us==5
    assert c.slots['port']['reservation']==did and c.slots['rack']['reservation']==did
    traffic.recover('v1','oht',target_type='oht');until(s,130045)
    assert c.locations['clow']=='port' and t.demands[did]['status']=='delivered'


def test_faulted_exchange_cancellation_does_not_block_another_port_of_same_tool():
    traffic,t,m,s,c,lots,v,e=factory(extra_ports=(('other',450),))
    for lot,carrier,port in [('low','clow','port'),('hot','chot','other')]:
        c.reserve(port,'admit');c.place(carrier,port,'admit');m.enter_visit(lot,'1')
    t.exchange.request('first','clow','port','rack');s.settle(0);until(s,100)
    traffic.pause('port','port',target_type='port');t.exchange.cancel('first')
    t.exchange.request('second','chot','other','rack');s.settle(100)
    assert t.exchange.requests['second']['status']=='active' and traffic.causes['port']['port']=={'port'}
    until(s,1000100);assert c.locations['chot']=='rack' and c.locations['clow']=='port'


def test_empty_injected_fault_list_adds_no_events_or_causes():
    from fab_sim.amhs_faults import AMHSFaults
    t,s,v,e=motion_factory();faults=AMHSFaults(t,[])
    assert not faults.records and s.next_time_us is None and all(not cs for targets in t.causes.values() for cs in targets.values())
