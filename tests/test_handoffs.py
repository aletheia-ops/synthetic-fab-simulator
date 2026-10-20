from copy import deepcopy
import pytest
from test_transfers import factory,arrive
from test_manufacturing_facade import place,drain
from test_route_rules import step


def enable(t):
    from fab_sim.clearance import ClearancePool
    from fab_sim.handoffs import HandoffPlans
    return HandoffPlans(t,ClearancePool(t.manufacturing,t,[],lambda k,p:None),t.emit)


def exit_fixture():
    t,m,s,c,lots,vehicles,events=factory();m.enter_visit('low','1')
    place(c,'clow','rack');m.delivered('clow','rack');drain(s)
    plans=enable(t);did=t.request('clow',None,('exit',))
    return plans,t,m,s,c,lots,vehicles,events,did


def test_no_onward_space_retains_rack_and_creates_no_output_exchange():
    from fab_sim.campaigns import SetupCampaigns
    t,m,s,c,lots,vehicles,events=factory([step(1),step(2,setup={'id':'B','when':'need'})],rack_count=2)
    m.setups['t'].current='A';m.setups['t'].transitions=({'current':'A','new':'B','minimum_runs':2,'duration_us':1},)
    SetupCampaigns(m,t,lambda carrier,tid:1,lambda k,p:None)
    vid=m.enter_visit('low','2');place(c,'clow','rack');place(c,'chot','buffer')
    enable(t);did=t.request('clow',vid,('port',));before=deepcopy(c.slots);s.settle(0)
    assert c.locations['clow']=='rack' and c.slots==before
    assert t.demands[did]['output_exchange_id'] is None and not t.exchange.requests
    assert vehicles['v1']['assignment_id'] is None


def test_complete_plan_precedes_physical_output():
    plans,t,m,s,c,lots,vehicles,events,did=exit_fixture()
    assert plans.prepare(did,'rack')
    assert c.locations['clow']=='rack' and not t.movements
    assert all(c.slots[sid]['reservation']==did for sid in ('rack','port','exit','vehicle:v1'))
    assert vehicles['v1']['assignment_id']==did and plans.records[did]['phase']=='output_reserved'
    s.settle(s.now_us);assert plans.records[did]['phase']=='output_active'
    assert c.locations['clow']=='rack' and not t.movements
    s.settle(s.now_us+1000000)
    assert c.locations['clow']=='port' and c.slots['port']['reservation']==did
    assert plans.records[did]['phase']=='pickup_ready' and did+':pickup' in t.movements


def test_active_description_retains_committed_plan_without_copying_history():
    plans,t,m,s,c,lots,vehicles,events,did=exit_fixture()
    assert plans.prepare(did,'rack')
    plans.records['past']={**plans.records[did],'demand_id':'past','phase':'completed'}
    assert [r['demand_id'] for r in plans.describe(active_only=True)]==[did]
    assert len(plans.describe())==2


def test_live_plan_index_retires_cancelled_plan_and_keeps_evidence():
    plans,t,m,s,c,lots,vehicles,events,did=exit_fixture()
    assert plans.prepare(did,'rack')
    assert [r['demand_id'] for r in plans.current_records()]==[did]
    t.cancel(did)
    assert plans.current_records()==()
    assert plans.records[did]['phase']=='cancelled'


@pytest.mark.parametrize('fault',['source','port','unavailable'])
def test_known_source_unavailability_does_not_reserve_a_stationary_vehicle(fault):
    plans,t,m,s,c,lots,vehicles,events,did=exit_fixture()
    if fault=='source':m.causes['t'].add('maintenance')
    elif fault=='port':t.exchange.port_causes={'port':{'fault'}}
    else:m.tools['t']['available']=False
    before=deepcopy(c.slots)
    assert not plans.prepare(did,'rack')
    assert c.slots==before and vehicles['v1']['assignment_id'] is None
    assert not plans.records and not t.exchange.requests
    m.causes['t'].clear();m.tools['t']['available']=True;t.exchange.port_causes={}
    assert plans.prepare(did,'rack')


def test_rack_handoff_preserves_queue_ranking_across_source_ports():
    from test_transfers import add_idle_replica
    t,m,s,c,lots,vehicles,events=factory(rack_count=3)
    add_idle_replica(t,m,c)
    low=m.enter_visit('low','1');m.enter_visit('hot','1')
    place(c,'clow','rack');place(c,'chot','rack-extra:1')
    c.slots['out']={**c.slots['port'],'id':'out','role':'OUT'}
    t.docks['out']={**t.docks['port'],'id':'out','role':'OUT','offset_um':200}
    plans=enable(t);did=t.request('clow',low,('port','other:port'))
    assert t._choices(t.demands[did],'out')[0][2]=='other:port'
    assert plans.prepare(did,'rack')
    assert plans.records[did]['destination_id']=='other:port'


@pytest.mark.parametrize('active',[False,True])
def test_cancel_before_output_keeps_rack_and_releases_only_plan_claims(active):
    plans,t,m,s,c,lots,vehicles,events,did=exit_fixture();assert plans.prepare(did,'rack')
    if active:s.settle(s.now_us)
    t.cancel(did);s.settle(s.now_us+1000000)
    assert c.locations['clow']=='rack' and not m.claims
    assert all(c.slots[sid]['reservation'] is None for sid in ('rack','port','exit','vehicle:v1'))
    assert plans.records[did]['phase']=='cancelled' and vehicles['v1']['assignment_id'] is None


