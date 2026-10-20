from fractions import Fraction
from test_stocker_flow import flow_factory
from fab_sim.probes import domain_digest,state_projection


def test_delivery_ranking_uses_costs_without_constructing_paths(monkeypatch):
    flow,traffic,t,m,s,c,lots,v,e=flow_factory(1)
    c.locations['carrier']='entry:0'
    def no_path(*args):raise AssertionError('delivery ranking constructed a full route')
    monkeypatch.setattr(t,'_path',no_path)
    assert flow.delivery_cost('carrier','t')==Fraction(5001,50000)
    c.slots['port']['reservation']='busy'
    assert flow.delivery_cost('carrier','t')==Fraction(5001,50000)


def test_delivery_rows_preserve_directed_rack_output_port_roles_and_same_rack_zero():
    flow,traffic,t,m,s,c,lots,v,e=flow_factory(1)
    c.locations['carrier']='rack'
    assert flow.delivery_cost('carrier','t')==0
    c.slots['source:rack']={'kind':'rack','equipment_id':'source'}
    c.locations['carrier']='source:rack'
    t.docks['source:out']={**t.docks['port'],'id':'source:out','equipment_id':'source','role':'OUT','offset_um':900}
    t.docks['source:in']={**t.docks['port'],'id':'source:in','equipment_id':'source','role':'IN','offset_um':350}
    expected=t._path(t._pose('source:out'),t._pose('port'))['seconds']+Fraction(20,1000000)
    assert flow.delivery_cost('carrier','t')==expected
    assert flow.delivery_cost('carrier','t')>4


def test_delivery_row_reuses_node_search_for_multiple_tools_and_offsets():
    flow,traffic,t,m,s,c,lots,v,e=flow_factory(1)
    m.tools['other']={**m.tools['t'],'id':'other','rack_slots':[]}
    t.docks['other:in']={**t.docks['port'],'id':'other:in','equipment_id':'other','offset_um':700}
    c.locations['carrier']='entry:0'
    assert flow.delivery_cost('carrier','t')==Fraction(5001,50000)
    assert flow.delivery_cost('carrier','other')==Fraction(10001,50000)
    assert flow._delivery_router.cache_info()['searches']==1
    c.locations['carrier']='buffer'
    assert flow.delivery_cost('carrier','other')==Fraction(1,30)+Fraction(1,50000)
    assert flow._delivery_router.cache_info()['searches']==1
    assert flow._delivery_router.cache_info()['projected_destinations']==3


def test_derived_delivery_rows_leave_physical_digest_unchanged():
    flow,traffic,t,m,s,c,lots,v,e=flow_factory(1);c.locations['carrier']='entry:0'
    state={'flow':flow,'custody':c};before=domain_digest(state_projection(state))
    flow.delivery_cost('carrier','t')
    assert domain_digest(state_projection(state))==before
    c.slots['port']['reservation']='physical'
    assert domain_digest(state_projection(state))!=before
