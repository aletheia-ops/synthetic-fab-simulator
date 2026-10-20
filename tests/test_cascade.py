import pytest
from fab_sim.scheduler import Scheduler


def pipeline(mode='lot',setup='A'):
    from fab_sim.service import Cascade
    s=Scheduler();events=[]
    c=Cascade(s,'tool',mode,setup,lambda:s.mark_dirty('manufacturing'),lambda k,p:events.append((k,p)))
    s.register_pass('manufacturing',c.dispatch)
    return c,s,events


def drain(s):
    s.settle(s.now_us)
    while s.next_time_us is not None:s.settle(s.next_time_us)


@pytest.mark.parametrize('p,c,starts,ends',[(12,5,[0,5,12],[12,19,26]),(5,5,[0,5,10],[5,10,15]),(3,5,[0,5,10],[3,8,13])])
def test_two_slots_cover_all_processing_interval_relationships(p,c,starts,ends):
    pipe,s,events=pipeline();done=[]
    for i in range(3):pipe.enqueue_lot(str(i),1,c,lambda:p,lambda u:done.append(s.now_us),'A')
    drain(s)
    assert [v['started_at_us'] for v in pipe.units.values()]==starts
    assert done==ends and pipe.slots==[None,None]
    assert all(v['second_service_us']>=0 for v in pipe.units.values())


def test_blocked_slot_two_holds_first_slot_and_draws_only_actual_starts():
    pipe,s,events=pipeline();draws=[]
    for i in range(3):pipe.enqueue_lot(str(i),1,5,lambda:draws.append(s.now_us) or 12,lambda u:None,'A')
    s.settle(0);s.settle(5);s.settle(10)
    assert draws==[0,5] and all(pipe.slots)
    assert pipe.units[pipe.slots[0]]['first_complete']
    s.settle(12);assert draws==[0,5,12]
    drain(s)


def test_wafer_order_no_interleaving_and_per_unit_samples():
    pipe,s,events=pipeline('wafer');draws=[];done=[]
    pipe.enqueue_lot('z',3,2,lambda:draws.append(('z',s.now_us)) or 3,lambda u:done.append(u),'A')
    pipe.enqueue_lot('a',2,2,lambda:draws.append(('a',s.now_us)) or 3,lambda u:done.append(u),'A')
    drain(s)
    assert [v['lot_id'] for v in pipe.units.values()]==['z']*3+['a']*2
    assert [d[0] for d in draws]==['z']*3+['a']*2
    assert done==['z:wafer:0','z:wafer:1','z:wafer:2','a:wafer:0','a:wafer:1']


def test_setup_requires_pipeline_drain_and_pauses_admission_interval():
    pipe,s,events=pipeline();done=[]
    pipe.enqueue_lot('one',1,10,lambda:3,lambda u:done.append(s.now_us),'A')
    pipe.enqueue_lot('two',1,10,lambda:3,lambda u:done.append(s.now_us),'B')
    s.settle(0)
    with pytest.raises(RuntimeError,match='drain'):pipe.begin_setup('B')
    s.settle(3);pipe.begin_setup('B');assert pipe.admission.remaining_us==7
    s.settle(8);pipe.end_setup();s.settle(15);drain(s)
    assert done==[3,18]


def test_faults_pause_both_slots_and_admission_without_redrawing():
    pipe,s,events=pipeline();draws=[];done=[]
    for i in range(2):pipe.enqueue_lot(str(i),1,5,lambda:draws.append(s.now_us) or 12,lambda u:done.append(s.now_us),'A')
    s.settle(0);s.settle(5);s.settle(7);pipe.pause('x');pipe.pause('y')
    s.settle(9);pipe.resume('x');s.settle(10);pipe.resume('y');drain(s)
    assert draws==[0,5] and done==[15,22]


def test_zero_samples_are_causal_and_interval_still_limits_admission():
    pipe,s,events=pipeline();done=[]
    for i in range(2):pipe.enqueue_lot(str(i),1,5,lambda:0,lambda u:done.append(s.now_us),'A')
    drain(s);assert done==[0,5]


def test_not_ready_and_wrong_setup_unit_does_not_draw_or_block_matching_work():
    pipe,s,events=pipeline();draws=[]
    pipe.enqueue_lot('wrong',1,5,lambda:draws.append('wrong') or 2,lambda u:None,'B')
    pipe.enqueue_lot('blocked',1,5,lambda:draws.append('blocked') or 2,lambda u:None,'A',eligible=lambda:False)
    pipe.enqueue_lot('ready',1,5,lambda:draws.append('ready') or 2,lambda u:None,'A')
    drain(s);assert draws==['ready']


