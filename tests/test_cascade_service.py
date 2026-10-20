from copy import deepcopy
import pytest
from test_route_rules import step
from fab_sim.scheduler import Scheduler
from fab_sim.rng import Streams
from fab_sim.custody import Custody
from fab_sim.manufacturing import Manufacturing


def factory(mode='lot',p=12,c=5,loading=2,unloading=3,setup=None):
    definition=step(1,processing_unit=mode,cascade_interval_us=c,setup=setup,
                    processing={'distribution':'constant','mean_seconds':str(p/1000000),'offset_seconds':'0'})
    source={'model':{'routes':[{'id':'r','steps':[definition],'dedication_components':[]}],
                    'tool_groups':[{'id':'A','initial_setup':setup['id'] if setup else None,
                                    'loading_us':loading,'unloading_us':unloading}]}}
    s=Scheduler();streams=Streams(7);events=[]
    lots={lid:{'route_id':'r','priority':priority,'wafer_count':25,'carrier_id':lid+'c','admitted_at_us':0}
          for lid,priority in [('one',20),('two',10)]}
    racks=['rack0','rack1'] if mode=='lot' else ['rack0']
    tools={'t':{'id':'t','tool_group_id':'A','rack_slots':racks}}
    custody=Custody([{'id':sid,'kind':'rack','equipment_id':'t'} for sid in racks]+[{'id':'entry','kind':'entry'}])
    m=Manufacturing(source,s,streams,lots,custody,tools,lambda k,p:events.append((k,deepcopy(p))))
    for lid in lots:m.enter_visit(lid,'1')
    for lid,sid in zip(lots,racks):
        custody.reserve(sid,'admit');custody.place(lid+'c',sid,'admit');m.delivered(lid+'c',sid)
    return m,s,custody,lots,events


def drain(s):
    s.settle(s.now_us)
    while s.next_time_us is not None:s.settle(s.next_time_us)


@pytest.mark.parametrize('p,c,starts,ends',[(12,5,[2,7],[14,21]),(5,5,[2,7],[7,12]),(3,5,[2,7],[5,10])])
def test_lot_cascade_overlaps_after_each_source_load_and_preserves_latency(p,c,starts,ends):
    m,s,custody,lots,events=factory(p=p,c=c);drain(s)
    visits=[m.rules.visits[lot['route_history'][0]] for lot in lots.values()]
    assert [v['processing_started_at_us'] for v in visits]==starts
    assert [v['processing_completed_at_us'] for v in visits]==ends
    assert all(lot['route_complete_at_us']>=v['processing_completed_at_us']+3 for lot,v in zip(lots.values(),visits))
    assert m.tools['t']['processed_wafers']==50 and m.setups['t'].completed_lots==2
    assert custody.locations=={'onec':'rack0','twoc':'rack1'}


def test_wafer_cascade_earns_counters_per_completed_wafer_but_credit_after_whole_lot():
    m,s,custody,lots,events=factory('wafer',p=3,c=2);s.settle(0);s.settle(2);s.settle(4);s.settle(5)
    assert m.tools['t']['processed_wafers']==1 and m.setups['t'].completed_lots==0
    assert m.rules.visits[lots['one']['current_visit_id']]['status']=='processing'
    drain(s);pipe=m.pipelines['t']
    units=list(pipe.units.values());assert len(units)==25
    assert [u['index'] for u in units]==list(range(25))
    assert units[-1]['completed_at_us']==53 and lots['one']['route_complete_at_us']==56
    assert m.tools['t']['processed_wafers']==25 and m.setups['t'].completed_lots==1
    assert len([x for x in events if x[0]=='service_started' and x[1]['kind']=='load'])==1


def test_pending_pm_drains_committed_wafer_units_and_blocks_new_lots():
    m,s,custody,lots,events=factory('wafer',p=3,c=2);s.settle(0);m.tools['t']['pm_pending']=True
    drain(s);assert lots['one']['route_complete_at_us']==56
    assert not m.rules.visits[lots['two']['current_visit_id']].get('execution_id')


def test_cascade_fault_pauses_both_slots_and_service_retaining_sample_and_racks():
    m,s,custody,lots,events=factory();s.settle(0);s.settle(2);s.settle(4);s.settle(7);s.settle(8)
    states={lid:m.streams.get('processing',lid).getstate() for lid in lots}
    m.pause('t','a');m.pause('t','b');s.settle(20);m.resume('t','a');s.settle(23);m.resume('t','b');drain(s)
    visits=[m.rules.visits[lot['route_history'][0]] for lot in lots.values()]
    assert [v['processing_completed_at_us'] for v in visits]==[29,36]
    assert states=={lid:m.streams.get('processing',lid).getstate() for lid in lots}
    assert not m.claims and all(custody.slots[sid]['reservation'] is None for sid in ('rack0','rack1'))


