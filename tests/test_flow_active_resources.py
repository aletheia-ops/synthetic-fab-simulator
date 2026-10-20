"""Live flow dispatch preserves physical behavior without scanning exit history."""
from copy import deepcopy
import pytest
from test_stocker_flow import flow_factory


class LookupOnlyHistory(dict):
    def items(self):raise AssertionError('finished history enumerated')
    def values(self):raise AssertionError('finished history enumerated')
    def __iter__(self):raise AssertionError('finished history enumerated')


def drain(flow,s,events,count):
    for _ in range(3000):
        if sum(k=='lot_exited' for k,p in events)==count:return
        assert s.next_time_us is not None
        s.settle(s.next_time_us)
    pytest.fail('live flow failed to drain')


def test_exited_history_is_not_enumerated_by_flow_dispatch():
    flow,traffic,t,m,s,c,lots,v,e=flow_factory()
    history=LookupOnlyHistory(lots)
    history['history']={'carrier_id':'old','exited_at_us':0,'status':'exited'}
    flow.lots=m.lots=m.rules.lots=history
    s.settle(0);s.settle(1);s.settle(2)
    assert len(flow.pending)==1
    drain(flow,s,e,3)
    assert flow.current_lots()==() and history['history']['status']=='exited'
    assert history['a:2']['admission_delay_us']>0
    assert [p['lot_id'] for k,p in e if k=='lot_admitted']==['a:0','a:1','a:2']
    assert [history[f'a:{i}']['due_at_us'] for i in range(3)]==[100,101,102]


def test_same_tick_visit_and_exit_wake_flow():
    from test_route_rules import step
    runs=[]
    for narrow in (False,True):
        flow,traffic,t,m,s,c,lots,v,e=flow_factory(1,[step(1),step(2)])
        if narrow:
            from fab_sim.resource_index import ResourceIndex
            m.resource_index=t.resource_index=ResourceIndex(c.slots,m.tools,t.model)
        s.settle(0);drain(flow,s,e,1)
        runs.append((deepcopy(e),lots['a:0']['exited_at_us']))
    assert runs[0]==runs[1]


def test_entry_freed_wakes_pending_admission():
    flow,traffic,t,m,s,c,lots,v,e=flow_factory()
    s.settle(0);s.settle(1);s.settle(2)
    assert len(flow.pending)==1
    drain(flow,s,e,3)
    assert not flow.pending and lots['a:2']['admitted_at_us']>2


def test_reserved_carrier_wakes_on_release():
    from fab_sim.resource_index import ResourceIndex
    flow,traffic,t,m,s,c,lots,v,e=flow_factory(1)
    m.resource_index=t.resource_index=ResourceIndex(c.slots,m.tools,t.model)
    v['v1']['available']=False;s.settle(0)
    carrier=lots['a:0']['carrier_id'];origin=c.locations[carrier]
    assert c.reserve_many([],'inspection',origins={origin:carrier})
    flow._dirty_lots.take()
    c.release(origin,'inspection')
    assert flow._dirty_lots.take()==frozenset({'a:0'})
    v['v1']['available']=True;t.dirty();s.settle(0);drain(flow,s,e,1)


def test_unrelated_empty_slot_skips_lot_consideration():
    from fab_sim.probes import Probes
    from fab_sim.resource_index import ResourceIndex
    flow,traffic,t,m,s,c,lots,v,e=flow_factory(1)
    m.resource_index=t.resource_index=ResourceIndex(c.slots,m.tools,t.model)
    flow.probes=Probes();s.settle(0)
    flow._dirty_lots.take();flow.probes.work_counts.clear()
    c.reserve('exit:0','unrelated');flow.dispatch()
    assert flow.probes.work_counts.get('flow_lots_considered',0)==0


def test_manufacturing_publishes_route_completion_and_carrier_identity():
    flow,traffic,t,m,s,c,lots,v,e=flow_factory(1)
    changes=[];m.subscribe_resources(changes.append)
    s.settle(0);drain(flow,s,e,1)
    assert any('a:0:carrier' in change.carrier_ids for change in changes)
    assert lots['a:0']['route_complete_at_us'] is not None


@pytest.mark.parametrize('enabled',[False,True])
def test_work_counts_are_observational_and_validate_inputs(enabled):
    from fab_sim.probes import Probes
    p=Probes(enabled=enabled);p.count_work('flow_lots_considered',3)
    assert dict(p.work_counts)==({'flow_lots_considered':3} if enabled else {})
    for kind,amount in [('',1),('work',True),('work',0),('work',-1),('work',1.5)]:
        with pytest.raises(ValueError):p.count_work(kind,amount)
