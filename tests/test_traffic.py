"""Finite swept permissions and exact motion clocks on prepared physical rails."""
from copy import deepcopy
from fractions import Fraction
import pytest
from fab_sim.scheduler import Scheduler
from fab_sim.topology import compile_traffic


def model(vehicles=None,length=10000,speed='1000',half=100,pitch=1000,dock=5000):
    return compile_traffic({'nodes':[{'id':n,'zcu_id':None,'zcu_type':'NONE'} for n in ('x','y')],
        'rails':[{'id':rid,'from_node':a,'to_node':b,'length_um':length,'bay_id':'bay','curve':False,'speed_multiplier':'1'}
                 for rid,a,b in [('a','x','y'),('b','y','x')]],'zcus':[],
        'docks':[{'id':'dock','rail_id':'a','offset_um':dock}],
        'vehicle_types':[{'id':'type','length_um':2*half,'gap_um':0,'speed_um_s':speed}],
        'vehicles':vehicles or [{'id':'v1','type_id':'type','rail_id':'a','offset_um':500}],
        'policy':{'holding_pitch_um':pitch,'raw_dock_clamp':False,'curve_cap_um_s':speed}})


def factory(m=None):
    from fab_sim.traffic import Traffic
    m=m or model();s=Scheduler();events=[];vehicles={v['id']:deepcopy(v) for v in m['vehicles']}
    t=Traffic(m,s,vehicles,lambda k,p:events.append((k,deepcopy(p))))
    return t,s,vehicles,events


def drain(s):
    s.settle(s.now_us)
    while s.next_time_us is not None:s.settle(s.next_time_us)


@pytest.mark.parametrize('manage_idle',[False,True])
def test_dispatch_does_not_walk_completed_movement_history(manage_idle):
    class History(dict):
        visited = 0
        def values(self):
            for value in super().values():
                self.visited += 1
                yield value
    t,s,v,events=factory();t.manage_idle=manage_idle
    t.request('v1','dock','finished');drain(s)
    finished=t.requests['finished']
    t.requests=History({f'past:{i}':{**finished,'id':f'past:{i}'} for i in range(1000)})
    target=next(h['id'] for h in t.holdings.values() if h['rail_id']=='a' and h['offset_um']>8000)
    t.request('v1',target,'next')
    t.dispatch()
    assert t.requests['next']['status']=='moving'
    assert len(t.requests)==1001 and t.requests['past:0']['status']=='complete'
    assert t.requests.visited==0


def test_safe_hops_keep_whole_sweeps_until_arrival_then_stationary_envelope():
    t,s,v,e=factory();arrivals=[];t.request('v1','dock','move',on_complete=lambda:arrivals.append(s.now_us));s.settle(0)
    r=t.requests['move'];assert r['status']=='moving' and v['v1']['offset_um']==500
    assert t.hops['v1']['claims']=={'a':[(400,1200)]}
    s.settle(300000);assert t.pose('v1')=={'rail_id':'a','offset_um':Fraction(800)}
    assert t.hops['v1']['claims']=={'a':[(400,1200)]}
    drain(s);assert arrivals==[4500000] and v['v1']['offset_um']==5000
    assert t.occupancy['v1']=={'a':[(4900,5100)]} and not t.hops and not t.resources
    starts=[p for k,p in e if k=='traffic_hop_started'];assert len(starts)==5
    assert all(p['distance_um']>0 for p in starts)


def test_nonoverlapping_moves_share_long_parent_rail_and_touching_is_allowed():
    m=model([{'id':'v1','type_id':'type','rail_id':'a','offset_um':500},
             {'id':'v2','type_id':'type','rail_id':'a','offset_um':7000}])
    m['docks'].append({'id':'dock2','rail_id':'a','offset_um':9000})
    t,s,v,e=factory(m);t.request('v1','dock','one');t.request('v2','dock2','two');s.settle(0)
    assert set(t.hops)=={'v1','v2'}
    assert t.hops['v1']['claims']['a'][0][1]<=t.hops['v2']['claims']['a'][0][0]