def test_load_completion_same_tick_fault_does_not_admit_cascade_unit():
    m,s,custody,lots,events=factory();s.register('fault',lambda p:m.pause('t','fault'));s.schedule(2,2,'fault',{})
    s.settle(0);s.settle(2);assert not m.pipelines['t'].units or all(u['status']=='pending' for u in m.pipelines['t'].units.values())
    assert all(v['processing_started_at_us'] is None for v in m.rules.visits.values())
    s.settle(10);m.resume('t','fault');drain(s)
    assert m.rules.visits[lots['one']['route_history'][0]]['processing_started_at_us']==10


def test_early_public_cascade_completion_rejects_without_credits():
    m,s,custody,lots,events=factory('wafer',p=3,c=2);s.settle(0);s.settle(2)
    eid=m.rules.visits[lots['one']['current_visit_id']]['execution_id']
    with pytest.raises(RuntimeError,match='completion'):m.processing_complete(eid)
    assert m.setups['t'].completed_lots==0;drain(s)


def test_setup_change_waits_for_committed_pipeline_and_unload_to_drain():
    setup={'id':'A','when':'need','fallback':{'distribution':'constant','mean_seconds':'0.000004','offset_seconds':'0'}}
    m,s,custody,lots,events=factory(setup=setup)
    # Second LOT uses a different source step/recipe at the same group.
    definition=step(2,processing_unit='lot',cascade_interval_us=5,
                    setup={**setup,'id':'B'},processing={'distribution':'constant','mean_seconds':'0.000012','offset_seconds':'0'})
    m.rules.routes['r']['steps'].append(definition);m.rules.steps['r']['2']=definition
    second=m.rules.visits[lots['two']['current_visit_id']];second['source_step_id']='2'
    drain(s)
    phases=[(k,p) for k,p in events if k=='service_started' and p['kind']=='setup']
    assert phases[0][1]['started_at_us']>=17
    assert m.rules.visits[second['id']]['processing_started_at_us']>=23


def test_rework_reuses_carrier_with_distinct_cascade_execution_ids():
    m,s,custody,lots,events=factory('wafer',p=0,c=2,loading=0,unloading=0)
    definition=m.rules.steps['r']['1'];definition['rework']={'probability':'1','target_step_id':'1'}
    s.settle(0)
    # First visit completes after wafer24 at48; disable rework after second is entered.
    while len(lots['one']['route_history'])<2:s.settle(s.next_time_us)
    definition['rework']=None;drain(s)
    executions=[e for e in m.executions.values() if e['visit_id'].startswith('one:')]
    assert len(executions)==2 and all(e['status']=='unloaded' for e in executions)
    assert len(m.pipelines['t'].executions)==2 and m.tools['t']['processed_wafers']==50
    assert lots['one']['route_complete_at_us']==98 and custody.locations['onec']=='rack0'


def test_setup_pauses_remaining_admission_interval_during_actual_setup():
    setup={'id':'A','when':'always','fallback':{'distribution':'constant','mean_seconds':'0.000004','offset_seconds':'0'}}
    m,s,custody,lots,events=factory(p=1,c=20,loading=0,unloading=0,setup=setup);drain(s)
    visits=[m.rules.visits[lot['route_history'][0]] for lot in lots.values()]
    assert [v['processing_started_at_us'] for v in visits]==[4,28]
    assert [v['processing_completed_at_us'] for v in visits]==[5,29]


def test_completed_internal_unit_settles_before_same_tick_fault_and_unload_waits():
    m,s,custody,lots,events=factory('wafer',p=1,c=1,loading=0,unloading=3)
    s.register('fault',lambda p:m.pause('t','fault'));s.schedule(25,2,'fault',{})
    s.settle(0)
    while s.now_us<25:s.settle(s.next_time_us)
    first=m.rules.visits[lots['one']['current_visit_id']]
    assert first['status']=='processed' and first['processing_completed_at_us']==25
    assert m.setups['t'].completed_lots==1 and lots['one'].get('route_complete_at_us') is None
    s.settle(30);m.resume('t','fault');drain(s)
    assert lots['one']['route_complete_at_us']==33


