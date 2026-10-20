from copy import deepcopy
from decimal import Decimal
import pytest


def step(number,**overrides):
    return {'source_step_id':str(number),'kind':'processing','tool_group_id':'A',
            'sampling_probability':'1','rework':None,'cqt':None,'setup':None,
            'processing':{'distribution':'constant','mean_seconds':'0.00001','offset_seconds':'0'},
            'processing_unit':'lot','batch_min':None,'batch_max':None,'cascade_interval_us':None,
            **overrides}


def controller(steps=None,components=None):
    from fab_sim.scheduler import Scheduler
    from fab_sim.rng import Streams
    from fab_sim.manufacturing import RouteRules
    source={'model':{'routes':[{'id':'r','steps':steps or [step(1),step(2)],
                               'dedication_components':components or []}]}}
    scheduler=Scheduler(); streams=Streams(7); events=[]
    lots={'lot:1':{'route_id':'r','admitted_at_us':0,'priority':10,'wafer_count':25}}
    rules=RouteRules(source,scheduler,streams,lots,lambda kind,payload:events.append((kind,deepcopy(payload))))
    return rules,scheduler,streams,lots,events


def test_sampling_extremes_skip_without_rng_and_abstractions_are_replaced():
    rules,s,streams,lots,events=controller([step(1,sampling_probability='0'),
        {'source_step_id':'2','kind':'transport_abstraction','replacement':{'next_executable_step_id':'3'}},step(3)])
    rng=streams.get('processing','lot:1'); before=rng.getstate()
    visit=rules.enter_visit('lot:1','1')
    assert rules.visits[visit]['source_step_id']=='3' and rng.getstate()==before
    assert [kind for kind,payload in events][:2]==['visit_skipped','transport_abstraction_replaced']
    assert lots['lot:1']['current_visit_id']==visit


def test_intermediate_sampling_is_once_per_candidate_visit():
    rules,s,streams,lots,events=controller([step(1,sampling_probability='0.5'),step(2)])
    rng=streams.get('processing','lot:1'); state=rng.getstate()
    visit=rules.enter_visit('lot:1','1'); after=rng.getstate()
    assert state!=after
    rules.eligible_tools(visit,['tool-a','tool-b']); rules.eligible_tools(visit,['tool-a'])
    assert rng.getstate()==after


def test_dedication_binds_at_actual_start_and_retains_binding_through_rework():
    component={'id':'same-tool','source_step_ids':['1','2']}
    rules,s,streams,lots,events=controller([step(1,rework={'probability':'1','target_step_id':'1'}),step(2)],[component])
    first=rules.enter_visit('lot:1','1')
    assert rules.eligible_tools(first,['a','b'])==['a','b'] and not lots['lot:1']['dedication']
    rules.started(first,'b'); rules.processing_complete(first)
    assert rules.visits[first]['next_source_step_id']=='1'
    again=rules.after_unload(first)
    assert again!=first and rules.eligible_tools(again,['a','b'])==['b']
    with pytest.raises(RuntimeError,match='dedication'): rules.started(again,'a')
    assert len(lots['lot:1']['route_history'])==2


def test_live_group_index_follows_unload_rework_and_route_completion():
    rules,s,streams,lots,events=controller([step(1),step(2,tool_group_id='B',
        rework={'probability':'1','target_step_id':'1'})])
    first=rules.enter_visit('lot:1','1')
    assert [v['id'] for v in rules.current_visits('A')]==[first]
    assert not rules.current_visits('B')
    rules.started(first,'a');rules.processing_complete(first)
    second=rules.after_unload(first)
    assert not rules.current_visits('A')
    assert [v['id'] for v in rules.current_visits('B')]==[second]
    rules.started(second,'b');rules.processing_complete(second)
    again=rules.after_unload(second)
    assert not rules.current_visits('B')
    assert [v['id'] for v in rules.current_visits()]==[again]
    rules.started(again,'a');rules.processing_complete(again)
    # End at the supplied next step boundary without another rework cycle.
    rules.visits[again]['next_source_step_id']=None
    assert rules.after_unload(again) is None
    assert not rules.current_visits() and not rules.current_visits('A')
    assert len(rules.visits)==3


def test_rework_to_abstraction_continues_from_its_next_executable_step():
    rules,s,streams,lots,events=controller([step(1),
        {'source_step_id':'2','kind':'transport_abstraction','replacement':{'next_executable_step_id':'3'}},
        step(3,rework={'probability':'1','target_step_id':'2'})])
    visit=rules.enter_visit('lot:1','3'); rules.started(visit,'a'); rules.processing_complete(visit)
    again=rules.after_unload(visit)
    assert rules.visits[again]['source_step_id']=='3'