def test_denied_hop_owns_no_partial_claims_and_retries_only_on_changes():
    m=model([{'id':'v1','type_id':'type','rail_id':'a','offset_um':500},
             {'id':'v2','type_id':'type','rail_id':'a','offset_um':1100}])
    t,s,v,e=factory(m);t.request('v1','dock','one');s.settle(0)
    r=t.requests['one'];assert r['status']=='pending' and 'v1' not in t.hops and not t.resources
    assert r['blockers'][0]['owner']=='v2' and t.occupancy['v1']=={'a':[(400,600)]}
    calls=s.pass_calls;s.settle(2000000);assert s.pass_calls==calls and s.next_time_us is None
    t.request('v2','dock','two');drain(s)
    assert v['v1']['offset_um']<5000 and r['status']=='pending' # terminal service dock remains occupied
    assert r['cumulative_seconds']>0 and r['wait_us']>0


def test_hop_rounding_carries_cumulative_duration_and_zero_ticks_settle_causally():
    m=model([{'id':'v1','type_id':'type','rail_id':'a','offset_um':1}],length=20,speed='3000000',half=1,pitch=2,dock=19)
    t,s,v,e=factory(m);t.request('v1','dock','tiny');drain(s)
    r=t.requests['tiny'];assert s.now_us==6 and r['cumulative_seconds']==Fraction(18,3000000)
    assert any(p['duration_us']==0 for k,p in e if k=='traffic_hop_started')
    assert r['status']=='complete' and v['v1']['offset_um']==19


def test_same_dock_has_no_zero_distance_hop_and_preserves_occupancy():
    m=model([{'id':'v1','type_id':'type','rail_id':'a','offset_um':5000}])
    t,s,v,e=factory(m);done=[];t.request('v1','dock','same',on_complete=lambda:done.append(s.now_us));s.settle(0)
    assert done==[0] and not t.hops and s.next_time_us is None
    assert not [k for k,p in e if k=='traffic_hop_started']
    assert t.occupancy['v1']=={'a':[(4900,5100)]}


def test_service_docks_are_not_intermediate_hop_stops():
    m=model();m['docks'].append({'id':'other','rail_id':'a','offset_um':1500})
    # Recompile to retain source geometry exclusions around both docks.
    m=compile_traffic(m);t,s,v,e=factory(m);t.request('v1','dock','one');drain(s)
    assert all(p['target_pose']['offset_um']!=1500 for k,p in e if k=='traffic_hop_started')


def test_reposition_destination_claim_persists_while_path_is_blocked():
    m=model([{'id':'v1','type_id':'type','rail_id':'a','offset_um':500},
             {'id':'v2','type_id':'type','rail_id':'a','offset_um':1100}])
    t,s,v,e=factory(m);target=next(h['id'] for h in m['holding_points'] if h['rail_id']=='a' and h['offset_um']==3100)
    t.request('v1',target,'idle',reposition=True);s.settle(0)
    assert t.targets['v1']=={'a':[(3000,3200)]} and 'v1' not in t.hops
    t.cancel('idle');assert 'v1' not in t.targets and t.occupancy['v1']=={'a':[(400,600)]}


def test_cancel_moving_request_retains_whole_hop_to_safe_endpoint():
    t,s,v,e=factory();t.request('v1','dock','one');s.settle(0);s.settle(300000)
    t.cancel('one');assert t.hops['v1']['claims']=={'a':[(400,1200)]}
    assert t.requests['one']['status']=='moving' and v['v1']['movement_id']=='one'
    s.settle(600000);assert v['v1']['offset_um']==1100 and t.requests['one']['status']=='cancelled'
    assert not t.hops and v['v1']['movement_id'] is None


