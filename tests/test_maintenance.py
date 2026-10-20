from copy import deepcopy
from fractions import Fraction
import pytest
from test_manufacturing_facade import factory as manufacturing_factory,place


def duration(us):
    return {'distribution':'constant','mean_seconds':str(Fraction(us,1000000)) if us%1000000==0 else str(us/1000000),'offset_seconds':'0'}


def pm(kind='calendar',first=5,interval=20,repair=3):
    return {'id':'pm','scope':'toolgroup','scope_id':'A','type':kind,'interval':interval,
            'first':{'distribution':'constant','value':str(first)} if kind=='counter' else duration(first),
            'duration':duration(repair)}


def fault(first=5,ttf=20,repair=3):
    return {'id':'break','scope':'area','scope_id':'area','first':duration(first),
            'ttf':duration(ttf),'duration':duration(repair)}


def factory(definitions=(),breakdowns=(),injected=()):
    from fab_sim.maintenance import Maintenance
    m,s,c,lots,events=manufacturing_factory()
    source={'model':{'tool_groups':[{'id':'A','area':'area'}],
                    'maintenance':list(definitions),'breakdowns':list(breakdowns)}}
    topology={'model':{'equipment':[{'id':'t','kind':'ordinary','tool_group_id':'A'}]}}
    maintenance=Maintenance(m,lambda k,p:events.append((k,deepcopy(p))))
    maintenance.initialize(topology,source,injected_faults=list(injected))
    return maintenance,m,s,c,lots,events


def run_until(s,until):
    s.settle(s.now_us)
    while s.next_time_us is not None and s.next_time_us<=until:s.settle(s.next_time_us)
    if s.now_us<until:s.settle(until)


def records(maintenance,kind=None):
    return [r for r in maintenance.causes.values() if kind is None or r['kind']==kind]


def test_calendar_pm_blocks_new_commitments_drains_owned_phases_and_starts_after_unload():
    maintenance,m,s,c,lots,events=factory([pm()]);m.enter_visit('low','1');place(c,'clow','rack');m.delivered('clow','rack')
    run_until(s,5);r=records(maintenance)[0]
    assert r['status']=='pending' and m.tools['t']['pm_pending']
    run_until(s,15);assert r['status']=='active' and r['started_at_us']==15
    assert lots['low']['route_complete_at_us']==15 and c.locations['clow']=='rack'
    run_until(s,18);assert r['status']=='recovered'
    assert records(maintenance)[1]['due_at_us']==35


def test_counter_overshoot_preserves_cumulative_wafer_count_and_each_threshold():
    maintenance,m,s,c,lots,events=factory([pm('counter',10,10,2)])
    m.tools['t']['processed_wafers']=25;m.dirty();s.settle(0)
    assert len(records(maintenance))==2
    assert maintenance.definitions[('t','pm','pm')]['next_threshold']==30
    run_until(s,4);assert [r['started_at_us'] for r in records(maintenance)]==[0,2]
    assert m.tools['t']['processed_wafers']==25 and not m.tools['t']['pm_pending']
    m.tools['t']['processed_wafers']=31;m.dirty();s.settle(4)
    assert len(records(maintenance))==3 and maintenance.definitions[('t','pm','pm')]['next_threshold']==40


def test_pm_duration_samples_only_actual_start_and_never_blocks_committed_drain():
    definition=pm();definition['duration']={'distribution':'uniform','mean_seconds':'0.000003','offset_seconds':'0.000001'}
    maintenance,m,s,c,lots,events=factory([definition]);m.enter_visit('low','1');place(c,'clow','rack');m.delivered('clow','rack')
    binding=maintenance.definitions[('t','pm','pm')];rng=m.streams.get('maintenance',binding['rng_scope'])
    before=rng.getstate();run_until(s,5);assert rng.getstate()==before
    run_until(s,15);assert rng.getstate()!=before and records(maintenance)[0]['started_at_us']==15


def test_breakdown_retains_process_remaining_time_and_repeats_from_recovery():
    maintenance,m,s,c,lots,events=factory(breakdowns=[fault()]);m.enter_visit('low','1');place(c,'clow','rack');m.delivered('clow','rack')
    run_until(s,5);assert records(maintenance)[0]['status']=='active'
    run_until(s,8);assert records(maintenance)[0]['status']=='recovered'
    assert records(maintenance)[1]['due_at_us']==28
    run_until(s,18);assert lots['low']['route_complete_at_us']==18
    assert m.tools['t']['processed_wafers']==25


