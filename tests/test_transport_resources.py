"""Fault-blocked handling frontiers and conservative shared competition."""
from copy import deepcopy
from fab_sim.resource_changes import ResourceChange
from fab_sim.probes import Probes,state_projection,domain_digest
from test_manufacturing_facade import place
from test_amhs_faults import admitted
from test_amhs_integration import until


def blocked_pickup():
    state=admitted();traffic,t,m,s,c,lots,v,e,did=state
    t.probes=traffic.probes=Probes()
    traffic.pause('entry','blocked',target_type='port');until(s,30000)
    assert t.demands[did]['status']=='pickup_wait'
    return state


def test_unrelated_resource_does_not_recheck_fault_blocked_handling():
    traffic,t,m,s,c,lots,v,e,did=blocked_pickup()
    def unrelated_check(*args):raise AssertionError('unrelated blocked handling was reconsidered')
    t._handling_blocked=unrelated_check
    t.resource_changed(ResourceChange(slot_ids=frozenset({'rack'})));s.settle(s.now_us)
    assert t.demands[did]['status']=='pickup_wait'


def test_vehicle_release_reconsiders_all_ranked_contenders():
    from test_stocker_flow import stocker_factory
    def run(selective):
        t,m,s,c,lots,v,e=stocker_factory();del v['v2']
        v['v1']['available']=False
        a=m.enter_visit('low','1');b=m.enter_visit('hot','1')
        place(c,'clow','entry:0');place(c,'chot','entry:1')
        first=t.request('clow',a,('port',));second=t.request('chot',b,('port',));s.settle(0)
        assert all(t.demands[did]['status']=='pending' for did in (first,second))
        v['v1']['available']=True
        if selective:t.resource_changed(ResourceChange(vehicle_ids=frozenset({'v1'})))
        else:t.dirty()
        s.settle(0)
        assert t.demands[first]['status']=='assigned' and t.demands[second]['status']=='pending'
        return e,deepcopy(c.slots)
    assert run(True)==run(False)


def test_cancelled_waiter_does_not_wake():
    traffic,t,m,s,c,lots,v,e,did=blocked_pickup();t.cancel(did);s.settle(s.now_us)
    t.resource_changed(ResourceChange(slot_ids=frozenset({'entry'})))
    s.settle(s.now_us)
    assert all(did not in ids for ids in t._handling_waiters.values())
    assert did not in t._handling_frontiers


def test_fault_repair_wakes_loaded_carrier():
    traffic,t,m,s,c,lots,v,e,did=admitted(100)
    until(s,10);assert c.locations['clow']=='vehicle:v1'
    traffic.pause('port','blocked',target_type='port');until(s,300000)
    assert t.demands[did]['status']=='drop_wait'
    traffic.recover('port','blocked',target_type='port');until(s,300010)
    assert c.locations['clow']=='port' and t.demands[did]['status']=='delivered'


def test_changed_origin_invalidates_prior_destination_wait():
    traffic,t,m,s,c,lots,v,e,did=blocked_pickup()
    t.cancel(did)
    with c.batch_notifications():
        assert c.reserve_many(['buffer'],'relocate',origins={'entry':'clow'})
        c.move('clow','entry','buffer','relocate')
    traffic.pause('buffer','new-origin',target_type='port')
    second=t.request('clow',lots['low']['current_visit_id'],('port',))
    until(s,1000000)
    assert t.demands[second]['status']=='pickup_wait'
    assert did not in t._handling_frontiers
    prior=t._handling_blocked
    def unrelated_check(*args):raise AssertionError('old origin remained in the frontier')
    t._handling_blocked=unrelated_check
    t.resource_changed(ResourceChange(slot_ids=frozenset({'entry'})))
    s.settle(s.now_us)
    t._handling_blocked=prior
    traffic.recover('buffer','new-origin',target_type='port');until(s,1000010)
    assert c.locations['clow']=='vehicle:v1'


def test_passage_release_preserves_global_hop_rank():
    from test_traffic import factory,short_links,drain
    def run(selective):
        model=short_links(True);model['docks'].append({'id':'clear','rail_id':'b','offset_um':500})
        traffic,s,v,e=factory(model)
        traffic.request('v2','dock','a-first');traffic.request('v1','dock','b-second');s.settle(0)
        assert traffic.resources=={'junction:n2':'v2'}
        if selective:traffic.resource_changed(ResourceChange(rail_ids=frozenset({'a'})))
        else:traffic.dirty()
        while traffic.requests['a-first']['status']!='complete':s.settle(s.next_time_us)
        traffic.request('v2','clear','clear-v2')
        drain(s)
        assert traffic.requests['b-second']['status']=='complete'
        return e,deepcopy(traffic.occupancy)
    assert run(True)==run(False)


def test_clearance_pool_release_reconsiders_complete_cohort():
    from test_clearance import lease_fixture
    def run(selective):
        pool,t,m,s,c,lots=lease_fixture()
        assert c.reserve('protected:1','foreign')
        assert not pool.dispatch() and not pool.records
        c.release('protected:1','foreign')
        if selective:t.resource_changed(ResourceChange(slot_ids=frozenset({'protected:1'})))
        else:t.dirty()
        t.dispatch()
        record=next(iter(pool.records.values()))
        assert len(record['members'])==2 and len(record['slots'])==2
        return pool.describe(),deepcopy(c.slots)
    assert run(True)==run(False)


def test_frontier_bookkeeping_is_derived():
    traffic,t,m,s,c,lots,v,e,did=blocked_pickup();before=domain_digest(state_projection(t))
    t._dirty_handling.mark();t._handling_frontiers.clear();t._handling_waiters.clear()
    assert domain_digest(state_projection(t))==before
    t.demands[did]['original_request_time_us']+=1
    assert domain_digest(state_projection(t))!=before
