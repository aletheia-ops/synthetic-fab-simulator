"""Finite assembly membership; physical moves are explicit custody transitions."""
from copy import deepcopy
from fractions import Fraction
import pytest
from test_route_rules import controller,step


def factory(n=5,minimum=125,maximum=150,capacity=6,tools=('tool-a',)):
    from fab_sim.custody import Custody
    from fab_sim.manufacturing import BatchAssembly,SetupState
    rules,s,streams,lots,events=controller([step(1,batch_min=minimum,batch_max=maximum)])
    lots.clear()
    slots=[{'id':f'entry:{i}','kind':'entry'} for i in range(n)]
    slots += [{'id':f'{tool}:rack:{i}','kind':'rack','equipment_id':tool} for tool in tools for i in range(capacity)]
    slots += [{'id':f'port:{i}','kind':'port'} for i in range(4)]
    c=Custody(slots)
    for i in range(n):
        lid=f'lot:{i}'
        lots[lid]={'route_id':'r','product_id':'p','wafer_count':25,'priority':10,
                   'carrier_id':lid+':carrier','admitted_at_us':0}
        c.reserve(f'entry:{i}','admit');c.place(lid+':carrier',f'entry:{i}','admit')
        visit=rules.enter_visit(lid,'1');rules.visits[visit]['queue_ready_at_us']=i
    equipment={tool:{'id':tool,'tool_group_id':'A','rack_slots':[f'{tool}:rack:{i}' for i in range(capacity)],
                     'available':True,'active_execution_id':None} for tool in tools}
    setup={tool:SetupState(None,[]) for tool in tools}
    batch=BatchAssembly(rules,c,equipment,setup,lambda carrier,tool:Fraction(1),
                        lambda kind,payload:events.append((kind,deepcopy(payload))))
    return batch,rules,c,equipment,s,streams,lots,events


def move(c,carrier,destination,owner,destination_reserved=False):
    origin=c.locations[carrier]
    if destination_reserved:
        assert c.slots[destination]['reservation']==owner
        assert c.reserve_many([],owner,origins={origin:carrier})
    else:
        assert c.reserve_many([destination],owner,origins={origin:carrier})
    c.move(carrier,origin,destination,owner)


def deliver(batch,c,assembly,member,index):
    carrier=member['carrier_id'];owner=assembly['id']
    move(c,carrier,f'port:{index%4}',owner)
    # delivery alone at an external port is insufficient to start processing.
    assert not batch.ready(assembly['id'])
    move(c,carrier,member['rack_slot_id'],owner,True)
    batch.arrived(carrier,member['rack_slot_id'])


def test_five_lot_assembly_uses_four_ports_and_waits_for_every_rack_arrival():
    b,r,c,tools,s,streams,lots,events=factory()
    aid=b.commit(); a=b.assemblies[aid]
    assert len(a['members'])==5 and not b.ready(aid)
    assert all(c.slots[m['rack_slot_id']]['reservation']==aid for m in a['members'])
    assert not any(c.slots[f'port:{i}']['reservation'] for i in range(4))
    for i,m in enumerate(a['members']):deliver(b,c,a,m,i)
    assert b.ready(aid)
    assert all(r.visits[m['visit_id']]['processing_started_at_us'] is None for m in a['members'])
    b.started(aid)
    assert all(r.visits[m['visit_id']]['processing_started_at_us']==0 for m in a['members'])


def test_largest_ready_prefix_and_partial_valid_batch_do_not_wait_for_maximum():
    b,r,c,tools,s,streams,lots,events=factory(n=8)
    aid=b.commit(); a=b.assemblies[aid]
    assert [m['lot_id'] for m in a['members']]==[f'lot:{i}' for i in range(6)]
    assert sum(v.get('assembly_id') is None for v in r.visits.values())==2
    b2,*_=factory(n=5)
    assert len(b2.assemblies[b2.commit()]['members'])==5