def test_pm_repair_continues_through_breakdown_but_pending_pm_cannot_start_in_fault():
    maintenance,m,s,c,lots,events=factory([pm(first=0,repair=10)],breakdowns=[fault(first=3,repair=20)])
    run_until(s,10);assert records(maintenance,'pm')[0]['status']=='recovered'
    assert records(maintenance,'breakdown')[0]['status']=='active'
    run_until(s,20);assert records(maintenance,'pm')[1]['status']=='pending'
    run_until(s,23);assert records(maintenance,'pm')[1]['started_at_us']==23


def test_overlapping_injected_faults_require_all_recoveries_without_processing_redraw():
    injected=[{'id':'x','target_id':'t','target_type':'equipment','onset_us':5,'duration_us':3},
              {'id':'y','target_id':'t','target_type':'equipment','onset_us':6,'duration_us':5}]
    maintenance,m,s,c,lots,events=factory(injected=injected);m.enter_visit('low','1');place(c,'clow','rack');m.delivered('clow','rack')
    run_until(s,8);assert len(m.causes['t'])==1
    run_until(s,21);assert lots['low']['route_complete_at_us']==21
    assert all(r['status']=='recovered' for r in records(maintenance))


def test_recovery_and_new_fault_same_tick_do_not_start_ready_work_between_causes():
    injected=[{'id':'x','target_id':'t','target_type':'equipment','onset_us':0,'duration_us':5},
              {'id':'y','target_id':'t','target_type':'equipment','onset_us':5,'duration_us':5}]
    maintenance,m,s,c,lots,events=factory(injected=injected);v=m.enter_visit('low','1');place(c,'clow','rack');m.delivered('clow','rack')
    run_until(s,5);assert not m.executions and m.rules.visits[v]['status']=='pending'
    run_until(s,10);assert m.executions
    run_until(s,25);assert lots['low']['route_complete_at_us']==25


def test_empty_injected_list_does_not_suppress_native_breakdowns():
    maintenance,m,s,c,lots,events=factory(breakdowns=[fault()],injected=[])
    run_until(s,5);assert records(maintenance,'breakdown')[0]['status']=='active'


def test_calendar_recurrence_uses_actual_pm_start_and_serializes_same_tick_due_causes():
    a=pm(first=0,interval=20,repair=4);b={**pm(first=0,interval=30,repair=2),'id':'other'}
    maintenance,m,s,c,lots,events=factory([a,b]);run_until(s,6)
    firsts=[r for r in records(maintenance) if r['status']=='recovered']
    assert sorted((r['definition_id'],r['started_at_us']) for r in firsts)==[('other',0),('pm',2)]
    for r in firsts:
        later=next(v for v in records(maintenance) if v['definition_id']==r['definition_id'] and v['status']=='scheduled')
        expected=20 if r['definition_id']=='pm' else 30
        assert later['due_at_us']==r['started_at_us']+expected


def test_effective_instance_counts_spread_constant_pm_first_times_and_fractional_thresholds():
    from fab_sim.maintenance import Maintenance
    from fab_sim.manufacturing import Manufacturing
    from fab_sim.scheduler import Scheduler
    from fab_sim.rng import Streams
    from fab_sim.custody import Custody
    from test_route_rules import step
    source={'model':{'routes':[{'id':'r','steps':[step(1)],'dedication_components':[]}],
        'tool_groups':[{'id':'A','area':'area','initial_setup':None,'loading_us':0,'unloading_us':0,'source_count':999}],
        'maintenance':[pm(first=3),{**pm('counter',3,10),'id':'counter'}],'breakdowns':[]}}
    tools={tid:{'id':tid,'tool_group_id':'A','rack_slots':[tid+'rack']} for tid in ('a','b')}
    c=Custody([{'id':tid+'rack','kind':'rack','equipment_id':tid} for tid in tools]);s=Scheduler()
    m=Manufacturing(source,s,Streams(7),{},c,tools,lambda k,p:None)
    topology={'model':{'equipment':[{'id':tid,'kind':'ordinary','tool_group_id':'A'} for tid in reversed(tools)]}}
    maintenance=Maintenance(m,lambda k,p:None);maintenance.initialize(topology,source)
    assert [r['due_at_us'] for r in records(maintenance)]==[2,3]
    assert maintenance.definitions[('a','pm','counter')]['next_threshold']==Fraction(3,2)
    assert maintenance.definitions[('b','pm','counter')]['next_threshold']==3


