import pytest
from test_route_rules import step
from fab_sim.scheduler import Scheduler
from fab_sim.rng import Streams
from fab_sim.custody import Custody


def factory(steps=None, rack_count=1):
    from fab_sim.manufacturing import Manufacturing
    s=Scheduler(); streams=Streams(7); events=[]
    source={'model':{'routes':[{'id':'r','steps':steps or [step(1)],'dedication_components':[]}],
                    'tool_groups':[{'id':'A','initial_setup':None,'setup_transitions':[], 'loading_us':2,'unloading_us':3}]}}
    lots={k:{'route_id':'r','priority':p,'wafer_count':25,'carrier_id':'c'+k,'admitted_at_us':0}
          for k,p in [('low',10),('hot',30)]}
    racks = ['rack'] + [f'rack-extra:{i}' for i in range(1, rack_count)]
    tools={'t':{'id':'t','tool_group_id':'A','rack_slots':racks}}
    custody=Custody([{'id':sid,'kind':'rack','equipment_id':'t'} for sid in racks] +
                    [{'id':'entry','kind':'entry'}])
    m=Manufacturing(source,s,streams,lots,custody,tools,lambda k,p:events.append((k,dict(p))))
    return m,s,custody,lots,events


def place(c,carrier,slot):
    assert c.reserve(slot,'test'); c.place(carrier,slot,'test')


def drain(s):
    s.settle(s.now_us)
    while s.next_time_us is not None:s.settle(s.next_time_us)


def test_physical_load_process_unload_retains_carrier_and_distinct_milestones():
    m,s,c,lots,events=factory(); v=m.enter_visit('low','1')
    place(c,'clow','rack'); m.delivered('clow','rack'); s.settle(0)
    assert m.rules.visits[v]['status']=='pending'
    s.settle(2); assert m.rules.visits[v]['processing_started_at_us']==2
    s.settle(12); assert m.rules.visits[v]['status']=='processed'
    assert lots['low'].get('route_complete_at_us') is None
    s.settle(15); assert lots['low']['route_complete_at_us']==15
    assert c.locations['clow']=='rack' and m.setups['t'].completed_lots==1
    assert m.tools['t']['processed_wafers']==25


def test_upstream_hot_lot_does_not_idle_physically_ready_regular_lot():
    m,s,c,lots,events=factory(); low=m.enter_visit('low','1'); m.enter_visit('hot','1')
    place(c,'clow','rack'); place(c,'chot','entry'); m.delivered('clow','rack'); s.settle(0)
    assert m.rules.visits[low].get('execution_id')
    assert not m.rules.visits[lots['hot']['current_visit_id']].get('execution_id')
    drain(s)


def test_delivery_requires_matching_unclaimed_physical_rack_and_preserves_age():
    m,s,c,lots,events=factory(); v=m.enter_visit('low','1'); place(c,'clow','entry')
    with pytest.raises(ValueError,match='rack'):m.delivered('clow','rack')
    assert m.rules.visits[v]['queue_ready_at_us'] is None
    c.reserve_many(['rack'],'move',origins={'entry':'clow'}); c.move('clow','entry','rack','move')
    m.delivered('clow','rack'); s.settle(0)
    with pytest.raises(ValueError):m.delivered('clow','rack')
    assert m.rules.visits[v]['queue_ready_at_us']==0


def test_same_tool_next_visit_reuses_rack_only_after_unload():
    m,s,c,lots,events=factory([step(1),step(2)]); first=m.enter_visit('low','1')
    place(c,'clow','rack'); m.delivered('clow','rack'); drain(s)
    history=lots['low']['route_history']; assert len(history)==2
    second=m.rules.visits[history[1]]
    assert second['queue_ready_at_us']==15 and second['processing_started_at_us']==17
    assert lots['low']['route_complete_at_us']==30 and c.locations['clow']=='rack'


def test_pm_pending_blocks_new_commitments_but_committed_job_drains():
    m,s,c,lots,events=factory(); v=m.enter_visit('low','1'); place(c,'clow','rack'); m.delivered('clow','rack')
    m.tools['t']['pm_pending']=True; s.settle(0); assert not m.executions
    m.tools['t']['pm_pending']=False; m.dirty(); s.settle(0)
    m.tools['t']['pm_pending']=True; drain(s)
    assert m.rules.visits[v]['status']=='unloaded'


def test_downtime_pauses_processing_without_redraw_and_retains_ownership():
    m,s,c,lots,events=factory(); v=m.enter_visit('low','1'); place(c,'clow','rack'); m.delivered('clow','rack')
    s.settle(0); s.settle(2); s.settle(5); m.pause('t','fault'); s.settle(20)
    assert m.rules.visits[v]['status']=='processing' and m.claims['clow']
    m.resume('t','fault'); drain(s)
    assert m.rules.visits[v]['processing_completed_at_us']==27
    assert lots['low']['route_complete_at_us']==30 and not m.claims