def test_cqt_inclusive_target_start_and_each_declaration_gets_its_own_clock():
    rules,s,streams,lots,events=controller([step(1,cqt={'target_step_id':'2','threshold_us':5}),step(2)])
    first=rules.enter_visit('lot:1','1'); rules.started(first,'a'); rules.processing_complete(first)
    second=rules.after_unload(first); s.settle(5); rules.started(second,'a'); s.settle(6)
    assert rules.violation_count==0
    rules.processing_complete(second); rules.after_unload(second)
    again=rules.enter_visit('lot:1','1'); rules.started(again,'a'); rules.processing_complete(again)
    rules.after_unload(again); s.settle(12)
    assert len(rules.clocks)==2 and rules.violation_count==1


def test_late_cqt_reports_once_and_does_not_block_processing():
    rules,s,streams,lots,events=controller([step(1,cqt={'target_step_id':'2','threshold_us':5}),step(2)])
    first=rules.enter_visit('lot:1','1'); rules.started(first,'a'); rules.processing_complete(first)
    second=rules.after_unload(first); s.settle(6)
    assert rules.violation_count==1
    s.settle(10); rules.started(second,'a'); s.settle(11)
    assert rules.violation_count==1 and rules.visits[second]['processing_started_at_us']==10
    assert sum(kind=='cqt_violation' for kind,p in events)==1


def test_final_unload_marks_route_completion_separately_from_exit():
    rules,s,streams,lots,events=controller([step(1)])
    visit=rules.enter_visit('lot:1','1'); rules.started(visit,'a'); rules.processing_complete(visit)
    assert lots['lot:1'].get('route_complete_at_us') is None
    assert rules.after_unload(visit) is None
    assert lots['lot:1']['route_complete_at_us']==0 and lots['lot:1'].get('exited_at_us') is None


def test_invalid_visit_transitions_do_not_mutate_state_or_draw():
    rules,s,streams,lots,events=controller()
    visit=rules.enter_visit('lot:1','1')
    rng=streams.get('processing','lot:1'); before=rng.getstate()
    with pytest.raises(RuntimeError,match='active'): rules.enter_visit('lot:1','2')
    with pytest.raises(RuntimeError,match='processing'): rules.processing_complete(visit)
    with pytest.raises(RuntimeError,match='complete'): rules.after_unload(visit)
    assert rng.getstate()==before and lots['lot:1']['route_history']==[visit]
    rules.started(visit,'a')
    with pytest.raises(RuntimeError,match='twice'): rules.started(visit,'a')
    rules.processing_complete(visit); rules.after_unload(visit)
    with pytest.raises(RuntimeError,match='complete'): rules.after_unload(visit)


def test_all_skipped_steps_finish_route_without_a_processing_visit():
    rules,s,streams,lots,events=controller([step(1,sampling_probability='0'),step(2,sampling_probability='0')])
    assert rules.enter_visit('lot:1','1') is None
    assert not rules.visits and not lots['lot:1']['route_history']
    assert lots['lot:1']['route_complete_at_us']==0


def test_deadline_event_makes_progress_and_dispatch_at_boundary_is_inclusive():
    rules,s,streams,lots,events=controller([step(1,cqt={'target_step_id':'2','threshold_us':5}),step(2)])
    first=rules.enter_visit('lot:1','1'); rules.started(first,'a'); rules.processing_complete(first)
    second=rules.after_unload(first)
    s.register_pass('manufacturing',lambda: rules.started(second,'a'))
    s.register('ready',lambda p:s.mark_dirty('manufacturing'))
    s.schedule(5,3,'ready',{}); s.settle(s.next_time_us)
    assert rules.violation_count==0 and s.next_time_us is None


def test_rework_draws_only_once_after_successful_processing():
    rules,s,streams,lots,events=controller([step(1,rework={'probability':'1','target_step_id':'1'})])
    rng=streams.get('processing','lot:1'); before=rng.getstate()
    first=rules.enter_visit('lot:1','1'); rules.started(first,'a')
    assert rng.getstate()==before
    rules.processing_complete(first); after=rng.getstate()
    assert before!=after
    with pytest.raises(RuntimeError):rules.processing_complete(first)
    assert rng.getstate()==after