def test_actual_dataset1_cascade_service_preserves_each_unit_sample():
    import os
    from pathlib import Path
    from fab_sim.sources.smt import prepare_manufacturing
    from fab_sim.rng import sample_seconds
    from fab_sim.timebase import to_ticks
    path=os.environ.get('FAB_HVLM_WORKBOOK')
    if not path:pytest.skip('set FAB_HVLM_WORKBOOK for actual cascade service')
    source=prepare_manufacturing(Path(path));before=deepcopy(source)
    route=next(r for r in source['model']['routes'] if r['product_id']=='Product_3')
    for sid in ('252','288'):
        definition=next(v for v in route['steps'] if v['source_step_id']==sid)
        gid=definition['tool_group_id'];group=next(g for g in source['model']['tool_groups'] if g['id']==gid)
        s=Scheduler();streams=Streams(7);reference=Streams(7)
        count=25 if definition['processing_unit']=='wafer' else 1
        racks=['rack0','rack1'] if count==1 else ['rack0']
        lot={'route_id':route['id'],'priority':10,'wafer_count':25,'carrier_id':'carrier','admitted_at_us':0}
        tools={'t':{'id':'t','tool_group_id':gid,'rack_slots':racks,
                    'initial_setup':definition['setup']['id'] if definition['setup'] else group['initial_setup']}}
        c=Custody([{'id':r,'kind':'rack','equipment_id':'t'} for r in racks])
        m=Manufacturing(source,s,streams,{'source:0':lot},c,tools,lambda k,p:None)
        c.reserve('rack0','admit');c.place('carrier','rack0','admit')
        visit=m.enter_visit('source:0',sid);s.settle(0)
        eid=m.rules.visits[visit]['execution_id']
        while m.executions[eid]['status']!='unloaded':s.settle(s.next_time_us)
        units=[u for u in m.pipelines['t'].units.values() if u['execution_id']==eid]
        expected=[to_ticks(sample_seconds(definition['processing'],reference.get('processing','source:0'))) for _ in range(count)]
        assert [u['processing_us'] for u in units]==expected
        assert units[0]['started_at_us']==group['loading_us']
        assert m.rules.visits[visit]['unloaded_at_us']==units[-1]['completed_at_us']+group['unloading_us']
        assert m.tools['t']['processed_wafers']==25 and source==before


def test_complete_dataset1_model_initializes_every_required_tool_group_without_wip():
    import os
    from pathlib import Path
    from fab_sim.sources.smt import prepare_manufacturing
    path=os.environ.get('FAB_HVLM_WORKBOOK')
    if not path:pytest.skip('set FAB_HVLM_WORKBOOK for complete facade initialization')
    source=prepare_manufacturing(Path(path));tools={};slots=[]
    steps=[v for r in source['model']['routes'] for v in r['steps'] if v['kind']=='processing']
    for group in source['model']['tool_groups']:
        definitions=[v for v in steps if v['tool_group_id']==group['id']]
        capacity=1
        if group['batching']:capacity=max(v['batch_max']//25 for v in definitions)
        elif group['cascading'] and definitions[0]['processing_unit']=='lot':capacity=2
        tid=group['id']+':1';racks=[tid+':rack:'+str(i) for i in range(capacity)]
        tools[tid]={'id':tid,'tool_group_id':group['id'],'rack_slots':racks}
        slots.extend({'id':r,'kind':'rack','equipment_id':tid} for r in racks)
    s=Scheduler();m=Manufacturing(source,s,Streams(7),{},Custody(slots),tools,lambda k,p:None)
    s.settle(0)
    assert len(m.tools)==105 and not m.executions and not m.batches.assemblies
    assert len(m.pipelines)==sum(g['cascading'] for g in source['model']['tool_groups'])
    assert all(t['processed_wafers']==0 for t in tools.values())
    assert all(state.completed_lots==0 and state.minimum_runs==0 for state in m.setups.values())


@pytest.mark.parametrize('mode,change',[('lot','rack_hold'),('wafer','rack_hold'),('lot','visit_owner')])
def test_unit_completion_preflights_physical_and_visit_ownership_before_counters(mode,change):
    m,s,custody,lots,events=factory(mode,p=3,c=5);s.settle(0);s.settle(2)
    visit=m.rules.visits[lots['one']['current_visit_id']];eid=visit['execution_id']
    if change=='rack_hold':custody.release('rack0',eid)
    else:lots['one']['current_visit_id']='foreign'
    if s.next_time_us==4:s.settle(4)
    with pytest.raises(RuntimeError,match='ownership'):s.settle(5)
    assert m.tools['t']['processed_wafers']==0 and m.setups['t'].completed_lots==0
    assert visit['status']=='processing'
