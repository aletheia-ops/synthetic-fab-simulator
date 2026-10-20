from copy import deepcopy
from types import SimpleNamespace
from fractions import Fraction
import pytest
from fab_sim.topology import compile_traffic
from test_traffic import factory

def cycle(*,parked=False,unreachable=False):
    raw={'nodes':[{'id':n,'zcu_id':None,'zcu_type':'NONE'} for n in ('x','y','z')],
      'rails':[{'id':rid,'from_node':a,'to_node':b,'length_um':1000,'bay_id':'bay','curve':False,'speed_multiplier':'1'}
        for rid,a,b in [('a','x','y'),('b','y','x'),('c','y','z'),('d','z','x')]],
      'zcus':[],'docks':[{'id':'goal1','rail_id':'b','offset_um':900},{'id':'goal2','rail_id':'a','offset_um':900}],
      'vehicle_types':[{'id':'type','length_um':200,'gap_um':0,'speed_um_s':'1000'}],
      'vehicles':[{'id':'v1','type_id':'type','rail_id':'a','offset_um':100},
                  {'id':'v2','type_id':'type','rail_id':'b','offset_um':100}],
      'policy':{'holding_pitch_um':1000,'raw_dock_clamp':False,'curve_cap_um_s':'1000'}}
    if parked:raw['vehicles'].append({'id':'parked','type_id':'type','rail_id':'c','offset_um':100})
    if unreachable:raw['rails']=[r for r in raw['rails'] if r['id']!='d']
    m=compile_traffic(raw)
    t,s,v,e=factory(m);t.manage_idle=False
    movements={};demands={};slots={};locations={}
    for i in (1,2):
        vid=f'v{i}';did=f'd{i}';mid=did+':drop';goal=f'goal{i}';carrier=f'carrier{i}';slot=f'vehicle{i}'
        path=t._router('type').path(v[vid],t.docks[goal])
        t.request(vid,goal,mid,path=path);v[vid].update(assignment_id=did,carrier_slot_id=slot)
        movements[mid]={'id':mid,'demand_id':did,'vehicle_id':vid,'phase':'drop','path':deepcopy(path),
            'status':'moving','cancelled':False,'target_pose':dict(t.requests[mid]['target_pose'])}
        demands[did]={'id':did,'status':'picked_up','carrier_id':carrier,'vehicle_id':vid,
            'destination_id':goal,'rack_slot_id':None,'loaded_path':deepcopy(path)}
        slots[slot]={'carrier_id':carrier,'reservation':did};locations[carrier]=slot
        slots[goal]={'carrier_id':None,'reservation':did}
    t.transfers=SimpleNamespace(movements=movements,demands=demands,custody=SimpleNamespace(slots=slots,locations=locations),dirty=lambda:None)
    s.settle(0)
    assert not t.hops
    return t,s,v,e

def test_closed_body_cycle_uses_legal_detour_without_changing_custody_or_destination():
    t,s,v,e=cycle();before=deepcopy(t.transfers.custody.slots)
    s.settle(1);t.dirty();s.settle(1)
    amendments=[p for k,p in e if k=='traffic_route_amended']
    assert len(amendments)==1
    assert t.hops and t.requests['d1:drop']['status']=='moving'
    r=t.requests['d1:drop'];m=t.transfers.movements[r['id']]
    assert r['destination_pose_id']=='goal1' and v['v1']['assignment_id']=='d1'
    assert t.transfers.custody.slots==before
    assert m['path']==t.transfers.demands['d1']['loaded_path']
    assert m['path']['seconds']==t._duration(m['path']['sublinks'],'type')
    assert r['cumulative_seconds']==Fraction(0) and r['boundary_us']==0
    assert t.hops['v1']['claims'].keys()=={'a','c'}

@pytest.mark.parametrize('problem',['fault','cancelled','custody','reservation','not_persistent'])
def test_recovery_rejects_invalid_cycle_member_or_route(problem):
    t,s,v,e=cycle()
    if problem=='fault':t.causes['rail']['c'].add('fault')
    elif problem=='cancelled':t.requests['d1:drop']['cancel_requested']=True
    elif problem=='custody':t.transfers.custody.locations['carrier1']='foreign'
    elif problem=='reservation':t.transfers.custody.slots['goal1']['reservation']='foreign'
    if problem!='not_persistent':s.settle(1)
    t.dirty();s.settle(s.now_us)
    assert not [p for k,p in e if k=='traffic_route_amended']

