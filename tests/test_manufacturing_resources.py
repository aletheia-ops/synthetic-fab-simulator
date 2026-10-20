"""Group-local wakes preserve physical ordering and inclusive readiness."""
from copy import deepcopy
from fab_sim.custody import Custody
from fab_sim.manufacturing import Manufacturing
from fab_sim.resource_changes import ResourceChange
from fab_sim.resource_index import ResourceIndex
from fab_sim.scheduler import Scheduler
from fab_sim.rng import Streams
from fab_sim.probes import Probes,state_projection,domain_digest
from test_route_rules import step
from test_manufacturing_facade import place,drain


def two_groups():
    s=Scheduler();events=[]
    groups=[{'id':g,'initial_setup':None,'loading_us':2,'unloading_us':3} for g in ('A','B')]
    routes=[{'id':g,'steps':[step(1,tool_group_id=g)],'dedication_components':[]} for g in ('A','B')]
    tools={g:{'id':g,'tool_group_id':g,'rack_slots':[g+':rack']} for g in ('A','B')}
    c=Custody([{'id':g+':rack','kind':'rack','equipment_id':g} for g in ('A','B')])
    lots={g:{'route_id':g,'priority':10,'wafer_count':25,'carrier_id':g+':carrier','admitted_at_us':0} for g in ('A','B')}
    m=Manufacturing({'model':{'routes':routes,'tool_groups':groups}},s,Streams(7),lots,c,tools,
        lambda k,p:events.append((s.now_us,k,deepcopy(p))))
    m.resource_index=ResourceIndex(c.slots,tools,{'docks':[]});m.probes=Probes()
    m.carrier_lot_lookup=lambda carrier:(carrier.split(':')[0],lots[carrier.split(':')[0]])
    for lid in lots:m.enter_visit(lid,'1')
    s.settle(0)
    return m,s,c,lots,events


def test_unrelated_tool_group_is_not_considered():
    m,s,c,lots,events=two_groups();old=m.rules.current_visits
    def guarded(group=None):
        assert group!='B','unrelated group candidate lookup'
        return old(group)
    m.rules.current_visits=guarded
    place(c,'A:carrier','A:rack');m.delivered('A:carrier','A:rack');s.settle(0);s.settle(2)
    assert m.rules.visits[lots['A']['current_visit_id']]['processing_started_at_us']==2
    assert m.tools['B']['active_execution_id'] is None


def test_change_during_dispatch_requires_another_same_tick_pass():
    m,s,c,lots,events=two_groups()
    called=[]
    def guard():
        called.append(s.now_us)
        if len(called)==1:m.resource_changed(ResourceChange(group_ids=frozenset({'B'})))
    m.dispatch_guards.append(guard)
    m.resource_changed(ResourceChange(group_ids=frozenset({'A'})));s.settle(0)
    assert called==[0,0]


def test_repair_completion_and_deadline_preserve_same_tick_start():
    from test_manufacturing_facade import factory
    m,s,c,lots,events=factory([step(1,cqt={'target_step_id':'2','threshold_us':10}),step(2)])
    m.resource_index=ResourceIndex(c.slots,m.tools,{'docks':[]})
    first=m.enter_visit('low','1');place(c,'clow','rack');m.delivered('clow','rack')
    s.settle(0);s.settle(2);s.settle(12)
    m.tools['t']['available']=False;s.settle(15);m.pause('t','fault')
    second=lots['low']['current_visit_id'];assert second!=first
    def repair(payload):
        m.tools['t']['available']=True;m.resume('t','fault')
    s.register('repair',repair);s.schedule(20,1,'repair',{})
    s.settle(20);s.settle(22)
    assert m.rules.visits[second]['processing_started_at_us']==22
    assert m.rules.violation_count==0


def test_shared_batch_and_campaign_rank_matches_full_dispatch():
    from test_batch_service import factory as batch_factory,deliver
    from test_campaigns import factory as campaign_factory,deliver_members
    def run_batch(selective):
        m,s,c,lots,events=batch_factory()
        if selective:m.resource_index=ResourceIndex(c.slots,m.tools,{'docks':[]})
        s.settle(0);a=next(iter(m.batches.assemblies.values()))
        for i,member in enumerate(a['members']):deliver(m,c,a,member,i);s.settle(0)
        drain(s)
        return events,{lid:lot['route_complete_at_us'] for lid,lot in lots.items()}
    def run_campaign(selective):
        state=campaign_factory();campaigns,t,m,s,c,lots,vehicles,events=state
        if selective:m.resource_index=ResourceIndex(c.slots,m.tools,t.model)
        campaigns.dispatch();deliver_members(state)
        while campaigns.active_by_tool:s.settle(s.next_time_us)
        return events,campaigns.describe(),m.setups['t'].completed_lots
    assert run_batch(True)==run_batch(False)
    assert run_campaign(True)==run_campaign(False)


def test_group_worklist_is_derived_but_physical_claims_remain_visible():
    m,s,c,lots,events=two_groups();before=domain_digest(state_projection(m))
    m._dirty_groups.mark({'A'});m.probes.count_work('manufacturing_tools_considered')
    assert domain_digest(state_projection(m))==before
    m.tools['A']['available']=False
    assert domain_digest(state_projection(m))!=before