def test_pending_counter_pm_drains_all_remaining_committed_wafer_units():
    from test_cascade_service import factory as cascade_factory
    from fab_sim.maintenance import Maintenance
    m,s,c,lots,events=cascade_factory('wafer',p=3,c=2)
    source={'model':{'tool_groups':[{'id':'A','area':'area'}],
                    'maintenance':[pm('counter',1,100,4)],'breakdowns':[]}}
    maintenance=Maintenance(m,lambda k,p:events.append((k,deepcopy(p))))
    maintenance.initialize({'model':{'equipment':[{'id':'t','kind':'ordinary','tool_group_id':'A'}]}},source)
    run_until(s,5);r=records(maintenance)[0];assert r['status']=='pending'
    run_until(s,56);assert r['started_at_us']==56 and lots['one']['route_complete_at_us']==56
    assert m.tools['t']['processed_wafers']==25


def test_pm_waits_for_incoming_committed_batch_members_and_shared_unload():
    from test_batch_service import factory as batch_factory,deliver
    from fab_sim.maintenance import Maintenance
    m,s,c,lots,events=batch_factory();s.settle(0);a=next(iter(m.batches.assemblies.values()))
    source={'model':{'tool_groups':[{'id':'A','area':'area'}],'maintenance':[pm(first=5)],'breakdowns':[]}}
    maintenance=Maintenance(m,lambda k,p:events.append((k,deepcopy(p))))
    maintenance.initialize({'model':{'equipment':[{'id':'tool-a','kind':'ordinary','tool_group_id':'A'}]}},source)
    run_until(s,5);assert records(maintenance)[0]['status']=='pending'
    for i,member in enumerate(a['members']):deliver(m,c,a,member,i)
    run_until(s,20);assert a['status']=='finished' and records(maintenance)[0]['started_at_us']==20


def test_pm_waits_for_active_exchange_then_pauses_that_handler():
    from fab_sim.maintenance import Maintenance
    from fab_sim.service import SerializedHandler
    m,s,c,lots,events=manufacturing_factory()
    exchange=SerializedHandler(s,'exchange',m.claims,m.dirty,lambda k,p:None)
    m.dispatch_guards.append(exchange.dispatch)
    source={'model':{'tool_groups':[{'id':'A','area':'area'}],'maintenance':[pm(first=2,repair=3)],'breakdowns':[]}}
    maintenance=Maintenance(m,lambda k,p:None,exchange_handlers={'t':exchange})
    maintenance.initialize({'model':{'equipment':[{'id':'t','kind':'ordinary','tool_group_id':'A'}]}},source)
    exchange.request('move','exchange',['carrier'],lambda:5,lambda:None)
    run_until(s,2);assert records(maintenance)[0]['status']=='pending'
    run_until(s,5);assert records(maintenance)[0]['started_at_us']==5 and exchange.causes
    run_until(s,8);assert not exchange.causes


def test_completed_processing_then_same_tick_native_failure_preserves_completion_credit():
    maintenance,m,s,c,lots,events=factory(breakdowns=[fault(first=12,repair=5)])
    v=m.enter_visit('low','1');place(c,'clow','rack');m.delivered('clow','rack')
    run_until(s,12);assert m.rules.visits[v]['status']=='processed' and m.tools['t']['processed_wafers']==25
    assert lots['low'].get('route_complete_at_us') is None
    run_until(s,20);assert lots['low']['route_complete_at_us']==20


def test_public_recovery_rejects_early_without_clearing_active_cause():
    maintenance,m,s,c,lots,events=factory(breakdowns=[fault()]);run_until(s,5);r=records(maintenance)[0]
    with pytest.raises(RuntimeError,match='deadline'):maintenance.recover(r['id'])
    assert r['status']=='active' and r['id'] in m.causes['t']


def test_due_pm_precedes_new_exchange_dispatch_in_same_manufacturing_closure():
    from fab_sim.maintenance import Maintenance
    from fab_sim.service import SerializedHandler
    m,s,c,lots,events=manufacturing_factory()
    exchange=SerializedHandler(s,'exchange',m.claims,m.dirty,lambda k,p:None)
    m.dispatch_guards.append(exchange.dispatch)
    maintenance=Maintenance(m,lambda k,p:None,exchange_handlers={'t':exchange})
    source={'model':{'tool_groups':[{'id':'A','area':'area'}],'maintenance':[pm(first=0)],'breakdowns':[]}}
    maintenance.initialize({'model':{'equipment':[{'id':'t','kind':'ordinary','tool_group_id':'A'}]}},source)
    exchange.request('move','exchange',['carrier'],lambda:5,lambda:None)
    s.settle(0)
    assert records(maintenance)[0]['status']=='active' and exchange.active is None


