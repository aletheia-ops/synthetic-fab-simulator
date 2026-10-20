from decimal import Decimal
from fractions import Fraction
import pytest
from fab_sim.scheduler import Scheduler


def handlers():
    from fab_sim.service import SerializedHandler
    s=Scheduler();claims={};events=[]
    dirty=lambda:s.mark_dirty('manufacturing')
    service=SerializedHandler(s,'tool:service',claims,dirty,lambda k,p:events.append((k,p)))
    exchange=SerializedHandler(s,'tool:exchange',claims,dirty,lambda k,p:events.append((k,p)))
    s.register_pass('manufacturing',lambda:(service.dispatch(),exchange.dispatch()))
    return s,service,exchange,claims,events


def test_two_handlers_overlap_disjoint_carriers_but_serialize_each_handler():
    s,a,b,claims,events=handlers();done=[]
    a.request('load','load',['c1'],lambda:10,lambda:done.append(('load',s.now_us)))
    a.request('unload','unload',['c1'],lambda:2,lambda:done.append(('unload',s.now_us)))
    b.request('exchange','exchange',['c2'],lambda:4,lambda:done.append(('exchange',s.now_us)))
    s.settle(0);assert len(claims)==2
    while s.next_time_us is not None:s.settle(s.next_time_us)
    assert done==[('exchange',4),('load',10),('unload',12)] and claims=={}


def test_cross_handler_carrier_exclusion_and_no_speculative_duration_draws():
    s,a,b,claims,events=handlers();draws=[]
    a.request('load','load',['c'],lambda:draws.append('load') or 10,lambda:None)
    b.request('move','exchange',['c'],lambda:draws.append('exchange') or 1,lambda:None)
    assert draws==[]
    s.settle(0);assert draws==['load'] and b.active is None
    s.settle(10);assert draws==['load','exchange']
    s.settle(11);assert claims=={}


def test_oldest_feasible_request_preserves_age_when_blocked_and_stable_id_ties():
    s,a,b,claims,events=handlers();ready={'old':False};order=[]
    a.request('old','unload',[],lambda:1,lambda:order.append('old'),eligible=lambda:ready['old'])
    a.request('z','load',[],lambda:1,lambda:order.append('z'))
    a.request('a','load',[],lambda:1,lambda:order.append('a'))
    s.settle(0);s.settle(1);ready['old']=True;s.mark_dirty('manufacturing');s.settle(1)
    s.settle(2);s.settle(3)
    assert order==['a','z','old']
    assert a.requests['old']['queued_at_us']==0


def test_overlapping_faults_pause_service_without_resampling():
    s,a,b,claims,events=handlers();draws=[];done=[]
    a.request('work','load',['c'],lambda:draws.append(1) or 10,lambda:done.append(s.now_us))
    s.settle(0);s.settle(3);a.pause('x');a.pause('y')
    assert claims and a.active['timer'].remaining_us==7
    s.settle(5);a.resume('x');s.settle(8);a.resume('y');s.settle(15)
    assert done==[15] and draws==[1] and not claims


def test_processing_draw_multiplicity_and_shared_batch_owner():
    from fab_sim.service import processing_duration_us
    from fab_sim.rng import Streams,sample_seconds
    from fab_sim.timebase import to_ticks
    definition={'distribution':'uniform','mean_seconds':'1','offset_seconds':'0.2'}
    streams=Streams(7);reference=Streams(7)
    value=processing_duration_us({'processing':definition,'processing_unit':'batch','cascade_interval_us':None},['z','a'],[25,25],streams)
    assert value==to_ticks(sample_seconds(definition,reference.get('processing','a')))
    assert streams.get('processing','a').getstate()==reference.get('processing','a').getstate()
    assert streams.get('processing','z').getstate()==reference.get('processing','z').getstate()
    serial={'processing':definition,'processing_unit':'wafer','cascade_interval_us':None}
    expected=sum((Fraction(sample_seconds(definition,reference.get('processing','z'))) for i in range(25)),Fraction(0))
    assert processing_duration_us(serial,['z'],[25],streams)==to_ticks(expected)
    assert streams.get('processing','z').getstate()==reference.get('processing','z').getstate()


def test_constant_serial_wafer_duration_sums_before_rounding_without_rng():
    from fab_sim.service import processing_duration_us
    from fab_sim.rng import Streams
    streams=Streams(1);before=streams.get('processing','lot').getstate()
    step={'processing':{'distribution':'constant','mean_seconds':'0.0000004','offset_seconds':'0'},'processing_unit':'wafer','cascade_interval_us':None}
    assert processing_duration_us(step,['lot'],[25],streams)==10
    assert streams.get('processing','lot').getstate()==before


def test_setup_sampling_shared_owner_constant_transition_and_no_draw_ranking():
    from fab_sim.manufacturing import SetupState
    from fab_sim.service import setup_duration_us
    from fab_sim.rng import Streams,sample_seconds
    from fab_sim.timebase import to_ticks
    streams=Streams(4);reference=Streams(4)
    setup=SetupState('A',[])
    definition={'distribution':'uniform','mean_seconds':'2','offset_seconds':'1'}
    decision=setup.decision({'setup':{'id':'B','when':'need','fallback':definition}})
    assert streams.get('processing','a').getstate()==reference.get('processing','a').getstate()
    assert setup_duration_us(decision,['z','a'],streams)==to_ticks(sample_seconds(definition,reference.get('processing','a')))
    state=streams.get('processing','a').getstate()
    exact=SetupState('A',[{'current':'A','new':'B','duration_us':10,'minimum_runs':7}])
    assert setup_duration_us(exact.decision({'setup':{'id':'B','when':'need','fallback':None}}),['a'],streams)==10
    assert streams.get('processing','a').getstate()==state


def test_serialized_setup_waits_for_slot_drain_and_pauses_interval_on_start():
    from fab_sim.service import Cascade,SerializedHandler
    s=Scheduler();claims={};dirty=lambda:s.mark_dirty('manufacturing')
    a=SerializedHandler(s,'service',claims,dirty,lambda k,p:None)
    b=SerializedHandler(s,'exchange',claims,dirty,lambda k,p:None)
    pipe=Cascade(s,'pipe','lot','A',lambda:s.mark_dirty('manufacturing'),lambda k,p:None)
    s.register_pass('manufacturing',lambda:(a.dispatch(),b.dispatch(),pipe.dispatch()))
    pipe.enqueue_lot('old',1,10,lambda:3,lambda u:None,'A')
    pipe.enqueue_lot('new',1,10,lambda:3,lambda u:None,'B')
    s.settle(0)
    a.request('setup','setup',[],lambda:5,pipe.end_setup,
              eligible=lambda:not any(pipe.slots),on_start=lambda:pipe.begin_setup('B'))
    # Carrier exchange is a distinct lane and can continue during setup.
    b.request('exchange','exchange',['c'],lambda:4,lambda:None)
    s.settle(0);assert a.active is None
    s.settle(3);assert pipe.setup_active and a.active['kind']=='setup'
    s.settle(4);s.settle(8);assert not pipe.setup_active
    s.settle(15);s.settle(18);s.settle(25)
    assert pipe.units['new:lot:0']['started_at_us']==15
    assert pipe.units['new:lot:0']['completed_at_us']==18