def short_links(two_vehicles=False):
    nodes=[{'id':n,'zcu_id':None,'zcu_type':'NONE'} for n in ('n0','n1','n2','n3')]
    nodes[1].update(zcu_id='z',zcu_type='STOP');nodes[2].update(zcu_id='z',zcu_type='RESET')
    vehicles=[{'id':'v1','type_id':'type','rail_id':'a','offset_um':500}]
    if two_vehicles:vehicles.append({'id':'v2','type_id':'type','rail_id':'c','offset_um':900})
    return compile_traffic({'nodes':nodes,'rails':[
        {'id':rid,'from_node':a,'to_node':b,'length_um':length,'bay_id':'bay','curve':False,'speed_multiplier':'1'}
        for rid,a,b,length in [('a','n0','n1',1000),('s','n1','n2',50),('t','n2','n3',1000),('b','n3','n0',1000),('c','n0','n2',1050)]],
        'zcus':[{'id':'z'}],'docks':[{'id':'dock','rail_id':'t','offset_um':500}],
        'vehicle_types':[{'id':'type','length_um':200,'gap_um':0,'speed_um_s':'1000'}],
        'vehicles':vehicles,'policy':{'holding_pitch_um':1000,'raw_dock_clamp':False,'curve_cap_um_s':'1000'}})


def test_short_links_are_swept_whole_and_junction_zcu_claims_retained_to_arrival():
    t,s,v,e=factory(short_links());t.request('v1','dock','one');s.settle(0)
    hop=t.hops['v1']
    assert hop['claims']=={'a':[(400,1000)],'s':[(0,50)],'t':[(0,200)]}
    assert t.resources=={'junction:n2':'v1','zcu:z':'v1'}
    s.settle(625000);assert t.pose('v1')=={'rail_id':'t','offset_um':Fraction(75)}
    assert t.hops['v1'] is hop and t.resources=={'junction:n2':'v1','zcu:z':'v1'}
    s.settle(650000);assert not t.resources and t.occupancy['v1']=={'t':[(0,200)]}
    drain(s);assert s.now_us==1050000 and v['v1']['offset_um']==500


def test_conflicting_passage_permission_never_partially_claims_free_zcu_or_rail():
    t,s,v,e=factory(short_links(True));t.request('v2','dock','a-first');t.request('v1','dock','b-second');s.settle(0)
    assert 'v2' in t.hops and 'v1' not in t.hops
    assert t.resources=={'junction:n2':'v2'} and 'zcu:z' not in t.resources
    assert t.occupancy['v1']=={'a':[(400,600)]}
    assert {'kind':'passage','id':'junction:n2','owner':'v2'} in t.requests['b-second']['blockers']


def test_motion_follows_directed_return_for_a_behind_destination():
    m=model();m['docks'].append({'id':'behind','rail_id':'a','offset_um':200})
    m=compile_traffic(m);t,s,v,e=factory(m);t.request('v1','behind','one');drain(s)
    assert s.now_us==19700000 and v['v1']['offset_um']==200
    assert any(p['rail_id']=='b' for k,hop in e if k=='traffic_hop_started' for p in hop['parts'])


def test_actual_transfer_callbacks_start_hoisting_only_after_traffic_arrival():
    from test_transfers import factory as transfer_factory
    from fab_sim.traffic import Traffic
    transfers,m,s,c,lots,vehicles,events=transfer_factory();top=transfers.model
    vehicles['v1']['offset_um']=10
    top.update(nodes=[{'id':n,'zcu_id':None,'zcu_type':'NONE'} for n in ('n1','n2')],zcus=[],vehicles=list(vehicles.values()))
    top['vehicle_types'][0].update(length_um=20,gap_um=0)
    for rail in top['rails']:rail['bay_id']='bay'
    top['policy'].update(holding_pitch_um=100,raw_dock_clamp=False);compile_traffic(top)
    t=Traffic(top,s,vehicles,lambda k,p:None);v=m.enter_visit('low','1')
    c.reserve('entry','admit');c.place('clow','entry','admit')
    did=transfers.request('clow',v,('port',));s.settle(0);mid=did+':pickup'
    t.request('v1','entry',mid,path=transfers.movements[mid]['path'],
        on_start=lambda:transfers.movement_started(mid),on_complete=lambda:transfers.arrived(did,'pickup'))
    s.settle(0);assert c.locations['clow']=='entry' and transfers.demands[did]['status']=='assigned'
    drain(s);assert c.locations['clow']=='vehicle:v1' and s.now_us==30010
    assert t.occupancy['v1']=={'a':[(90,110)]}
    drop=did+':drop';t.request('v1','port',drop,path=transfers.movements[drop]['path'],
        on_start=lambda:transfers.movement_started(drop),on_complete=lambda:transfers.arrived(did,'drop'))
    drain(s);assert c.locations['clow']=='rack' and s.now_us==1130035
    assert lots['low']['route_complete_at_us']==1130035