def test_rework_retains_multiple_outstanding_cqt_clocks_for_same_target():
    rules,s,streams,lots,events=controller([step(1,cqt={'target_step_id':'2','threshold_us':5},
        rework={'probability':'1','target_step_id':'1'}),step(2)])
    first=rules.enter_visit('lot:1','1'); rules.started(first,'a'); rules.processing_complete(first)
    second=rules.after_unload(first); rules.started(second,'a'); rules.processing_complete(second)
    assert len(rules.clocks)==2
    s.settle(6)
    assert rules.violation_count==2
    assert all(c['closed_at_us'] is None for c in rules.clocks.values())


def test_complete_dataset1_routes_use_normalized_rules_without_mutating_source():
    import os
    from pathlib import Path
    from fab_sim.sources.smt import prepare_manufacturing
    from fab_sim.manufacturing import RouteRules,SetupState
    from fab_sim.scheduler import Scheduler
    from fab_sim.rng import Streams
    path=os.environ.get('FAB_HVLM_WORKBOOK')
    if not path:pytest.skip('set FAB_HVLM_WORKBOOK for Dataset-1 rule coverage')
    source=prepare_manufacturing(Path(path)); before=deepcopy(source)
    groups={g['id']:g for g in source['model']['tool_groups']}
    counts=[]
    for route in source['model']['routes']:
        lots={'source:0':{'route_id':route['id'],'priority':10,'wafer_count':25}}
        s=Scheduler(); streams=Streams(7)
        rules=RouteRules(source,s,streams,lots,lambda kind,payload:None)
        setups={gid:SetupState(g['initial_setup'],source['model']['setup_transitions']) for gid,g in groups.items()}
        visit=rules.enter_visit('source:0',route['steps'][0]['source_step_id'])
        visits=0
        while visit is not None:
            visits+=1
            assert visits<10000, 'source route must terminate for this pinned seed'
            record=rules.visits[visit]; definition=rules.steps[route['id']][record['source_step_id']]
            setup=setups[definition['tool_group_id']]
            # Resolve expected setup without claiming service execution or capacity.
            assert setup.decision(definition)['expected_us']>=0
            rules.started(visit,definition['tool_group_id']+':tool:1')
            rules.processing_complete(visit)
            visit=rules.after_unload(visit)
        assert lots['source:0']['route_complete_at_us']==0
        assert lots['source:0'].get('exited_at_us') is None
        assert len(lots['source:0']['route_history'])==visits
        counts.append(visits)
    assert all(count>200 for count in counts)
    assert source==before


def test_batch_supply_excludes_consumed_visit_and_retains_possible_rework_without_rng():
    rules,s,streams,lots,_=controller([step(1),step(2),step(3,rework={'probability':'0.5','target_step_id':'2'})])
    visit=rules.enter_visit('lot:1','1')
    state=streams.get('processing','lot:1').getstate()
    assert rules.can_supply_batch('lot:1','1')
    rules.visits[visit]['assembly_id']='batch:already-committed'
    assert not rules.can_supply_batch('lot:1','1')
    assert rules.can_supply_batch('lot:1','2')
    rules.started(visit,'a')
    assert not rules.can_supply_batch('lot:1','1')
    rules.processing_complete(visit)
    visit=rules.after_unload(visit)
    assert rules.can_supply_batch('lot:1','2')
    rules.started(visit,'a')
    assert rules.can_supply_batch('lot:1','2')  # Later positive rework may return.
    assert streams.get('processing','lot:1').getstate()==state


def test_batch_supply_uses_chosen_successor_after_processing_and_abstraction_replacement():
    rules,_,_,lots,_=controller([step(1),
        {'source_step_id':'2','kind':'transport_abstraction','replacement':{'next_executable_step_id':'4'}},
        step(3),step(4,rework={'probability':'0','target_step_id':'1'})])
    visit=rules.enter_visit('lot:1','1');rules.started(visit,'a')
    assert not rules.can_supply_batch('lot:1','3')
    assert rules.can_supply_batch('lot:1','4')
    rules.processing_complete(visit)
    assert not rules.can_supply_batch('lot:1','1')
    visit=rules.after_unload(visit);assert rules.visits[visit]['source_step_id']=='4'
    rules.started(visit,'a');rules.processing_complete(visit)
    assert not rules.can_supply_batch('lot:1','4')
    rules.after_unload(visit)
    assert not rules.can_supply_batch('lot:1','1')