def test_recovery_retains_exact_previously_traveled_prefix():
    t,s,v,e=cycle();r=t.requests["d1:drop"];m=t.transfers.movements[r["id"]]
    prefix=[{"id":"b@900:1000","rail_id":"b","start_um":900,"end_um":1000},{"id":"a@0:1000","rail_id":"a","start_um":0,"end_um":100}]
    m["path"]["sublinks"]=[prefix[0],{**m["path"]["sublinks"][0],"start_um":0,"id":"a@0:1000"}]+m["path"]["sublinks"][1:]
    m["path"]["seconds"]=t._duration(m["path"]["sublinks"],"type")
    r["cumulative_seconds"]=Fraction(1,5);r["boundary_us"]=200000;r["started_at_us"]=0
    s.settle(500000);t.dirty();s.settle(500000)
    assert m["path"]["sublinks"][:2]==prefix
    assert r["cumulative_seconds"]==Fraction(1,5) and r["boundary_us"]==200000
    assert t.hops["v1"]["duration_us"]==1000000
    assert m["path"]["seconds"]==r["cumulative_seconds"]+t._duration(r["remaining"],"type")


def test_unchanged_failed_recovery_does_not_repeat_waypoint_search():
    from fab_sim.probes import Probes
    t,s,v,e=cycle();p=Probes();t.transfers.probes=p;t.causes["rail"]["c"].add("fault")
    s.settle(1);t.dirty();s.settle(1)
    searched=p.work_counts["traffic_cycle_recovery_searches"]
    assert searched==1
    for tick in (2,3,4):s.settle(tick);t.dirty();s.settle(tick)
    assert p.work_counts["traffic_cycle_recovery_searches"]==searched
    assert p.work_counts["traffic_denied_checks_reused"]>0
    t.causes["rail"]["c"].clear();t.dirty();s.settle(4)
    assert p.work_counts["traffic_cycle_route_amendments"]==1


def test_persistent_cycle_recovers_at_settled_boundary_without_dirty_traffic():
    t,s,v,e=cycle()
    assert not s.dirty and not t.hops
    s.settle(1)
    assert t.hops and any(k=="traffic_route_amended" for k,p in e)


def test_amended_waiter_remains_in_cycle_so_other_member_can_recover():
    t,s,v,e=cycle()
    # Missing prior elapsed evidence prevents a repeat, but its body wait still participates.
    t.requests["d2:drop"]["route_recovery"]={"policy":"prior"}
    s.settle(1)
    assert t.hops and t.requests["d1:drop"]["route_recovery"]
    assert len([p for k,p in e if k=="traffic_route_amended"])==1


@pytest.mark.parametrize("problem",["occupied","unreachable"])
def test_recovery_never_bypasses_occupied_waypoint_or_unreachable_goal(problem):
    if problem=="unreachable":
        # Directed return reachability is enforced before Traffic is allocated.
        with pytest.raises(ValueError,match="directed return"):cycle(unreachable=True)
        return
    t,s,v,e=cycle(parked=True)
    before=deepcopy(t.transfers.custody.slots)
    s.settle(1)
    assert not t.hops and not any(k=="traffic_route_amended" for k,p in e)
    assert t.transfers.custody.slots==before


@pytest.fixture
def recovery_cycle_searches(monkeypatch):
    """Observe the real SCC algorithm; never replace its result or effects."""
    import fab_sim.traffic_cycle_recovery as recovery
    calls=[]
    original=recovery.strongly_connected_cycles
    def counted(graph):
        calls.append(deepcopy(graph))
        return original(graph)
    monkeypatch.setattr(recovery, "strongly_connected_cycles", counted)
    return calls


def test_unchanged_eligible_graph_reuses_cycle_search_even_when_physical_fault_changes(recovery_cycle_searches):
    t,s,v,e=cycle();calls=recovery_cycle_searches
    t.causes['rail']['c'].add('fault');s.now_us=1
    assert not t._recover_cycles()
    searched=len(calls)
    for tick in (2,3,4):
        s.now_us=tick
        assert not t._recover_cycles()
    assert len(calls)==searched
    # The broad physical recovery signature must still wake route search.
    t.causes['rail']['c'].clear()
    assert t._recover_cycles()
    assert len(calls)==searched
    amendments=[p for k,p in e if k=='traffic_route_amended']
    assert len(amendments)==1
    assert amendments[0]['cycle_members']==['v1','v2']