def test_known_finite_population_preserves_a_feasible_batch_remainder():
    b,*_=factory(n=6)
    # Five already consumed from a thirty-LOT cohort: 25 remain. Taking six
    # leaves 19, which cannot be partitioned into batches of five or six.
    b.exact_population=lambda lot,step:25
    aid=b.commit()
    assert len(b.assemblies[aid]['members'])==5


def test_feasible_remainder_can_require_waiting_for_one_more_arrival():
    b,*_=factory(n=5)
    b.exact_population=lambda lot,step:6
    assert b.commit() is None
    assert not b.assemblies


def test_remainder_guard_does_not_wait_for_a_batch_larger_than_the_tool():
    b,*_=factory(n=5,capacity=5)
    b.exact_population=lambda lot,step:6
    assert len(b.assemblies[b.commit()]['members'])==5


def test_batch_tail_and_nonphysical_or_committed_candidates_leave_no_claims():
    for n in (0,4):
        b,r,c,tools,s,streams,lots,events=factory(n=n)
        before=deepcopy(c.slots)
        assert b.commit() is None and c.slots==before
    b,r,c,tools,s,streams,lots,events=factory(n=6)
    r.visits[lots['lot:0']['current_visit_id']]['queue_ready_at_us']=None
    lots['lot:1']['transfer_id']='active'
    assert b.commit() is None
    assert not b.assemblies and all(v.get('assembly_id') is None for v in r.visits.values())


def test_unrelated_rack_occupants_and_claims_block_atomically():
    b,r,c,tools,s,streams,lots,events=factory()
    c.reserve('tool-a:rack:0','foreign');c.place('foreign-carrier','tool-a:rack:0','foreign')
    before=deepcopy(c.slots)
    assert b.commit() is None and c.slots==before
    b,r,c,tools,s,streams,lots,events=factory()
    c.reserve('tool-a:rack:0','other')
    # Five free racks are sufficient; unrelated reserved slot is not consumed.
    aid=b.commit(); assert c.slots['tool-a:rack:0']['reservation']=='other'
    assert len(b.assemblies[aid]['members'])==5


def test_included_reusable_rack_is_claimed_once_and_counts_as_arrived():
    b,r,c,tools,s,streams,lots,events=factory()
    move(c,'lot:0:carrier','tool-a:rack:0','reuse')
    aid=b.commit(); a=b.assemblies[aid]
    member=next(m for m in a['members'] if m['lot_id']=='lot:0')
    assert member['rack_slot_id']=='tool-a:rack:0' and member['arrived']
    assert sum(m['carrier_id']=='lot:0:carrier' for m in a['members'])==1
    for i,m in enumerate(m for m in a['members'] if not m['arrived']):deliver(b,c,a,m,i)
    assert b.ready(aid)


def test_membership_fixed_and_finished_carriers_block_until_actual_departure():
    b,r,c,tools,s,streams,lots,events=factory(n=10)
    aid=b.commit();a=b.assemblies[aid]; original=deepcopy(a['members'])
    for i,m in enumerate(a['members']):deliver(b,c,a,m,i)
    assert b.commit() is None
    b.started(aid);b.finished(aid);b.unloaded(aid)
    assert b.commit() is None
    for i,m in enumerate(a['members']):
        move(c,m['carrier_id'],f'entry:{i}',aid)
        b.departed(m['carrier_id'])
    assert [m['lot_id'] for m in a['members']]==[m['lot_id'] for m in original]
    assert tools['tool-a']['assembly_id'] is None
    # Remaining four-LOT tail is preserved, not relaxed to a batch.
    assert b.commit() is None


