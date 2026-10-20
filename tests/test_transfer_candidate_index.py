from test_resource_index import records
from fab_sim.resource_index import ResourceIndex

def test_indexed_candidates_equal_reference_order():
    slots,tools,model=records()
    model['docks'].extend([
        {'id':'b2','kind':'track_buffer','rail_id':'r','protected':False},
        {'id':'b1','kind':'track_buffer','rail_id':'r','protected':True}])
    index=ResourceIndex(slots,tools,model,protected_slot_ids=('b1',))
    assert index.input_ports('t')==('in',)
    assert index.output_ports('t')==('out',)
    assert index.buffer_candidates('t',protected=False)==('b2',)
    assert index.buffer_candidates('t',protected=True)==('b1',)


def test_live_loads_reuse_and_invalidate_without_retaining_history():
    from test_stocker_flow import flow_factory
    flow,traffic,t,m,s,c,lots,*_=flow_factory(3)
    s.settle(0)
    r=next(iter(t.demands.values()))
    first=t.destination_loads(r,m.tools)
    original=m.current_lot_items
    m.current_lot_items=lambda: (_ for _ in ()).throw(AssertionError('unchanged load frontier rescanned'))
    assert t.destination_loads(r,m.tools)==first
    m.current_lot_items=original
    t.rebuild_derived_loads()
    assert t.destination_loads(r,m.tools)==first
    live={lid for lid,lot in original()}
    assert set().union(set(),*t._resident_lots_by_tool.values(),*t._incoming_lots_by_tool.values())<=live

def test_available_vehicle_ids_rechecks_same_pass_state():
    from test_stocker_flow import flow_factory
    _,traffic,t,m,s,c,lots,*_=flow_factory(1)
    ids=t.available_vehicle_ids()
    assert ids
    vid=ids[0];t.vehicles[vid]['assignment_id']='claimed'
    assert vid not in t.available_vehicle_ids()
    t.vehicles[vid]['assignment_id']=None
    assert vid in t.available_vehicle_ids()


def test_direct_assignment_requests_cost_only_candidates(monkeypatch):
    from test_stocker_flow import flow_factory
    flow,traffic,t,m,s,c,lots,*_=flow_factory(1)
    calls=[];original=t._choices
    def choices(*args,**kwargs):
        calls.append(kwargs.get('with_paths',True))
        return original(*args,**kwargs)
    monkeypatch.setattr(t,'_choices',choices)
    s.settle(0)
    assert calls and not any(calls)


def test_overflow_uses_static_buffer_index_without_scanning_all_docks():
    from test_transfers import factory
    from test_manufacturing_facade import place
    t,m,s,c,lots,vehicles,events=factory()
    vid=m.enter_visit('low','1');place(c,'clow','entry');c.reserve('rack','busy')
    did=t.request('clow',vid,('port',));r=t.demands[did]
    expected=t._choices(r,'entry')
    assert expected and expected[0][-1] is True
    t.resource_index=ResourceIndex(c.slots,m.tools,t.model)
    class NoScan(dict):
        def items(self):raise AssertionError('all docks enumerated')
    t.docks=NoScan(t.docks)
    assert t._choices(r,'entry')==expected


def test_per_tool_loads_equal_a_full_rebuild():
    from test_stocker_flow import flow_factory
    flow,traffic,t,m,s,c,lots,*_=flow_factory(6)
    checked=0
    s.settle(0)
    for _ in range(400):
        if s.next_time_us is None:break
        s.settle(s.next_time_us)
        for r in list(t.demands.values()):
            visit=m.rules.visits.get(r['visit_id'])
            if not visit or m._step(visit).get('batch_min'):continue
            t._load_dirty=True
            lazy=t.destination_loads(r,m.tools)
            t.rebuild_derived_loads()
            full={tid:len(t._resident_lots_by_tool.get(tid,set())|t._incoming_lots_by_tool.get(tid,set())) for tid in m.tools}
            assert lazy==full
            checked+=1
    assert checked>50