def test_empty_cycle_result_reused_but_silent_edge_mutations_wake_search(recovery_cycle_searches):
    t,s,v,e=cycle();calls=recovery_cycle_searches;s.now_us=1
    saved=deepcopy(t.requests['d1:drop']['blockers'])
    t.requests['d1:drop']['blockers']=[]
    assert not t._recover_cycles()
    searched=len(calls)
    assert not t._recover_cycles()
    assert len(calls)==searched
    t.requests['d1:drop']['blockers']=deepcopy(saved)
    t.causes['rail']['c'].add('fault')
    assert not t._recover_cycles()
    assert len(calls)==searched+1
    assert calls[-1]=={'v1':('v2',),'v2':('v1',)}
    t.requests['d1:drop']['blockers'].clear()
    assert not t._recover_cycles()
    assert len(calls)==searched+2
    assert calls[-1]=={'v2':()}
    # Revisiting an old graph recomputes: retention is one current result only.
    t.requests['d1:drop']['blockers']=deepcopy(saved)
    assert not t._recover_cycles()
    assert len(calls)==searched+3


@pytest.mark.parametrize('problem',['custody','reservation','fault','movement_status','clock'])
def test_warm_cycle_result_rechecks_live_member_eligibility(problem,recovery_cycle_searches):
    t,s,v,e=cycle();calls=recovery_cycle_searches;s.now_us=1
    t.causes['rail']['c'].add('fault')
    assert not t._recover_cycles()
    searched=len(calls)
    if problem=='custody':t.transfers.custody.locations['carrier1']='foreign'
    elif problem=='reservation':t.transfers.custody.slots['goal1']['reservation']='foreign'
    elif problem=='fault':t.causes['oht']['v1'].add('fault')
    elif problem=='movement_status':t.transfers.movements['d1:drop']['status']='completed'
    else:t.requests['d1:drop']['waiting_since_us']=s.now_us
    t.causes['rail']['c'].clear()
    assert not t._recover_cycles()
    assert len(calls)==searched+1
    assert calls[-1]=={'v2':()}
    assert not any(k=='traffic_route_amended' for k,p in e)
    if problem=='custody':t.transfers.custody.locations['carrier1']='vehicle1'
    elif problem=='reservation':t.transfers.custody.slots['goal1']['reservation']='d1'
    elif problem=='fault':t.causes['oht']['v1'].clear()
    elif problem=='movement_status':t.transfers.movements['d1:drop']['status']='moving'
    else:s.now_us+=1
    assert t._recover_cycles()
    assert len(calls)==searched+2
    assert len([p for k,p in e if k=='traffic_route_amended'])==1


def test_recovery_cycle_cache_is_derived_and_cold_after_restore(tmp_path,recovery_cycle_searches):
    from test_runner import fixture
    from fab_sim.runner import build_runtime,load_config
    from fab_sim.runtime_state import capture_runtime,restore_runtime,runtime_digest
    config=load_config(fixture(tmp_path))
    owners=build_runtime(config,lambda *args:None,initialize=True)
    owners.scheduler.settle(0)
    assert owners.traffic._recovery_cycle_cache is not None
    state=capture_runtime(owners);digest=runtime_digest(owners)
    # Derived observation does not enter canonical physical state or checkpoint.
    owners.traffic._recovery_cycle_cache=(('stale',), (('stale',),))
    assert capture_runtime(owners)==state
    assert runtime_digest(owners)==digest
    restore_runtime(owners,state,owners.actions)
    assert owners.traffic._recovery_cycle_cache is None
    assert runtime_digest(owners)==digest
    searched=len(recovery_cycle_searches)
    # No eligible member: the cold cache is rebuilt as the empty graph without
    # running the SCC search on it, then reused.
    assert not owners.traffic._recover_cycles()
    assert owners.traffic._recovery_cycle_cache==((),())
    assert not owners.traffic._recover_cycles()
    assert len(recovery_cycle_searches)==searched



