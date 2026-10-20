from copy import deepcopy
from fractions import Fraction
import pytest
from test_routing import ring
from test_transfers import factory,place


@pytest.mark.parametrize('sections',[False,True])
def test_many_vehicle_costs_to_pickup_equal_exact_directed_paths(sections):
    from fab_sim.routing import free_flow_costs_to,free_flow_path
    m=ring()
    if sections:m['rails'][0]['speed_sections']=[{'start_um':0,'end_um':500,'curve':False},
        {'start_um':500,'end_um':1000,'curve':True}]
    origins={str(i):{'rail_id':rid,'offset_um':offset} for i,(rid,offset) in enumerate(
        [('a',0),('a',100),('a',700),('a',1000),('b',0),('b',1500),('b',2000)])}
    for end in origins.values():
        assert free_flow_costs_to(m,origins,end,'v')=={
            vid:free_flow_path(m,start,end,'v')['seconds'] for vid,start in origins.items()}


def test_reverse_cost_search_omits_unreachable_origins():
    from fab_sim.routing import free_flow_costs_to
    m=ring();m['rails']=m['rails'][:1]
    origins={'forward':{'rail_id':'a','offset_um':0},'behind':{'rail_id':'a','offset_um':800}}
    assert free_flow_costs_to(m,origins,{'rail_id':'a','offset_um':100},'v')=={'forward':Fraction(1,30)}


@pytest.mark.parametrize('offset',[-1,1001,True])
def test_reverse_cost_search_rejects_invalid_pose_offsets(offset):
    from fab_sim.routing import free_flow_costs_to
    with pytest.raises(ValueError,match='offset'):
        free_flow_costs_to(ring(),{'bad':{'rail_id':'a','offset_um':offset}},
            {'rail_id':'a','offset_um':100},'v')


def test_assignment_constructs_only_selected_vehicle_path_and_preserves_id_tie(monkeypatch):
    t,m,s,c,lots,vehicles,events=factory()
    for i in range(50):
        vid=f'v{i:03}';sid='vehicle:'+vid
        vehicles[vid]={**vehicles['v1'],'id':vid,'carrier_slot_id':sid}
        c.slots[sid]={**c.slots['vehicle:v1'],'id':sid}
    visit=m.enter_visit('low','1');place(c,'clow','entry')
    original=t._path;vehicle_paths=[]
    def counted(start,end):
        if start.get('carrier_slot_id'):vehicle_paths.append(start['id'])
        return original(start,end)
    monkeypatch.setattr(t,'_path',counted)
    did=t.request('clow',visit,('port',));s.settle(0)
    assert t.demands[did]['vehicle_id']=='v000'
    assert vehicle_paths==['v000']
    assert t.movements[t.vehicles['v000']['movement_id']]['path']==original(vehicles['v000'],t._pose('entry'))
