from copy import deepcopy
from fractions import Fraction
from test_batch import factory as assembly_factory, move
from fab_sim.manufacturing import Manufacturing


def factory(n=5):
    old,rules,c,tools,s,streams,lots,events=assembly_factory(n=n)
    for route in rules.routes.values():
        for step in route['steps']:step['processing_unit']='batch'
    source={'model':{'routes':list(rules.routes.values()),'setup_transitions':[],
                    'tool_groups':[{'id':'A','initial_setup':None,'loading_us':2,'unloading_us':3}]}}
    # Use a fresh shared scheduler; no duplicate RouteRules event owner.
    from fab_sim.scheduler import Scheduler
    s=Scheduler()
    for lot in lots.values():lot.pop('current_visit_id',None);lot['route_history']=[]
    m=Manufacturing(source,s,streams,lots,c,tools,lambda k,p:events.append((k,deepcopy(p))),
                    delivery_cost=lambda carrier,tool:Fraction(1))
    for lid in lots:m.enter_visit(lid,'1')
    return m,s,c,lots,events


def deliver(m,c,a,member,index):
    move(c,member['carrier_id'],f'port:{index%4}',a['id'])
    move(c,member['carrier_id'],member['rack_slot_id'],a['id'],True)
    m.delivered(member['carrier_id'],member['rack_slot_id'])


def drain(s):
    s.settle(s.now_us)
    while s.next_time_us is not None:s.settle(s.next_time_us)


def test_batch_shared_phases_wait_for_all_five_members_using_four_ports():
    m,s,c,lots,events=factory(); s.settle(0)
    a=next(iter(m.batches.assemblies.values()));assert len(a['members'])==5
    for i,member in enumerate(a['members'][:-1]):deliver(m,c,a,member,i);s.settle(0)
    assert not m.executions and a['status']=='assembling'
    deliver(m,c,a,a['members'][-1],4);s.settle(0)
    assert a['status']=='assembling'
    s.settle(2); assert a['status']=='processing'
    assert all(m.rules.visits[x['visit_id']]['processing_started_at_us']==2 for x in a['members'])
    s.settle(12); assert a['status']=='processed'
    assert m.setups['tool-a'].completed_lots==5 and m.tools['tool-a']['processed_wafers']==125
    s.settle(15);assert a['status']=='finished'
    assert all(lot['route_complete_at_us']==15 for lot in lots.values())
    assert len([x for x in events if x[0]=='service_started' and x[1]['kind']=='load'])==1
    assert len([x for x in events if x[0]=='service_started' and x[1]['kind']=='unload'])==1
    assert all(c.locations[x['carrier_id']]==x['rack_slot_id'] for x in a['members'])


def test_undersized_tail_creates_no_assembly_execution_or_processing_draw():
    m,s,c,lots,events=factory(4);states={lid:m.streams.get('processing',lid).getstate() for lid in lots}
    s.settle(0);assert not m.batches.assemblies and not m.executions
    assert states=={lid:m.streams.get('processing',lid).getstate() for lid in lots}
    assert all(c.slots[x]['reservation'] is None for x in c.slots)


def test_committed_batch_drains_with_pending_pm_after_incoming_members_arrive():
    m,s,c,lots,events=factory();s.settle(0);a=next(iter(m.batches.assemblies.values()))
    m.tools['tool-a']['pm_pending']=True
    for i,member in enumerate(a['members']):deliver(m,c,a,member,i)
    drain(s);assert a['status']=='finished'


def test_batch_fault_between_load_completion_and_start_retains_all_commitments():
    m,s,c,lots,events=factory();s.settle(0);a=next(iter(m.batches.assemblies.values()))
    for i,member in enumerate(a['members']):deliver(m,c,a,member,i)
    s.register('fault',lambda p:m.pause('tool-a','fault'));s.schedule(2,2,'fault',{})
    s.settle(0);s.settle(2);assert a['status']=='assembling'
    assert all(m.rules.visits[x['visit_id']]['status']=='pending' for x in a['members'])
    s.settle(10);m.resume('tool-a','fault');drain(s)
    assert a['processing_started_at_us']==10 and a['unloaded_at_us']==23