def test_completion_then_fault_does_not_admit_another_unit_at_same_tick():
    pipe,s,events=pipeline();draws=[]
    for i in range(2):pipe.enqueue_lot(str(i),1,5,lambda:draws.append(s.now_us) or 5,lambda u:None,'A')
    s.register('fault',lambda p:pipe.pause('equipment'))
    s.schedule(5,2,'fault',{});s.settle(0);s.settle(5)
    assert draws==[0]
    s.settle(8);pipe.resume('equipment');drain(s)
    assert draws==[0,8]


def test_fault_during_setup_preserves_both_independent_admission_pause_causes():
    pipe,s,events=pipeline()
    pipe.enqueue_lot('old',1,10,lambda:3,lambda u:None,'A')
    pipe.enqueue_lot('new',1,10,lambda:3,lambda u:None,'B')
    s.settle(0);s.settle(3);pipe.begin_setup('B');pipe.pause('x')
    s.settle(5);pipe.resume('x');s.settle(8);pipe.end_setup();drain(s)
    assert pipe.units['new:lot:0']['started_at_us']==15


def test_dataset1_cascade_rows_keep_per_unit_source_samples_including_p_below_c():
    import os
    from pathlib import Path
    from fab_sim.sources.smt import prepare_manufacturing
    from fab_sim.rng import Streams,sample_seconds
    from fab_sim.timebase import to_ticks
    path=os.environ.get('FAB_HVLM_WORKBOOK')
    if not path:pytest.skip('set FAB_HVLM_WORKBOOK for source cascade coverage')
    source=prepare_manufacturing(Path(path))
    route=next(r for r in source['model']['routes'] if r['product_id']=='Product_3')
    saw_short=False
    for sid in ('252','288'):
        definition=next(v for v in route['steps'] if v['source_step_id']==sid)
        mode=definition['processing_unit'];count=25 if mode=='wafer' else 1
        recipe=definition['setup']['id'] if definition['setup'] else None
        pipe,s,events=pipeline(mode,recipe);streams=Streams(7);reference=Streams(7)
        expected=[to_ticks(sample_seconds(definition['processing'],reference.get('processing','source:0'))) for _ in range(count)]
        pipe.enqueue_lot('source:0',count,definition['cascade_interval_us'],
            lambda:to_ticks(sample_seconds(definition['processing'],streams.get('processing','source:0'))),lambda u:None,recipe)
        drain(s)
        assert [u['processing_us'] for u in pipe.units.values()]==expected
        assert streams.get('processing','source:0').getstate()==reference.get('processing','source:0').getstate()
        assert all(u['first_service_us']+u['second_service_us']==u['processing_us'] for u in pipe.units.values())
        saw_short=saw_short or any(p<definition['cascade_interval_us'] for p in expected)
    assert saw_short


def test_zero_second_phase_completes_before_new_fault_at_boundary():
    pipe,s,events=pipeline();done=[]
    pipe.enqueue_lot('one',1,5,lambda:5,lambda u:done.append(s.now_us),'A')
    s.register('fault',lambda p:pipe.pause('fault'))
    s.schedule(5,2,'fault',{});s.settle(0);s.settle(5)
    assert done==[5] and pipe.slots==[None,None]


def test_wafer_setup_waits_for_entire_committed_lot_to_drain():
    pipe,s,events=pipeline('wafer')
    pipe.enqueue_lot('one',2,5,lambda:2,lambda u:None,'A')
    s.settle(0);s.settle(2)
    assert pipe.slots==[None,None]
    with pytest.raises(RuntimeError,match='drain'):pipe.begin_setup('B')
    drain(s);pipe.begin_setup('B');pipe.end_setup()
    assert all(u['status']=='complete' for u in pipe.units.values())


def test_same_logical_lot_has_distinct_visit_executions_and_no_wafer_interleaving():
    pipe,s,events=pipeline('wafer');done=[]
    for visit in ('visit:1','visit:2'):
        pipe.enqueue_lot('one',2,2,lambda:3,lambda u:done.append(u),'A',execution_id=visit)
    drain(s)
    assert done==['visit:1:wafer:0','visit:1:wafer:1','visit:2:wafer:0','visit:2:wafer:1']
    assert [u['started_at_us'] for u in pipe.units.values()]==[0,2,5,7]
    assert all(u['lot_id']=='one' for u in pipe.units.values())