def test_stale_vehicle_movement_owner_rejects_arrival_without_freeing_sweep():
    t,s,v,e=factory();t.request('v1','dock','one');s.settle(0)
    v['v1']['movement_id']='foreign'
    with pytest.raises(RuntimeError,match='ownership'):s.settle(600000)
    assert t.hops['v1']['claims']=={'a':[(400,1200)]} and v['v1']['offset_um']==500


def test_generated_geometry_speed_sections_reach_dock_at_exact_leg_duration():
    from test_generation import generate
    from fab_sim.routing import free_flow_path
    from fab_sim.timebase import to_ticks
    m=generate()['model'];t,s,v,e=factory(m);vid=min(v)
    candidates=[(free_flow_path(m,v[vid],dock,v[vid]['type_id'])['seconds'],dock['id']) for dock in m['docks']]
    seconds,dock=min(candidates);t.request(vid,dock,'generated');drain(s)
    assert s.now_us==to_ticks(seconds) and t.requests['generated']['cumulative_seconds']==seconds
    target=next(d for d in m['docks'] if d['id']==dock)
    assert t.pose(vid)=={k:target[k] for k in ('rail_id','offset_um')}
    assert not t.resources and not t.hops


def test_mixed_type_protection_uses_largest_declared_gap_and_actual_type_length():
    m=model();m['vehicle_types'].append({'id':'large','length_um':400,'gap_um':100,'speed_um_s':'800'})
    m['vehicles'].append({'id':'v2','type_id':'large','rail_id':'a','offset_um':1300})
    m=compile_traffic(m);t,s,v,e=factory(m)
    assert t.occupancy['v1']=={'a':[(350,650)]} and t.occupancy['v2']=={'a':[(1050,1550)]}


def test_stable_request_id_arbitrates_conflicting_hops_before_shorter_travel():
    t,s,v,e=factory(short_links(True));t.request('v2','dock','z-short');t.request('v1','dock','a-long');s.settle(0)
    assert set(t.hops)=={'v1'} and t.requests['z-short']['status']=='pending'


def test_unchanged_wait_fingerprint_emits_once_when_notifications_coalesce():
    m=model([{'id':'v1','type_id':'type','rail_id':'a','offset_um':500},
             {'id':'v2','type_id':'type','rail_id':'a','offset_um':1100}])
    t,s,v,e=factory(m);t.request('v1','dock','one');s.settle(0)
    t.dirty();t.dirty();s.settle(0)
    assert len([p for k,p in e if k=='traffic_wait_changed'])==1


@pytest.mark.parametrize('mutation',['origin','reverse','cost'])
def test_invalid_selected_path_rejects_before_vehicle_or_claim_mutation(mutation):
    from fab_sim.routing import free_flow_path
    t,s,v,e=factory();path=free_flow_path(t.model,v['v1'],t.docks['dock'],'type')
    if mutation=='origin':path['sublinks'][0]['start_um']=501
    elif mutation=='reverse':path['sublinks'][0]['end_um']=400
    else:path['seconds']+=1
    with pytest.raises(ValueError):t.request('v1','dock','bad',path=path)
    assert not t.requests and not t.active and not t.hops and v['v1']['movement_id'] is None