def test_native_breakdown_streams_are_per_equipment_and_do_not_consume_processing_rng():
    from fab_sim.maintenance import Maintenance
    from fab_sim.manufacturing import Manufacturing
    from fab_sim.scheduler import Scheduler
    from fab_sim.rng import Streams,sample_seconds
    from fab_sim.timebase import to_ticks
    from fab_sim.custody import Custody
    from test_route_rules import step
    stochastic={'distribution':'exponential','mean_seconds':'10','offset_seconds':'0'}
    definition={**fault(),'first':stochastic,'ttf':stochastic}
    source={'model':{'routes':[{'id':'r','steps':[step(1)],'dedication_components':[]}],
        'tool_groups':[{'id':'A','area':'area','initial_setup':None,'loading_us':0,'unloading_us':0}],
        'maintenance':[],'breakdowns':[definition]}}
    tools={tid:{'id':tid,'tool_group_id':'A','rack_slots':[tid+'rack']} for tid in ('a','b')}
    c=Custody([{'id':tid+'rack','kind':'rack','equipment_id':tid} for tid in tools]);s=Scheduler();streams=Streams(7)
    before=streams.get('processing','lot').getstate();m=Manufacturing(source,s,streams,{},c,tools,lambda k,p:None)
    maintenance=Maintenance(m,lambda k,p:None)
    maintenance.initialize({'model':{'equipment':[{'id':tid,'kind':'ordinary','tool_group_id':'A'} for tid in tools]}},source)
    reference=Streams(7)
    for r in records(maintenance):
        binding=maintenance.definitions[(r['tool_id'],'breakdown','break')]
        assert r['due_at_us']==to_ticks(sample_seconds(stochastic,reference.get('maintenance',binding['rng_scope'])))
    assert records(maintenance)[0]['due_at_us']!=records(maintenance)[1]['due_at_us']
    assert streams.get('processing','lot').getstate()==before


def test_actual_dataset1_native_definitions_bind_to_effective_tools_without_mutating_source():
    import os
    from pathlib import Path
    from fab_sim.sources.smt import prepare_manufacturing
    from fab_sim.maintenance import Maintenance
    from fab_sim.manufacturing import Manufacturing
    from fab_sim.scheduler import Scheduler
    from fab_sim.rng import Streams
    from fab_sim.custody import Custody
    path=os.environ.get('FAB_HVLM_WORKBOOK')
    if not path:pytest.skip('set FAB_HVLM_WORKBOOK for native maintenance source coverage')
    source=prepare_manufacturing(Path(path));before=deepcopy(source);tools={};slots=[]
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
    maintenance=Maintenance(m,lambda k,p:None)
    topology={'model':{'equipment':[{'id':tid,'kind':'ordinary','tool_group_id':t['tool_group_id']} for tid,t in tools.items()]}}
    maintenance.initialize(topology,source,injected_faults=[]);s.settle(0)
    assert len([b for b in maintenance.definitions.values() if b['kind']=='pm'])==292
    expected_breakdowns=sum(g['area']==d['scope_id'] for g in source['model']['tool_groups'] for d in source['model']['breakdowns'])
    assert len([b for b in maintenance.definitions.values() if b['kind']=='breakdown'])==expected_breakdowns
    assert all(t['processed_wafers']==0 for t in tools.values()) and source==before


@pytest.mark.parametrize('replacement',[{'duration_us':0},{'onset_us':-1},{'target_id':'unknown'},{'target_type':'rail'}])
def test_invalid_injected_equipment_faults_reject_instead_of_silently_ignoring(replacement):
    record={'id':'bad','target_type':'equipment','target_id':'t','onset_us':0,'duration_us':1,**replacement}
    with pytest.raises(ValueError,match='injected'):factory(injected=[record])


def test_public_pm_start_requires_available_tool_before_sampling_or_pausing():
    definition=pm(first=0);definition['duration']={'distribution':'uniform','mean_seconds':'0.000003','offset_seconds':'0.000001'}
    maintenance,m,s,c,lots,events=factory([definition]);m.tools['t']['available']=False;s.settle(0)
    r=records(maintenance)[0];rng=m.streams.get('maintenance',r['binding']['rng_scope']);before=rng.getstate()
    assert r['status']=='pending'
    with pytest.raises(RuntimeError,match='available'):maintenance.start(r['id'])
    assert r['status']=='pending' and rng.getstate()==before and not m.causes['t']
