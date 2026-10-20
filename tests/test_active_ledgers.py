"""Live work must not enumerate terminal evidence ledgers."""
import pytest
from test_transfers import factory as transfer_factory, place
from test_manufacturing_facade import factory as manufacturing_factory, drain
from test_traffic import factory as traffic_factory


class Evidence(dict):
    """Allow historical lookup, reject whole-ledger traversal in hot paths."""
    def values(self):
        raise AssertionError('historical evidence enumerated')

    def items(self):
        raise AssertionError('historical evidence enumerated')


def test_transfer_assignment_and_handling_use_active_ownership():
    t,m,s,c,lots,vehicles,events=transfer_factory()
    vid=m.enter_visit('low','1');place(c,'clow','entry')
    did=t.request('clow',vid,('port',))
    t.demands=Evidence(t.demands)
    assert t.assign()
    assert t.demands[did]['status']=='assigned'
    vehicles['v1'].update(t.demands[did]['pickup_pose'])
    t.arrived(did,'pickup')
    assert t.dispatch()
    assert t.demands[did]['status']=='pickup_handling'
    assert t.active['clow']==did


def test_traffic_bridge_uses_vehicle_movement_ownership():
    from types import SimpleNamespace
    t,s,vehicles,events=traffic_factory()
    # A transfer path is requested in the same way as the real bridge does.
    from fab_sim.routing import free_flow_path
    path=free_flow_path(t.model,vehicles['v1'],t.docks['dock'],'type')
    movement={'id':'next:pickup','demand_id':'next','vehicle_id':'v1',
        'phase':'pickup','path':path,'status':'requested','cancelled':False,'queued_at_us':0}
    vehicles['v1']['movement_id']=movement['id']
    t.transfers=SimpleNamespace(
        movements=Evidence({'past':{**movement,'id':'past','status':'complete'},movement['id']:movement}),
        demands={'next':{'origin_id':'dock','destination_id':'dock'}},dock_id=lambda sid:sid,
        movement_started=lambda mid:None,arrived=lambda did,phase:None,
        cancelled_arrived=lambda mid:None)
    assert t._bridge()
    assert movement['id'] in t.requests
    assert not t._bridge()
    assert t.transfers.movements['past']['status']=='complete'


def test_manufacturing_dispatch_keeps_completed_visits_only_as_evidence():
    m,s,c,lots,events=manufacturing_factory()
    first=m.enter_visit('low','1');place(c,'clow','rack')
    m.delivered('clow','rack');drain(s)
    assert m.rules.visits[first]['status']=='unloaded'

    # Free the rack after route completion, then dispatch a different LOT.
    assert c.reserve_many([],'remove',origins={'rack':'clow'})
    c.remove('clow','rack','remove')
    second=m.enter_visit('hot','1');place(c,'chot','rack')
    m.delivered('chot','rack')
    m.rules.visits=Evidence(m.rules.visits)
    assert m.dispatch()
    assert m.rules.visits[second].get('execution_id')
    assert m.rules.visits[first]['status']=='unloaded'


def test_execution_count_does_not_scan_terminal_executions():
    m,s,c,lots,events=manufacturing_factory()
    first=m.enter_visit('low','1');place(c,'clow','rack')
    m.delivered('clow','rack');drain(s)
    assert m.active_execution_count()==0
    m.executions=Evidence(m.executions)
    assert c.reserve_many([],'remove',origins={'rack':'clow'})
    c.remove('clow','rack','remove')
    second=m.enter_visit('hot','1');place(c,'chot','rack');m.delivered('chot','rack')
    m.dispatch()
    assert m.active_execution_count()==1
    drain(s)
    assert m.active_execution_count()==0
    assert m.executions['execution:'+first]['status']=='unloaded'
    assert m.executions['execution:'+second]['status']=='unloaded'


@pytest.mark.parametrize('mode',['serial','batch','cascade_lot','cascade_wafer'])
def test_derived_state_matches_history_at_emitted_milestones(mode):
    if mode=='batch':
        from test_batch_service import factory,deliver
        m,s,c,lots,events=factory()
    elif mode.startswith('cascade'):
        from test_cascade_service import factory
        m,s,c,lots,events=factory('lot' if mode=='cascade_lot' else 'wafer')
    else:
        m,s,c,lots,events=manufacturing_factory()
        vid=m.enter_visit('low','1');place(c,'clow','rack');m.delivered('clow','rack')
    checked=[]
    def check():
        expected=[v for v in m.rules.visits.values() if v['status']!='unloaded']
        assert m.rules.current_visits()==tuple(expected)
        for group in m.groups:
            assert m.rules.current_visits(group)==tuple(v for v in expected if
                m._step(v)['tool_group_id']==group)
        assert m.active_execution_count()==sum(e['status']!='unloaded' for e in m.executions.values())
    # Keep the real event sinks; validate intermediate, not just settled state.
    for owner in (m,m.rules,m.batches,*m.handlers.values(),*m.pipelines.values()):
        sink=getattr(owner,'emit',None)
        if not callable(sink):continue
        def emit(kind,payload,sink=sink):
            check();checked.append(kind);sink(kind,payload)
        owner.emit=emit
    check()
    if mode=='batch':
        s.settle(0);assembly=next(iter(m.batches.assemblies.values()))
        for i,member in enumerate(assembly['members']):deliver(m,c,assembly,member,i)
    drain(s);check()
    assert 'processing_completed' in checked and 'unload_completed' in checked
    assert m.active_execution_count()==0