def test_batch_cannot_exchange_members_before_unload_then_requires_all_departures():
    m,s,c,lots,events=factory();s.settle(0);a=next(iter(m.batches.assemblies.values()))
    for i,member in enumerate(a['members']):deliver(m,c,a,member,i)
    s.settle(0)
    assert not c.reserve_many([],'other',origins={a['members'][0]['rack_slot_id']:a['members'][0]['carrier_id']})
    drain(s)
    for i,member in enumerate(a['members']):
        move(c,member['carrier_id'],f'entry:{i}','depart')
        m.departed(member['carrier_id'])
        if i<4:assert m.tools['tool-a']['assembly_id']==a['id']
    assert a['status']=='cleared' and m.tools['tool-a']['assembly_id'] is None


def test_paused_tool_does_not_commit_new_batch_and_recovery_reconsiders_it():
    m,s,c,lots,events=factory();m.pause('tool-a','fault');s.settle(0)
    assert not m.batches.assemblies
    m.resume('tool-a','fault');s.settle(0);assert len(m.batches.assemblies)==1


def test_batch_setup_and_processing_draw_once_from_smallest_logical_lot_stream():
    from fab_sim.rng import Streams,sample_seconds
    from fab_sim.timebase import to_ticks
    m,s,c,lots,events=factory();step=m.rules.routes['r']['steps'][0]
    distribution={'distribution':'uniform','mean_seconds':'0.000010','offset_seconds':'0.000001'}
    step['setup']={'id':'B','when':'need','fallback':distribution};step['processing']=distribution
    reference=Streams(7);owner=min(lots)
    setup_time=to_ticks(sample_seconds(distribution,reference.get('processing',owner)))
    processing_time=to_ticks(sample_seconds(distribution,reference.get('processing',owner)))
    untouched={lid:m.streams.get('processing',lid).getstate() for lid in lots if lid!=owner}
    s.settle(0);a=next(iter(m.batches.assemblies.values()))
    for i,member in enumerate(a['members']):deliver(m,c,a,member,i)
    drain(s)
    assert a['processing_started_at_us']==setup_time+2
    assert a['processing_completed_at_us']==setup_time+2+processing_time
    assert m.streams.get('processing',owner).getstate()==reference.get('processing',owner).getstate()
    assert untouched=={lid:m.streams.get('processing',lid).getstate() for lid in untouched}


def test_batch_start_requires_every_owned_rack_before_binding_any_member():
    import pytest
    m,s,c,lots,events=factory();s.settle(0);a=next(iter(m.batches.assemblies.values()))
    for i,member in enumerate(a['members']):deliver(m,c,a,member,i)
    s.settle(0);member=a['members'][-1];c.release(member['rack_slot_id'],a['id'])
    with pytest.raises(RuntimeError,match='ownership'):s.settle(2)
    assert all(m.rules.visits[x['visit_id']]['status']=='pending' for x in a['members'])


def test_batch_delivery_requires_precommitted_assembly_membership():
    import pytest
    m,s,c,lots,events=factory(4);s.settle(0)
    carrier=lots['lot:0']['carrier_id'];move(c,carrier,'tool-a:rack:0','uncommitted')
    with pytest.raises(ValueError,match='assembly'):m.delivered(carrier,'tool-a:rack:0')
    assert not m.executions and not m.batches.assemblies


def test_shared_completion_preflights_every_visit_owner_before_awarding_any_credit():
    import pytest
    m,s,c,lots,events=factory();s.settle(0);a=next(iter(m.batches.assemblies.values()))
    for i,member in enumerate(a['members']):deliver(m,c,a,member,i)
    s.settle(0);s.settle(2)
    m.rules.visits[a['members'][-1]['visit_id']]['execution_id']='foreign'
    with pytest.raises(RuntimeError,match='ownership'):s.settle(12)
    assert m.setups['tool-a'].completed_lots==0 and m.tools['tool-a']['processed_wafers']==0
    assert all(m.rules.visits[x['visit_id']]['status']=='processing' for x in a['members'])