def test_recovery_cycle_work_counts_distinguish_searches_and_empty_or_cycle_reuse():
    from fab_sim.probes import Probes
    t,s,v,e=cycle();p=Probes();t.transfers.probes=p
    assert not t._recover_cycles()  # The initial nonpersistent empty graph is cached.
    assert p.work_counts.get('traffic_cycle_scc_reuses',0)==1
    assert p.work_counts.get('traffic_cycle_scc_searches',0)==0
    s.now_us=1;t.causes['rail']['c'].add('fault')
    assert not t._recover_cycles()
    assert p.work_counts['traffic_cycle_scc_searches']==1
    s.now_us=2
    assert not t._recover_cycles()
    assert p.work_counts['traffic_cycle_scc_reuses']==2
    t.causes['rail']['c'].clear()
    assert t._recover_cycles()
    assert p.work_counts['traffic_cycle_scc_searches']==1
    assert p.work_counts['traffic_cycle_scc_reuses']==3
    assert p.work_counts['traffic_cycle_route_amendments']==1


def mixed_cycle():
    t,s,v,e=cycle()
    t.requests.pop(t.active.pop('v2'))
    t.targets.pop('d2:drop',None)
    t.transfers.movements.pop('d2:drop')
    t.transfers.demands.pop('d2')
    v['v2'].update(assignment_id=None,movement_id=None)
    t.transfers.custody.slots['vehicle2'].update(carrier_id=None,reservation=None)
    t.transfers.custody.locations.pop('carrier2')
    t.manage_idle=True
    t.dirty();s.settle(0)
    assert 'v2' in t.idle_wait and 'v2' not in t.active
    return t,s,v,e


def test_mixed_empty_and_assigned_body_cycle_recovers_without_assigning_empty_member():
    t,s,v,e=mixed_cycle();before=deepcopy(t.transfers.custody.slots)
    s.settle(1)
    amendments=[p for k,p in e if k=='traffic_route_amended']
    assert len(amendments)==1
    assert amendments[0]['cycle_members']==['v1','v2']
    assert amendments[0]['vehicle_id']=='v1' and 'v1' in t.hops
    assert v['v2']['assignment_id'] is None
    assert t.transfers.custody.slots==before


@pytest.mark.parametrize('problem',['assigned','loaded','movement','unavailable','fault'])
def test_mixed_cycle_rechecks_empty_member_live_eligibility(problem):
    t,s,v,e=mixed_cycle();s.now_us=1
    if problem=='assigned':v['v2']['assignment_id']='foreign'
    elif problem=='loaded':t.transfers.custody.slots['vehicle2']['carrier_id']='foreign'
    elif problem=='movement':v['v2']['movement_id']='foreign'
    elif problem=='unavailable':v['v2']['available']=False
    else:t.causes['oht']['v2'].add('fault')
    assert not t._recover_cycles()
    assert not any(k=='traffic_route_amended' for k,p in e)


def test_mixed_cycle_uses_selected_first_hop_not_union_of_idle_waits(monkeypatch):
    t,s,v,e=mixed_cycle();s.now_us=1
    assert any(b['owner']=='v1' for b in t.idle_wait['v2'])
    original=t._blockers
    def no_current_first_hop(owner,claims,resources,**kwargs):
        if owner=='v2' and resources:return []
        return original(owner,claims,resources,**kwargs)
    monkeypatch.setattr(t,'_blockers',no_current_first_hop)
    assert not t._recover_cycles()
    assert not any(k=='traffic_route_amended' for k,p in e)


def test_mixed_cycle_failed_search_is_reused_but_cleared_fault_wakes():
    from fab_sim.probes import Probes
    t,s,v,e=mixed_cycle();p=Probes();t.transfers.probes=p;s.now_us=1
    t.causes['rail']['d'].add('fault')
    assert not t._recover_cycles()
    assert p.work_counts['traffic_cycle_recovery_searches']==1
    s.now_us=2
    assert not t._recover_cycles()
    assert p.work_counts['traffic_cycle_recovery_searches']==1
    t.causes['rail']['d'].clear()
    assert t._recover_cycles()
    assert p.work_counts['traffic_cycle_route_amendments']==1


def test_clearing_edge_fault_gate_matches_first_hop_not_later_route():
    from fab_sim.traffic_cycle_recovery import _clearing_edges
    t,s,v,e=mixed_cycle();s.now_us=1
    t.causes['rail']['c'].add('later-fault')
    edges,selected=_clearing_edges(t,{'v1':t.requests['d1:drop']},t._claim_index())
    assert edges=={'v2':('v1',)}
    t.causes['rail']['b'].add('first-hop-fault')
    edges,selected=_clearing_edges(t,{'v1':t.requests['d1:drop']},t._claim_index())
    assert edges=={}