def test_ranking_costs_tool_ids_and_dedication_filter_do_not_draw_rng():
    b,r,c,tools,s,streams,lots,events=factory(tools=('tool-b','tool-a'))
    for lot in lots.values():lot['dedication']['same']='tool-b'
    r.routes['r']['dedication_components']=[{'id':'same','source_step_ids':['1']}]
    states={lid:streams.get('processing',lid).getstate() for lid in lots}
    aid=b.commit();assert b.assemblies[aid]['tool_id']=='tool-b'
    assert {lid:streams.get('processing',lid).getstate() for lid in lots}==states
    b,r,c,tools,s,streams,lots,events=factory(tools=('tool-b','tool-a'))
    b.delivery_cost=lambda carrier,tool:Fraction(1 if tool=='tool-b' else 2)
    assert b.assemblies[b.commit()]['tool_id']=='tool-b'


def test_notification_observes_whole_commit_and_wrong_arrival_cannot_mutate():
    b,r,c,tools,s,streams,lots,events=factory()
    seen=[]
    c.on_change=lambda:seen.append((len(b.assemblies),sum(v.get('assembly_id') is not None for v in r.visits.values())))
    aid=b.commit();assert seen==[(1,5)]
    a=b.assemblies[aid];before=deepcopy(a)
    with pytest.raises(ValueError,match='custody'):b.arrived('lot:0:carrier',a['members'][0]['rack_slot_id'])
    assert a==before
    with pytest.raises(RuntimeError,match='ready'):b.started(aid)
    with pytest.raises(RuntimeError,match='processing'):b.finished(aid)


def test_processing_credits_only_after_complete_and_duplicate_finish_rejects():
    b,r,c,tools,s,streams,lots,events=factory()
    aid=b.commit();a=b.assemblies[aid]
    setup=b.setup_states['tool-a'];assert setup.completed_lots==0
    for i,m in enumerate(a['members']):deliver(b,c,a,m,i)
    b.started(aid);assert setup.completed_lots==0
    b.finished(aid);assert setup.completed_lots==5
    with pytest.raises(RuntimeError):b.finished(aid)
    assert setup.completed_lots==5
    with pytest.raises(ValueError):b.departed(a['members'][0]['carrier_id'])


def test_no_second_assembly_after_moving_to_another_rack_on_same_tool():
    b,r,c,tools,s,streams,lots,events=factory()
    aid=b.commit();a=b.assemblies[aid]
    for i,m in enumerate(a['members']):deliver(b,c,a,m,i)
    b.started(aid);b.finished(aid);b.unloaded(aid)
    move(c,a['members'][0]['carrier_id'],'tool-a:rack:5',aid)
    with pytest.raises(ValueError,match='clearance'):b.departed(a['members'][0]['carrier_id'])
    assert not a['members'][0]['departed']


def test_incompatible_products_step_ids_and_unreachable_candidates_do_not_mix():
    b,r,c,tools,s,streams,lots,events=factory(n=8)
    for i in range(4):lots[f'lot:{i}']['product_id']='other'
    assert b.commit() is None
    b,r,c,tools,s,streams,lots,events=factory(n=6)
    b.delivery_cost=lambda carrier,tool: None if carrier in ('lot:0:carrier','lot:1:carrier') else Fraction(1)
    assert b.commit() is None


def test_unavailable_pending_pm_and_execution_block_new_commitments():
    for changes in ({'available':False},{'pm_pending':True},{'active_execution_id':'busy'}):
        b,r,c,tools,s,streams,lots,events=factory()
        tools['tool-a'].update(changes);before=deepcopy(c.slots)
        assert b.commit() is None and c.slots==before


def test_higher_priority_waits_for_no_upstream_candidate_and_rank_precedes_cost():
    b,r,c,tools,s,streams,lots,events=factory(n=7)
    hot=r.visits[lots['lot:6']['current_visit_id']];hot['priority']=30
    upstream=r.visits[lots['lot:5']['current_visit_id']];upstream.update(priority=30,queue_ready_at_us=None)
    aid=b.commit();members=b.assemblies[aid]['members']
    assert members[0]['lot_id']=='lot:6' and len(members)==6
    assert 'lot:5' not in [m['lot_id'] for m in members]


