"""Physical digest includes future work and active domain ownership/timers."""
from fab_sim.probes import domain_digest
from fab_sim.scheduler import Scheduler
from fab_sim.service import Timer
from fab_sim.rng import Streams
from fab_sim.manufacturing import SetupState


def test_projection_detects_tool_setup_timer_future_work_and_rng_changes():
    from fab_sim.probes import state_projection
    s=Scheduler();timer=Timer(s,'phase',10,lambda:None);setup=SetupState(None,[]);rng=Streams(7)
    state={'scheduler':s,'timer':timer,'setup':setup,'streams':rng,'clocks':{'q':{'closed_at_us':None}}}
    def digest():return domain_digest(state_projection(state))
    initial=digest();setup.completed_lots=1;assert digest()!=initial
    before=digest();timer.pause(0,'fault');assert digest()!=before
    before=digest();s.register('future',lambda p:None);s.schedule(100,2,'future',{});assert digest()!=before
    before=digest();rng.get('processing','lot').random();assert digest()!=before
    before=digest();state['clocks']['q']['closed_at_us']=0;assert digest()!=before


def test_projection_does_not_draw_rng_or_include_observation_counters():
    from fab_sim.probes import state_projection
    streams=Streams(7);r=streams.get('processing','lot');before=r.getstate();s=Scheduler()
    a=domain_digest(state_projection({'scheduler':s,'streams':streams}))
    s.handled_events+=100;s.pass_calls+=10;s.recent.append({'inspection':'only'})
    b=domain_digest(state_projection({'scheduler':s,'streams':streams}))
    assert a==b and r.getstate()==before


def test_scheduler_dirty_dispatch_state_changes_projection():
    from fab_sim.probes import state_projection
    s=Scheduler()
    s.register_pass('admission',lambda:False)
    before=domain_digest(state_projection({'scheduler':s}))
    s.mark_dirty('admission')
    assert domain_digest(state_projection({'scheduler':s}))!=before


def test_batch_supply_assessment_cache_does_not_change_domain_projection():
    from test_route_rules import controller,step
    from fab_sim.probes import state_projection,domain_digest
    rules,s,streams,lots,_=controller([step(1),step(2)])
    rules.enter_visit('lot:1','1')
    state={'rules':rules,'scheduler':s,'streams':streams,'lots':lots}
    before=domain_digest(state_projection(state))
    assert rules.can_supply_batch('lot:1','2')
    assert domain_digest(state_projection(state))==before

def test_resource_notification_bookkeeping_does_not_change_domain_projection():
    from fab_sim.probes import state_projection,domain_digest
    from test_custody import custody
    c=custody();before=domain_digest(state_projection(c))
    c.subscribe_resources(lambda change:None)
    c._resource_pending_slots.add('inspection');c._resource_pending_carriers.add('history')
    assert domain_digest(state_projection(c))==before
    c._resource_pending_slots.clear();c._resource_pending_carriers.clear()
    c.reserve('entry:0','real')
    assert domain_digest(state_projection(c))!=before


def test_active_flow_indexes_and_work_counts_are_excluded_from_projection():
    from test_stocker_flow import flow_factory
    from fab_sim.probes import Probes,state_projection
    from fab_sim.resource_index import ResourceIndex
    flow,traffic,t,m,s,c,lots,v,e=flow_factory(1)
    state={'flow':flow,'manufacturing':m,'transfers':t,'custody':c,'lots':lots}
    before=domain_digest(state_projection(state))
    m.resource_index=t.resource_index=ResourceIndex(c.slots,m.tools,t.model)
    flow.probes=m.probes=t.probes=Probes()
    flow.probes.count_work('flow_lots_considered',100)
    flow._active_lots['derived']={};flow._lot_by_carrier['derived']='derived'
    flow._dirty_lots.mark(['derived']);m.subscribe_resources(lambda change:None)
    assert domain_digest(state_projection(state))==before
    c.slots['entry:0']['reservation']='physical'
    assert domain_digest(state_projection(state))!=before


def test_projection_digest_equals_the_projected_digest_throughout_a_run(tmp_path):
    from test_ongoing_runner import observation_fixture
    from fab_sim.runner import build_runtime, load_config
    from fab_sim.probes import domain_digest, projection_digest, state_projection
    from fab_sim.runtime_state import runtime_digest
    config=load_config(observation_fixture(tmp_path,workload={},execution={'horizon_us':1000000000}))
    owners=build_runtime(config,lambda *a:None,initialize=True)
    roots=('scheduler','streams','custody','manufacturing','transfers','traffic','flow','maintenance','amhs_faults')
    seen=set()
    for step in range(300):
        if owners.scheduler.next_time_us is None:break
        owners.scheduler.settle(owners.scheduler.next_time_us)
        if step%15==0:
            state={name:getattr(owners,name) for name in roots}
            digest=projection_digest(state)
            assert digest==domain_digest(state_projection(state))
            assert digest==runtime_digest(owners)
            seen.add(digest)
    assert len(seen)>3


def test_projection_digest_matches_on_mixed_values():
    from collections import deque, OrderedDict
    from decimal import Decimal
    from fractions import Fraction
    from random import Random
    from fab_sim.probes import domain_digest, projection_digest, state_projection
    value={'a':[1,'x',None,True,2.5,Decimal('1.25'),Fraction(1,3),'é"\\',deque([3,4])],
        'b':{(1,'k'):'t',(0,'z'):[1],5:{'q':frozenset({('a',1),('b',2)})}},'c':{3,1,2},
        'd':OrderedDict([('z',1),('a',lambda:0)]),'r':Random(7),'t':(1,(2,3))}
    assert projection_digest(value)==domain_digest(state_projection(value))
