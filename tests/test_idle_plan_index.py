from test_traffic import factory
from test_traffic import model
from fractions import Fraction


def test_scoped_rail_claim_index_matches_full_blockers_and_touching_boundaries():
    t,s,v,e=factory()
    t.occupancy['stationary']={'b':[(100,200)],'a':[(800,900)]}
    t.hops['moving']={'claims':{'a':[(300,400)]}}
    t.targets['parking']={'a':[(500,600)]}
    t.resources['junction']='junction-owner'
    claim_index=t._claim_index()
    for owner in ('v1','moving','parking'):
        for claims in ({'a':[(350,550)]},{'a':[(600,800)]},{'b':[(100,150)]}):
            assert t._blockers(owner,claims,['junction'],claim_index=claim_index)==t._blockers(owner,claims,['junction'])


def test_scoped_blocker_lookup_does_not_enumerate_unrelated_vehicle_records():
    t,s,v,e=factory();t.occupancy['blocker']={'b':[(100,200)]}
    claims=t._claim_index()
    class NoScan(dict):
        def items(self):raise AssertionError('scanned every vehicle claim')
    t.occupancy=NoScan(t.occupancy);t.hops=NoScan(t.hops);t.targets=NoScan(t.targets)
    assert t._blockers('v1',{'b':[(150,250)]},[],claim_index=claims)==[
        {'kind':'rail','id':'b','owner':'blocker'}]


def test_idle_proposal_reuses_geometry_without_caching_physical_permission():
    t,s,vehicles,e=factory();v=vehicles['v1']
    target=next(hid for hid,cost in t._idle_destinations(v) if cost>0)
    router=t._router(v['type_id']);searches=lambda:router.stats['path_searches']
    path,claims,resources=t._idle_plan(v,target)
    assert t._idle_plan(v,target)==(path,claims,resources) and searches()==1
    assert t._blockers('v1',claims,resources)==[]
    rid=next(iter(claims));t.occupancy['new-blocker']={rid:list(claims[rid])}
    assert t._idle_plan(v,target)==(path,claims,resources) and searches()==1
    assert t._blockers('v1',claims,resources)


def test_derived_idle_plans_do_not_enter_domain_projection():
    from fab_sim.probes import state_projection,domain_digest
    t,s,v,e=factory();before=domain_digest(state_projection(t))
    target=next(hid for hid,cost in t._idle_destinations(v['v1']) if cost>0)
    t._idle_plan(v['v1'],target)
    assert domain_digest(state_projection(t))==before


def test_later_idle_vehicle_sees_target_claim_created_earlier_in_same_pass(monkeypatch):
    m=model([
        {'id':'v1','type_id':'type','rail_id':'a','offset_um':5000},
        {'id':'v2','type_id':'type','rail_id':'b','offset_um':5000}])
    m['docks'].append({'id':'reverse-dock','rail_id':'b','offset_um':5000})
    t,s,v,e=factory(m);t.manage_idle=True
    targets=tuple(h['id'] for h in t.holdings.values() if h['rail_id']=='a' and h['offset_um'] in (7100,8100))
    assert len(targets)==2
    monkeypatch.setattr(t,'_idle_destinations',lambda vehicle:tuple((hid,Fraction(i+1)) for i,hid in enumerate(targets)))
    assert t._plan_idle()
    assert t.requests[t.active['v1']]['destination_pose_id']==targets[0]
    assert t.requests[t.active['v2']]['destination_pose_id']==targets[1]
    assert set(t.targets)=={'v1','v2'}