def test_wait_at_same_dock_is_recorded_before_zero_travel_arrival():
    m=model([{'id':'v1','type_id':'type','rail_id':'a','offset_um':5000}])
    t,s,v,e=factory(m);v['v1']['available']=False;t.request('v1','dock','same');s.settle(0)
    s.settle(1000000);v['v1']['available']=True;t.dirty();s.settle(1000000)
    assert t.requests['same']['wait_us']==1000000 and t.requests['same']['completed_at_us']==1000000


@pytest.mark.parametrize('action',['zero_arrival','pending_cancel','pending_start'])
def test_stale_movement_owner_is_preflighted_before_pending_state_changes(action):
    if action=='zero_arrival':
        t,s,v,e=factory(model([{'id':'v1','type_id':'type','rail_id':'a','offset_um':5000}]))
        t.request('v1','dock','one')
    else:
        m=model([{'id':'v1','type_id':'type','rail_id':'a','offset_um':500},
                 {'id':'v2','type_id':'type','rail_id':'a','offset_um':1100}])
        t,s,v,e=factory(m);t.request('v1','dock','one')
        if action=='pending_cancel':s.settle(0)
    v['v1']['movement_id']='foreign';before=deepcopy(t.occupancy)
    with pytest.raises(RuntimeError,match='ownership'):
        if action=='pending_cancel':t.cancel('one')
        else:s.settle(0)
    assert v['v1']['movement_id']=='foreign' and t.requests['one']['status']=='pending'
    assert not t.requests['one']['cancel_requested'] and t.active['v1']=='one'
    assert t.occupancy==before and not t.hops and not t.resources


def test_legal_self_loop_returns_forward_across_its_parent_rail_boundary():
    m=compile_traffic({'nodes':[{'id':'n','zcu_id':None,'zcu_type':'NONE'}],
        'rails':[{'id':'loop','from_node':'n','to_node':'n','length_um':1000,'bay_id':'bay','curve':False,'speed_multiplier':'1'}],
        'zcus':[],'docks':[{'id':'dock','rail_id':'loop','offset_um':400}],
        'vehicle_types':[{'id':'type','length_um':200,'gap_um':0,'speed_um_s':'1000'}],
        'vehicles':[{'id':'v1','type_id':'type','rail_id':'loop','offset_um':800}],
        'policy':{'holding_pitch_um':200,'raw_dock_clamp':False,'curve_cap_um_s':'1000'}})
    t,s,v,e=factory(m);t.request('v1','dock','loop');drain(s)
    assert s.now_us==600000 and v['v1']['offset_um']==400


def test_incrementally_maintained_claim_index_matches_a_full_rebuild():
    def normal(index):
        return {rid:sorted((vid,tuple(interval)) for vid,interval in entries) for rid,entries in index.items() if entries}
    m=model([{'id':'v1','type_id':'type','rail_id':'a','offset_um':500},
             {'id':'v2','type_id':'type','rail_id':'a','offset_um':2500},
             {'id':'v3','type_id':'type','rail_id':'b','offset_um':4000}])
    t,s,v,e=factory(m);t.manage_idle=True;t._claim_index()
    holds=[h['id'] for h in m['holding_points']]
    t.request('v1','dock','to-dock');t.request('v2',holds[-1],'far');t.request('v3',holds[len(holds)//2],'idle',reposition=True)
    checks=0
    s.settle(s.now_us)
    while s.next_time_us is not None:
        assert normal(t._claim_index())==normal(t._build_claim_index()[0]);checks+=1
        s.settle(s.next_time_us)
    t.request('v1',holds[0],'back');t.cancel('back') if t.requests['back']['status']=='pending' else None
    drain(s)
    assert normal(t._claim_index())==normal(t._build_claim_index()[0]) and checks>5
