from test_traffic import factory,model


def cycle(service_root=False):
    m=model([
        {'id':'v1','type_id':'type','rail_id':'a','offset_um':1100},
        {'id':'v2','type_id':'type','rail_id':'a','offset_um':2100}])
    m['holding_points']=[h for h in m['holding_points'] if h['rail_id']=='a' and h['offset_um'] in (1100,2100)]
    if service_root:m['docks'].append({'id':'service-root','rail_id':'a','offset_um':1100})
    t,s,v,e=factory(m);t.manage_idle=True;s.transition_budget=20
    t.idle_wait['v1']=[{'kind':'rail','id':'a','owner':'v2'}]
    return t,s,v,e


def test_unrooted_idle_wait_cycle_settles_without_reactivating_itself():
    t,s,v,e=cycle();t.dirty();s.settle(0)
    assert not t.idle_wait and not t.active and not e


def test_service_root_keeps_clearing_chain():
    t,s,v,e=cycle(service_root=True)
    # A genuine service obstruction roots v1 -> v2 -> v1.
    t.dirty();s.settle(0)
    assert t.idle_wait=={
        'v1':[{'kind':'rail','id':'a','owner':'v2'}],
        'v2':[{'kind':'rail','id':'a','owner':'v1'}]}
    assert not t.active


def test_pending_movement_roots_transitive_idle_chain():
    t,s,v,e=cycle()
    # Three empty parked vehicles, with a real request blocked by the first.
    v['worker']={'id':'worker','type_id':'type','rail_id':'a','offset_um':500,'movement_id':None}
    t.occupancy['worker']={'a':[(400,600)]}
    t.request('worker','dock','work')
    t.dirty();s.settle(0)
    assert t.requests['work']['status']=='pending'
    assert set(t.idle_wait)=={'v1','v2'}
    assert t.idle_wait['v1']==[{'kind':'rail','id':'a','owner':'v2'}]
    assert t.idle_wait['v2']==[{'kind':'rail','id':'a','owner':'v1'}]
    t.cancel('work');s.settle(1)
    assert not t.idle_wait and not t.active



def obsolete_service_wait():
    m=model([
        {'id':'v1','type_id':'type','rail_id':'a','offset_um':5000},
        {'id':'v2','type_id':'type','rail_id':'a','offset_um':7000}])
    t,s,v,e=factory(m);t.manage_idle=True
    t.idle_wait['v1']=[{'kind':'rail','id':'a','owner':'v2'}]
    return t,s,v,e


def test_assigned_service_vehicle_does_not_root_obsolete_idle_clearing():
    t,s,v,e=obsolete_service_wait()
    v['v1']['assignment_id']='transfer:active'
    t._plan_idle()
    assert not t.active
    assert 'v1' not in t.idle_wait
    assert not e


def test_loaded_unassigned_service_vehicle_does_not_root_obsolete_idle_clearing():
    from types import SimpleNamespace
    t,s,v,e=obsolete_service_wait()
    for vid,vehicle in v.items():vehicle['carrier_slot_id']='slot:'+vid
    t.transfers=SimpleNamespace(custody=SimpleNamespace(slots={
        'slot:v1':{'carrier_id':'lot:loaded'},'slot:v2':{'carrier_id':None}}))
    t._plan_idle()
    assert not t.active
    assert 'v1' not in t.idle_wait
    assert not e


def test_assignment_release_rebuilds_genuine_service_clearing():
    t,s,v,e=obsolete_service_wait()
    v['v1']['assignment_id']='transfer:active'
    t._plan_idle()
    assert not t.active
    v['v1']['assignment_id']=None
    t._plan_idle()
    # Fresh geometry permits v1 to leave without moving its former blocker.
    assert set(t.active)=={'v1'}


def test_real_transport_blocker_survives_obsolete_idle_wait_removal():
    t,s,v,e=obsolete_service_wait()
    v['v1']['assignment_id']='transfer:active'
    t.request('v1','dock','real-work')
    t.requests['real-work']['blockers']=[{'kind':'rail','id':'a','owner':'v2'}]
    t._plan_idle()
    assert t.active['v1']=='real-work'
    assert 'v2' in t.active
    assert 'v1' not in t.idle_wait