def test_setup_completion_before_loading_and_credits_at_processing_end():
    target=step(1,setup={'id':'B','when':'need','fallback':{'distribution':'constant','mean_seconds':'0.000004','offset_seconds':'0'}})
    m,s,c,lots,events=factory([target]); v=m.enter_visit('low','1')
    place(c,'clow','rack'); m.delivered('clow','rack'); s.settle(0)
    assert m.setups['t'].current is None
    s.settle(4); assert m.setups['t'].current=='B' and m.setups['t'].completed_lots==0
    s.settle(6); assert m.rules.visits[v]['processing_started_at_us']==6
    s.settle(16); assert m.setups['t'].completed_lots==1
    drain(s)


def test_transfer_reservation_prevents_commitment_without_rng_draw():
    m,s,c,lots,events=factory(); m.enter_visit('low','1'); place(c,'clow','rack'); m.delivered('clow','rack')
    assert c.reserve_many([],'transfer',origins={'rack':'clow'})
    before=m.streams.get('processing','low').getstate(); s.settle(0)
    assert not m.executions and m.streams.get('processing','low').getstate()==before


def test_load_completion_same_tick_fault_does_not_start_or_bind_processing():
    m,s,c,lots,events=factory(); v=m.enter_visit('low','1'); place(c,'clow','rack'); m.delivered('clow','rack')
    s.register('fault',lambda p:m.pause('t','fault')); s.schedule(2,2,'fault',{})
    s.settle(0); s.settle(2)
    assert m.rules.visits[v]['status']=='pending'
    assert not lots['low']['dedication']
    s.settle(20); m.resume('t','fault'); drain(s)
    assert m.rules.visits[v]['processing_started_at_us']==20


def test_commitment_holds_rack_against_exchange_until_unload():
    m,s,c,lots,events=factory(); m.enter_visit('low','1'); place(c,'clow','rack'); m.delivered('clow','rack')
    s.settle(0)
    assert not c.reserve_many([],'exchange',origins={'rack':'clow'})
    s.settle(2); s.settle(12)
    assert not c.reserve_many([],'exchange',origins={'rack':'clow'})
    s.settle(15); assert c.reserve_many([],'exchange',origins={'rack':'clow'})


def test_custody_notifications_observe_complete_commitment_and_unload():
    m,s,c,lots,events=factory(); v=m.enter_visit('low','1'); place(c,'clow','rack'); m.delivered('clow','rack')
    snapshots=[]
    c.on_change=lambda:snapshots.append((c.slots['rack']['reservation'],m.tools['t']['active_execution_id'],m.rules.visits[v]['status']))
    drain(s)
    eid=m.rules.visits[v]['execution_id']
    assert snapshots==[(eid,eid,'pending'),(None,None,'unloaded')]


def test_unload_preflights_tool_owner_before_releasing_rack_or_advancing_route():
    m,s,c,lots,events=factory(); v=m.enter_visit('low','1'); place(c,'clow','rack'); m.delivered('clow','rack')
    s.settle(0); s.settle(2); s.settle(12)
    m.tools['t']['active_execution_id']='foreign'
    with pytest.raises(RuntimeError,match='ownership'):s.settle(15)
    assert m.tools['t']['active_execution_id']=='foreign'
    assert m.rules.visits[v]['status']=='processed' and c.slots['rack']['reservation']


def test_uncommitted_ready_carrier_refreshes_rack_after_move_without_resetting_age():
    m,s,c,lots,events=factory(); v=m.enter_visit('low','1'); place(c,'clow','rack'); m.delivered('clow','rack')
    m.tools['t']['pm_pending']=True; s.settle(0); s.settle(5)
    c.slots['rack2']={'id':'rack2','kind':'rack','equipment_id':'t','carrier_id':None,'reservation':None}
    m.tools['t']['rack_slots'].append('rack2')
    c.reserve_many(['rack2'],'move',origins={'rack':'clow'}); c.move('clow','rack','rack2','move')
    m.delivered('clow','rack2'); m.tools['t']['pm_pending']=False; m.dirty(); drain(s)
    assert m.rules.visits[v]['queue_ready_at_us']==0
    assert m.rules.visits[v]['rack_slot_id']=='rack2' and c.locations['clow']=='rack2'


def test_public_completion_rejects_before_owned_timer_finishes_without_credits():
    m,s,c,lots,events=factory(); v=m.enter_visit('low','1'); place(c,'clow','rack'); m.delivered('clow','rack')
    s.settle(0); s.settle(2); eid=m.rules.visits[v]['execution_id']
    with pytest.raises(RuntimeError,match='completion'):m.processing_complete(eid)
    assert m.tools['t']['processed_wafers']==0 and m.setups['t'].completed_lots==0
    drain(s); assert lots['low']['route_complete_at_us']==15
