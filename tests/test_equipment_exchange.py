from copy import deepcopy
import pytest
from test_manufacturing_facade import factory as manufacturing_factory,place,drain


def factory(exchange_us=1000000):
    from fab_sim.transfers import EquipmentExchange
    m,s,c,lots,events=manufacturing_factory()
    for sid,role in [('in','IN'),('out','OUT'),('both','BOTH')]:
        c.slots[sid]={'id':sid,'kind':'port','equipment_id':'t','role':role,'carrier_id':None,'reservation':None}
    exchange=EquipmentExchange(m,lambda k,p:events.append((k,deepcopy(p))),exchange_time_us={'t':exchange_us})
    s.register_pass('transfer',exchange.dispatch)
    return exchange,m,s,c,lots,events


def test_positive_default_exchange_holds_port_and_rack_until_atomic_arrival():
    exchange,m,s,c,lots,events=factory();v=m.enter_visit('low','1');place(c,'clow','in')
    exchange.request('input','clow','in','rack');s.settle(0)
    assert c.locations['clow']=='in' and c.slots['rack']['reservation']
    assert c.slots['in']['reservation'] and not m.executions
    s.settle(999999);assert c.locations['clow']=='in'
    s.settle(1000000);assert c.locations['clow']=='rack' and m.executions
    assert m.rules.visits[v]['processing_started_at_us'] is None
    s.settle(1000002);assert m.rules.visits[v]['processing_started_at_us']==1000002


def test_exchange_is_distinct_from_manufacturing_service_and_uses_shared_carrier_claims():
    exchange,m,s,c,lots,events=factory(5);m.enter_visit('low','1');place(c,'clow','rack');m.delivered('clow','rack')
    exchange.request('output','clow','rack','out');s.settle(0)
    assert exchange.handlers['t'].active is None and c.locations['clow']=='rack'
    s.settle(2);s.settle(12);assert exchange.handlers['t'].active is None
    s.settle(15);assert exchange.handlers['t'].active['id']=='output'
    s.settle(20);assert c.locations['clow']=='out' and lots['low']['route_complete_at_us']==15
    assert c.slots['rack']['carrier_id'] is None and not m.claims


def test_input_and_output_roles_reject_wrong_direction_without_claims():
    exchange,m,s,c,lots,events=factory(5);m.enter_visit('low','1');place(c,'clow','out')
    with pytest.raises(ValueError,match='role'):exchange.request('bad','clow','out','rack')
    assert not exchange.requests and c.slots['out']['reservation'] is None
    with pytest.raises(ValueError,match='role'):exchange.request('bad2','clow','rack','in')


def test_owned_inbound_rack_handoff_is_atomic_and_rejected_foreign_owner_does_not_claim_origin():
    exchange,m,s,c,lots,events=factory(5);m.enter_visit('low','1');place(c,'clow','in');c.reserve('rack','transfer')
    snapshots=[];c.on_change=lambda:snapshots.append((c.slots['in']['reservation'],c.slots['rack']['reservation'],exchange.requests['input']['status']))
    exchange.request('input','clow','in','rack',reservation_owner='transfer');s.settle(0)
    owner=exchange.requests['input']['owner_id'];assert snapshots==[(owner,owner,'active')]
    s.settle(5);assert c.locations['clow']=='rack'


def test_blocked_requests_keep_age_and_do_not_partially_reserve_origin():
    exchange,m,s,c,lots,events=factory(5);m.enter_visit('low','1');place(c,'clow','in');c.reserve('rack','foreign')
    exchange.request('input','clow','in','rack');s.settle(0)
    assert c.slots['in']['reservation'] is None and exchange.requests['input']['status']=='pending'
    s.settle(7);c.release('rack','foreign');s.settle(7)
    assert exchange.requests['input']['queued_at_us']==0 and exchange.requests['input']['started_at_us']==7


def test_occupied_inbound_port_cannot_be_taken_for_output():
    exchange,m,s,c,lots,events=factory(5);m.enter_visit('low','1');place(c,'clow','rack');m.delivered('clow','rack');drain(s)
    place(c,'chot','both');exchange.request('output','clow','rack','both');s.settle(s.now_us)
    assert exchange.requests['output']['status']=='pending' and c.locations['clow']=='rack'
    assert c.slots['rack']['reservation'] is None


def test_native_fault_pauses_exchange_remainder_and_retains_both_custody_claims():
    from fab_sim.maintenance import Maintenance
    from test_maintenance import fault,run_until
    exchange,m,s,c,lots,events=factory(10)
    maintenance=Maintenance(m,lambda k,p:None,exchange_handlers=exchange.handlers)
    source={'model':{'tool_groups':[{'id':'A','area':'area'}],'maintenance':[],'breakdowns':[fault(first=3,repair=5)]}}
    maintenance.initialize({'model':{'equipment':[{'id':'t','kind':'ordinary','tool_group_id':'A'}]}},source)
    m.enter_visit('low','1');place(c,'clow','in');exchange.request('input','clow','in','rack')
    run_until(s,3);owner=exchange.requests['input']['owner_id']
    assert c.locations['clow']=='in' and c.slots['rack']['reservation']==owner
    run_until(s,14);assert c.locations['clow']=='in'
    run_until(s,15);assert c.locations['clow']=='rack' and exchange.requests['input']['completed_at_us']==15


@pytest.mark.parametrize('value',[0,-1,True,1.5])
def test_exchange_duration_must_be_positive_integer(value):
    with pytest.raises(ValueError,match='duration'):factory(value)