def test_shared_unload_preflights_every_visit_owner_before_advancing_any_route():
    import pytest
    m,s,c,lots,events=factory();s.settle(0);a=next(iter(m.batches.assemblies.values()))
    for i,member in enumerate(a['members']):deliver(m,c,a,member,i)
    s.settle(0);s.settle(2);s.settle(12)
    m.rules.visits[a['members'][-1]['visit_id']]['execution_id']='foreign'
    with pytest.raises(RuntimeError,match='ownership'):s.settle(15)
    assert all(m.rules.visits[x['visit_id']]['status']=='processed' for x in a['members'])
    assert all(c.slots[x['rack_slot_id']]['reservation']==a['id'] for x in a['members'])


def test_actual_dataset1_first_batch_uses_preserved_source_parameters():
    import os
    import pytest
    from pathlib import Path
    from fab_sim.sources.smt import prepare_manufacturing
    from fab_sim.scheduler import Scheduler
    from fab_sim.rng import Streams,sample_seconds
    from fab_sim.timebase import to_ticks
    from fab_sim.custody import Custody
    path=os.environ.get('FAB_HVLM_WORKBOOK')
    if not path:pytest.skip('set FAB_HVLM_WORKBOOK for actual batch service')
    source=prepare_manufacturing(Path(path));before=deepcopy(source)
    route=next(r for r in source['model']['routes'] if r['product_id']=='Product_3')
    step=route['steps'][0];assert step['processing_unit']=='batch'
    group=next(g for g in source['model']['tool_groups'] if g['id']==step['tool_group_id'])
    lots={f'L{i}':{'route_id':route['id'],'product_id':route['product_id'],'priority':10,
                  'wafer_count':25,'carrier_id':f'C{i}','admitted_at_us':0} for i in range(5)}
    tools={'real':{'id':'real','tool_group_id':group['id'],'rack_slots':[f'rack{i}' for i in range(6)]}}
    c=Custody([{'id':f'entry{i}','kind':'entry'} for i in range(5)]+[
              {'id':f'rack{i}','kind':'rack','equipment_id':'real'} for i in range(6)])
    s=Scheduler();streams=Streams(7)
    m=Manufacturing(source,s,streams,lots,c,tools,lambda k,p:None,delivery_cost=lambda carrier,tid:Fraction(1))
    for i,lid in enumerate(lots):
        c.reserve(f'entry{i}','admit');c.place(lots[lid]['carrier_id'],f'entry{i}','admit')
        m.enter_visit(lid,step['source_step_id'])
    s.settle(0);a=next(iter(m.batches.assemblies.values()))
    for member in a['members']:
        move(c,member['carrier_id'],member['rack_slot_id'],a['id'],True)
        m.delivered(member['carrier_id'],member['rack_slot_id'])
    s.settle(0)
    while a['status']!='finished':s.settle(s.next_time_us)
    duration=to_ticks(sample_seconds(step['processing'],Streams(7).get('processing',min(lots))))
    assert a['processing_started_at_us']==group['loading_us']
    assert a['processing_completed_at_us']==group['loading_us']+duration
    assert a['unloaded_at_us']==group['loading_us']+duration+group['unloading_us']
    assert source==before and m.tools['real']['processed_wafers']==125


def test_rejected_delivery_leaves_visit_readiness_and_commitment_unchanged():
    import pytest
    m,s,c,lots,events=factory();s.settle(0);a=next(iter(m.batches.assemblies.values()))
    member=a['members'][0];before=deepcopy(m.rules.visits[member['visit_id']])
    move(c,member['carrier_id'],'tool-a:rack:5',a['id'])
    with pytest.raises(ValueError):m.delivered(member['carrier_id'],'tool-a:rack:5')
    assert m.rules.visits[member['visit_id']]==before and not member['arrived']