def test_actual_start_dedication_conflict_is_preflighted_for_all_members():
    b,r,c,tools,s,streams,lots,events=factory()
    r.routes['r']['dedication_components']=[{'id':'same','source_step_ids':['1']}]
    aid=b.commit();a=b.assemblies[aid]
    assert all(not lot['dedication'] for lot in lots.values())
    for i,m in enumerate(a['members']):deliver(b,c,a,m,i)
    lots['lot:4']['dedication']['same']='other-tool'
    with pytest.raises(RuntimeError,match='dedication'):b.started(aid)
    assert a['status']=='assembling' and all(v['status']=='pending' for v in r.visits.values())
    assert all(not lots[f'lot:{i}']['dedication'] for i in range(4))


def test_minimum_run_lock_filters_candidate_without_any_speculative_setup_draw():
    b,r,c,tools,s,streams,lots,events=factory()
    definition=r.steps['r']['1']
    definition['setup']={'id':'B','when':'need','fallback':None}
    from fab_sim.manufacturing import SetupState
    setup=SetupState('A',[{'current':'A','new':'B','duration_us':10,'minimum_runs':7}])
    setup.minimum_runs=7;b.setup_states['tool-a']=setup
    assert b.commit() is None
    setup.credit(7);aid=b.commit();assert aid is not None
    for i,m in enumerate(b.assemblies[aid]['members']):deliver(b,c,b.assemblies[aid],m,i)
    with pytest.raises(RuntimeError,match='setup'):b.started(aid)
    setup.apply(setup.decision(definition));b.started(aid)
    assert setup.completed_lots==0


def test_finished_origin_batch_can_feed_a_reserved_downstream_batch():
    b,r,c,tools,s,streams,lots,events=factory(tools=('tool-a','tool-b'))
    tools['tool-b']['tool_group_id']='B'
    following=step(2,tool_group_id='B',batch_min=125,batch_max=150)
    r.routes['r']['steps'].append(following);r.steps['r']['2']=following
    first=b.commit();origin=b.assemblies[first]
    for i,m in enumerate(origin['members']):deliver(b,c,origin,m,i)
    b.started(first);b.finished(first);b.unloaded(first)
    for lot in lots.values():r.visits[lot['current_visit_id']]['queue_ready_at_us']=0
    second=b.commit();target=b.assemblies[second]
    assert target['tool_id']=='tool-b' and tools['tool-a']['assembly_id']==first
    for i,m in enumerate(target['members']):
        move(c,m['carrier_id'],f'port:{i%4}',second)
        b.departed(m['carrier_id'])
        move(c,m['carrier_id'],m['rack_slot_id'],second,True)
        b.arrived(m['carrier_id'],m['rack_slot_id'])
    assert b.ready(second) and origin['status']=='cleared'


@pytest.mark.parametrize('foreign', [{'active_execution_id':'foreign-execution'},{'assembly_id':'foreign-assembly'}])
def test_actual_start_cannot_replace_foreign_tool_ownership(foreign):
    b,r,c,tools,s,streams,lots,events=factory()
    aid=b.commit();a=b.assemblies[aid]
    for i,m in enumerate(a['members']):deliver(b,c,a,m,i)
    tools['tool-a'].update(foreign);before=deepcopy((a,tools,r.visits,lots))
    with pytest.raises(RuntimeError,match='owner'):b.started(aid)
    assert (a,tools,r.visits,lots)==before


def test_unload_preflights_all_members_before_any_route_advances():
    b,r,c,tools,s,streams,lots,events=factory()
    aid=b.commit();a=b.assemblies[aid]
    for i,m in enumerate(a['members']):deliver(b,c,a,m,i)
    b.started(aid);b.finished(aid)
    r.visits[a['members'][-1]['visit_id']]['status']='processing'
    before=deepcopy((a,r.visits,lots,tools));count=len(events)
    with pytest.raises(RuntimeError,match='members'):b.unloaded(aid)
    assert (a,r.visits,lots,tools)==before and len(events)==count