@pytest.mark.parametrize('after_pickup',[False,True])
def test_post_output_cancel_finishes_promised_safe_delivery(after_pickup):
    plans,t,m,s,c,lots,vehicles,events,did=exit_fixture();s.settle(s.now_us);s.settle(s.now_us+1000000)
    if after_pickup:arrive(t,vehicles,did,'pickup');s.settle(s.now_us+10)
    t.cancel(did)
    assert plans.records[did]['cancel_requested'] and c.slots['exit']['reservation']==did
    if not after_pickup:arrive(t,vehicles,did,'pickup');s.settle(s.now_us+10)
    arrive(t,vehicles,did,'drop');s.settle(s.now_us+10)
    assert c.locations['clow']=='exit' and not t.active
    assert plans.records[did]['phase']=='completed' and vehicles['v1']['assignment_id'] is None


def test_faulted_source_port_preserves_plan_without_premature_pickup():
    plans,t,m,s,c,lots,vehicles,events,did=exit_fixture();assert plans.prepare(did,'rack')
    t.exchange.port_causes={'port':{'fault'}}
    s.settle(s.now_us)
    assert plans.records[did]['phase']=='output_reserved' and not t.movements
    assert c.locations['clow']=='rack' and c.slots['exit']['reservation']==did
    t.exchange.port_causes={};t.dirty();s.settle(s.now_us);s.settle(s.now_us+1000000)
    assert c.locations['clow']=='port' and did+':pickup' in t.movements


def test_stale_visit_before_commit_leaves_all_claims_untouched():
    plans,t,m,s,c,lots,vehicles,events,did=exit_fixture();lots['low']['route_complete_at_us']=None
    before=deepcopy(c.slots);assert not plans.prepare(did,'rack')
    assert c.slots==before and not plans.records and not t.exchange.requests


def test_yielded_idle_vehicle_stays_exclusive_while_output_is_faulted():
    from test_amhs_integration import factory as traffic_factory
    traffic,t,m,s,c,lots,vehicles,events=traffic_factory(initial=210)
    m.enter_visit('low','1');place(c,'clow','rack');m.delivered('clow','rack');drain(s)
    plans=enable(t);did=t.request('clow',None,('exit',))
    target=next(h['id'] for h in traffic.holdings.values() if h['rail_id']=='a' and h['offset_um']==710)
    traffic.request('v1',target,'idle',reposition=True)
    assert traffic.can_yield('v1')
    before=dict(vehicles['v1']);assert plans.prepare(did,'rack')
    t.exchange.port_causes={'port':{'fault'}};s.settle(s.now_us)
    assert traffic.requests['idle']['status']=='cancelled'
    assert vehicles['v1']['assignment_id']==did and not t.movements and not traffic.hops
    assert vehicles['v1']['offset_um']==before['offset_um']


def test_cancel_at_destination_port_retains_rack_until_input_exchange_finishes():
    from test_amhs_integration import factory as traffic_factory,until
    traffic,t,m,s,c,lots,vehicles,events=traffic_factory(extra_ports=(('out',500),))
    m.enter_visit('low','1');place(c,'clow','rack');m.delivered('clow','rack');drain(s)
    vid=m.enter_visit('low','1')
    c.slots['extra']={'id':'extra','kind':'rack','equipment_id':'t','carrier_id':None,'reservation':None}
    m.tools['t']['rack_slots'].append('extra')
    plans=enable(t);did=t.request('clow',vid,('out',));assert plans.prepare(did,'rack')
    while t.demands[did]['status']!='delivered':
        s.settle(s.now_us)
        assert s.next_time_us is not None
        s.settle(s.next_time_us)
    assert c.locations['clow']=='out' and plans.records[did]['phase']!='completed'
    t.cancel(did)
    assert plans.records[did]['cancel_requested'] and c.slots['extra']['reservation'] is not None
    drain(s);assert c.locations['clow']=='extra' and plans.records[did]['phase']=='completed'


def test_membership_changed_after_reservation_cancels_before_output():
    plans,t,m,s,c,lots,vehicles,events,did=exit_fixture();assert plans.prepare(did,'rack')
    lots['low']['route_complete_at_us']=None;s.settle(s.now_us)
    assert c.locations['clow']=='rack' and plans.records[did]['phase']=='cancelled'
    assert all(c.slots[sid]['reservation'] is None for sid in ('rack','port','exit','vehicle:v1'))


def test_failed_handoff_rechecks_buffer_and_destination_availability():
    from fab_sim.resource_index import ResourceIndex
    plans,t,m,s,c,lots,vehicles,events,did=exit_fixture()
    t.resource_index=ResourceIndex(c.slots,m.tools,{'docks':list(t.docks.values())})
    c.reserve('exit','blocked-exit');c.reserve('buffer','blocked-buffer')
    assert not plans.prepare(did,'rack')
    assert plans.waits[did]['reason']=='ordinary_overflow_exhausted'
    c.release('buffer','blocked-buffer')
    assert not plans.prepare(did,'rack')
    assert plans.waits[did]['reason']=='destination_admission_unavailable'
    c.release('exit','blocked-exit')
    assert plans.prepare(did,'rack')
    assert plans.records[did]['destination_id']=='exit'
