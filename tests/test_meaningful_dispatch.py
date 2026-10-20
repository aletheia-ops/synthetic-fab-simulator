"""Dispatch passes with nothing to do emit nothing; releases and custom handlers always run."""
import sys
from contextlib import contextmanager
import pytest
from fab_sim.service import SerializedHandler
from test_manufacturing_facade import factory, place, drain
from test_transfers import factory as transfer_factory


@contextmanager
def calls_to(method):
    calls=[]
    def observe(frame,event,arg):
        if event=='call' and frame.f_code is method.__code__:
            calls.append(frame.f_locals.get('self'))
    prior=sys.getprofile();sys.setprofile(observe)
    try:yield calls
    finally:sys.setprofile(prior)


def test_empty_manufacturing_lanes_do_not_dispatch_but_new_loads_do():
    m,s,c,lots,events=factory()
    with calls_to(SerializedHandler.dispatch) as calls:
        for _ in range(10):m.dirty();s.settle(0)
    assert calls==[]
    vid=m.enter_visit('low','1');place(c,'clow','rack');m.delivered('clow','rack')
    drain(s)
    assert m.rules.visits[vid]['status']=='unloaded'
    assert lots['low']['route_complete_at_us']==15


@pytest.mark.parametrize('blocked',['active','fault'])
def test_native_blocked_lanes_are_skipped_until_completion_or_repair(blocked):
    m,s,c,lots,events=factory();handler=m.handlers['t'];done=[]
    handler.request('first','load',[],lambda:10,lambda:done.append('first'))
    if blocked=='fault':handler.pause('fault')
    else:s.settle(0)
    with calls_to(SerializedHandler.dispatch) as calls:
        for _ in range(5):m.dirty();s.settle(0)
    assert calls==[] and done==[]
    if blocked=='fault':handler.resume('fault')
    drain(s)
    assert done==['first']


def test_custom_empty_lane_dispatch_is_never_suppressed():
    m,s,c,lots,events=factory();seen=[]
    m.handlers['t'].dispatch=lambda:seen.append(s.now_us) or False
    m.dirty();s.settle(0)
    assert seen==[0]


def test_custom_pending_order_is_never_suppressed():
    m,s,c,lots,events=factory();h=m.handlers['t'];done=[]
    h.request('work','load',[],lambda:1,lambda:done.append('work'))
    # An unknown ordering hook can supply work despite an empty native index.
    record=h.requests['work'];h._pending.clear();h.pending_requests=lambda:(record,)
    drain(s)
    assert done==['work']


def test_empty_transfer_assignment_is_skipped_and_new_requests_still_assign():
    t,m,s,c,lots,vehicles,events=transfer_factory()
    with calls_to(type(t).assign) as calls:
        for _ in range(10):t.dirty();s.settle(0)
    assert calls==[]
    vid=m.enter_visit('low','1');place(c,'clow','entry')
    did=t.request('clow',vid,('port',));s.settle(0)
    assert t.demands[did]['status']=='assigned'


def test_custom_assignment_without_pending_demands_is_preserved():
    t,m,s,c,lots,vehicles,events=transfer_factory();seen=[]
    t.assign=lambda:seen.append(s.now_us) or False
    t.dirty();s.settle(0)
    assert seen==[0]


def test_pose_only_wakeup_does_not_recheck_fault_blocked_handling():
    from test_transport_resources import blocked_pickup
    from test_amhs_integration import until
    from fab_sim.transfers import Transfers
    traffic,t,m,s,c,lots,vehicles,events,did=blocked_pickup()
    with calls_to(Transfers._handling_blocked) as calls:
        for _ in range(5):t.traffic_changed('v1');s.settle(s.now_us)
    assert calls==[] and t.demands[did]['status']=='pickup_wait'
    traffic.recover('entry','blocked',target_type='port');until(s,s.now_us+10)
    assert c.locations['clow']=='vehicle:v1'


def test_pose_wakeup_still_reconsiders_shared_vehicle_assignment():
    t,m,s,c,lots,vehicles,events=transfer_factory()
    vehicles['v1']['available']=False
    vid=m.enter_visit('low','1');place(c,'clow','entry')
    did=t.request('clow',vid,('port',));s.settle(0)
    assert t.demands[did]['status']=='pending'
    vehicles['v1']['available']=True;t.traffic_changed('v1');s.settle(0)
    assert t.demands[did]['status']=='assigned'


def test_native_empty_tool_loop_has_no_candidates_but_rack_arrival_wakes_it():
    from fab_sim.probes import Probes
    m,s,c,lots,events=factory();m.probes=Probes()
    for _ in range(10):m.dirty();s.settle(0)
    assert m.probes.work_counts['manufacturing_tools_considered']==0
    vid=m.enter_visit('low','1');place(c,'clow','rack');m.delivered('clow','rack');drain(s)
    assert m.rules.visits[vid]['status']=='unloaded'
    assert m.probes.work_counts['manufacturing_tools_considered']>0


def test_processing_only_tool_loop_waits_for_its_completion_event():
    from fab_sim.probes import Probes
    m,s,c,lots,events=factory();vid=m.enter_visit('low','1')
    place(c,'clow','rack');m.delivered('clow','rack');s.settle(0);s.settle(2)
    m.probes=Probes()
    for _ in range(10):m.dirty();s.settle(2)
    assert m.probes.work_counts['manufacturing_tools_considered']==0
    drain(s)
    assert m.rules.visits[vid]['status']=='unloaded' and lots['low']['route_complete_at_us']==15


def test_unknown_rack_candidate_hook_keeps_full_tool_evaluation():
    m,s,c,lots,events=factory();seen=[];original=m.rack_visits
    m.rack_visits=lambda tid:seen.append(tid) or original(tid)
    m.dirty();s.settle(0)
    assert seen==['t']


@pytest.mark.parametrize('faulted',[False,True])
def test_pending_maintenance_keeps_campaign_drain_check_even_on_empty_tools(faulted):
    from types import SimpleNamespace
    m,s,c,lots,events=factory();seen=[]
    m.tools['t']['pm_pending']=True
    if faulted:m.causes['t'].add('fault')
    m.campaigns=SimpleNamespace(drained=lambda tid:seen.append(tid) or True)
    m.dirty();s.settle(0)
    assert seen==['t']