def test_five_member_batch_exchanges_through_four_ports_then_clears_on_actual_departure():
    from fab_sim.transfers import EquipmentExchange
    from test_batch_service import factory as batch_factory
    from test_batch import move
    m,s,c,lots,events=batch_factory()
    for i in range(4):c.slots[f'port:{i}'].update(equipment_id='tool-a',role='BOTH')
    exchange=EquipmentExchange(m,lambda k,p:None,exchange_time_us={'tool-a':5})
    s.register_pass('transfer',exchange.dispatch);s.settle(0)
    a=next(iter(m.batches.assemblies.values()))
    for i,member in enumerate(a['members']):
        port=f'port:{i%4}';move(c,member['carrier_id'],port,a['id'])
        exchange.request(f'input:{i}',member['carrier_id'],port,member['rack_slot_id'],reservation_owner=a['id'])
        s.settle(s.now_us);s.settle(s.now_us+5)
        assert member['arrived']
        if i<4:assert not m.executions
    assert all(m.rules.visits[x['visit_id']]['processing_started_at_us'] is None for x in a['members'])
    drain(s);assert a['status']=='finished' and s.now_us==40
    for i,member in enumerate(a['members']):
        exchange.request(f'output:{i}',member['carrier_id'],member['rack_slot_id'],'port:0')
        s.settle(s.now_us);s.settle(s.now_us+5)
        assert member['departed']
        if i<4:assert a['status']=='finished'
        move(c,member['carrier_id'],f'entry:{i}','downstream')
    assert a['status']=='cleared' and m.tools['tool-a']['assembly_id'] is None


def test_reserved_empty_both_port_reconsiders_after_release_without_polling():
    exchange,m,s,c,lots,events=factory(5)
    m.enter_visit('low','1');place(c,'clow','rack');m.delivered('clow','rack');drain(s)
    c.reserve('both','incoming');exchange.request('output','clow','rack','both');s.settle(s.now_us)
    assert exchange.requests['output']['status']=='pending' and c.slots['rack']['reservation'] is None
    c.release('both','incoming');s.settle(s.now_us)
    assert exchange.requests['output']['status']=='active'


def test_lost_exchange_custody_claim_rejects_completion_before_moving_carrier():
    exchange,m,s,c,lots,events=factory(5);m.enter_visit('low','1');place(c,'clow','in')
    exchange.request('input','clow','in','rack');s.settle(0)
    c.slots['rack']['reservation']='foreign'
    with pytest.raises(RuntimeError,match='ownership'):s.settle(5)
    assert c.locations['clow']=='in' and c.slots['rack']['carrier_id'] is None
    assert not m.executions


def test_duplicate_pending_carrier_exchange_is_rejected():
    exchange,m,s,c,lots,events=factory(5);m.enter_visit('low','1');place(c,'clow','in')
    exchange.request('input','clow','in','rack')
    with pytest.raises(ValueError,match='duplicate'):exchange.request('again','clow','in','rack')
    assert list(exchange.requests)==['input']


def test_cancel_active_input_exchange_restores_admission_owner_and_origin_custody():
    exchange,m,s,c,lots,events=factory(5);m.enter_visit('low','1');place(c,'clow','in');c.reserve('rack','admission')
    exchange.request('input','clow','in','rack',reservation_owner='admission');s.settle(0)
    exchange.cancel('input');s.settle(5)
    assert c.locations['clow']=='in' and c.slots['in']['reservation'] is None
    assert c.slots['rack']['reservation']=='admission' and not m.claims
    assert exchange.requests['input']['status']=='cancelled' and not m.executions


def test_cancel_pending_exchange_preserves_foreign_claims():
    exchange,m,s,c,lots,events=factory(5);m.enter_visit('low','1');place(c,'clow','in');c.reserve('rack','foreign')
    exchange.request('input','clow','in','rack');s.settle(0);exchange.cancel('input')
    assert c.slots['rack']['reservation']=='foreign' and c.slots['in']['reservation'] is None
    assert c.locations['clow']=='in'


def test_cancel_exchange_preflights_all_claims_before_releasing_any():
    exchange,m,s,c,lots,events=factory(5);m.enter_visit('low','1');place(c,'clow','in')
    exchange.request('input','clow','in','rack');s.settle(0);owner=exchange.requests['input']['owner_id']
    c.slots['rack']['reservation']='foreign'
    with pytest.raises(RuntimeError,match='ownership'):exchange.cancel('input')
    assert c.slots['in']['reservation']==owner and m.claims['clow']==owner
    assert exchange.requests['input']['status']=='active'


@pytest.mark.parametrize('cancel',[False,True])
def test_outbound_exchange_restores_committed_port_owner(cancel):
    exchange,m,s,c,lots,events=factory(5);m.enter_visit('low','1')
    place(c,'clow','rack');m.delivered('clow','rack');drain(s)
    assert c.reserve_many(['out'],'handoff',origins={'rack':'clow'})
    exchange.request('output','clow','rack','out',origin_owner='handoff',
                     reservation_owner='handoff',return_owner='handoff')
    s.settle(s.now_us);owner=exchange.requests['output']['owner_id']
    assert c.slots['rack']['reservation']==c.slots['out']['reservation']==owner
    if cancel:
        exchange.cancel('output');s.settle(s.now_us+5)
        assert c.locations['clow']=='rack'
        assert c.slots['rack']['reservation']==c.slots['out']['reservation']=='handoff'
    else:
        s.settle(s.now_us+5)
        assert c.locations['clow']=='out' and c.slots['out']['reservation']=='handoff'
        assert c.slots['rack']['reservation'] is None
    assert not m.claims