def previously_amended_after_hop():
    """Hand-built traveled prefix: b900->1000 plus a0->100 takes .2 seconds."""
    t,s,v,e=cycle()
    r=t.requests['d1:drop'];m=t.transfers.movements[r['id']]
    m['path']['sublinks']=[{'id':'b@900:1000','rail_id':'b','start_um':900,'end_um':1000},
        {**m['path']['sublinks'][0],'start_um':0}]+m['path']['sublinks'][1:]
    m['path']['seconds']=t._duration(m['path']['sublinks'],'type')
    r['cumulative_seconds']=Fraction(1,5);r['boundary_us']=200000;r['started_at_us']=0
    r['route_recovery']={'policy':'prior','traveled_seconds':Fraction(0),
        'recovery_poses':[{'rail_id':'b','offset_um':900}]}
    # The other member has no completed hop after its amendment.
    t.requests['d2:drop']['route_recovery']={'policy':'prior','traveled_seconds':Fraction(0)}
    s.now_us=500000
    return t,s,v,e


def test_completed_hop_allows_another_safe_recovery_preserving_original_destination():
    t,s,v,e=previously_amended_after_hop();before=deepcopy(t.transfers.custody.slots)
    assert t._recover_cycles()
    t.dirty();s.settle(s.now_us)
    assert 'v1' in t.hops
    assert t.requests['d1:drop']['destination_pose_id']=='goal1'
    assert t.transfers.custody.slots==before
    assert t.transfers.movements['d1:drop']['path']['sublinks'][:2]==[
        {'id':'b@900:1000','rail_id':'b','start_um':900,'end_um':1000},
        {'id':'a@100:1000','rail_id':'a','start_um':0,'end_um':100}]
    assert len([p for k,p in e if k=='traffic_route_amended'])==1


@pytest.mark.parametrize('prior',['no_hop','visited_origin','missing_elapsed'])
def test_repeat_recovery_never_retries_without_progress_or_at_visited_pose(prior):
    t,s,v,e=previously_amended_after_hop();r=t.requests['d1:drop']
    if prior=='no_hop':r['route_recovery']['traveled_seconds']=r['cumulative_seconds']
    elif prior=='visited_origin':r['route_recovery']['recovery_poses'].append({'rail_id':'a','offset_um':100})
    else:r['route_recovery'].pop('traveled_seconds')
    before=deepcopy((t.requests,t.transfers.movements,t.transfers.custody.slots))
    for tick in (500000,600000,700000):
        s.now_us=tick
        assert not t._recover_cycles()
    assert not any(k=='traffic_route_amended' for k,p in e)
    assert (t.requests,t.transfers.movements,t.transfers.custody.slots)==before


def test_repeat_recovery_does_not_detour_back_to_a_previous_recovery_position():
    t,s,v,e=previously_amended_after_hop();r=t.requests['d1:drop']
    r['route_recovery']['recovery_poses'].append({'rail_id':'c','offset_um':100})
    assert not t._recover_cycles()
    assert not any(k=='traffic_route_amended' for k,p in e)


@pytest.mark.parametrize('problem',['fault','custody','reservation','cancelled','destination'])
def test_repeat_recovery_keeps_live_safety_and_ownership_guards(problem):
    t,s,v,e=previously_amended_after_hop()
    if problem=='fault':t.causes['rail']['c'].add('fault')
    elif problem=='custody':t.transfers.custody.locations['carrier1']='foreign'
    elif problem=='reservation':t.transfers.custody.slots['goal1']['reservation']='foreign'
    elif problem=='cancelled':t.requests['d1:drop']['cancel_requested']=True
    else:t.transfers.movements['d1:drop']['target_pose']={'rail_id':'b','offset_um':100}
    assert not t._recover_cycles()
    assert not any(k=='traffic_route_amended' for k,p in e)


def test_repeat_recovery_history_is_independent_between_request_movement_and_event():
    t,s,v,e=previously_amended_after_hop()
    assert t._recover_cycles()
    r=t.requests['d1:drop'];m=t.transfers.movements[r['id']]
    event=next(p for k,p in e if k=='traffic_route_amended')
    assert r['route_recovery']['recovery_poses']==[
        {'rail_id':'b','offset_um':900},{'rail_id':'a','offset_um':100}]
    r['route_recovery']['recovery_poses'][0]['offset_um']=123
    assert m['route_recovery']['recovery_poses'][0]['offset_um']==900
    assert event['recovery_poses'][0]['offset_um']==900
