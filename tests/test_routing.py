from fractions import Fraction
import pytest


def ring():
    return {'rails': [
        {'id': 'a', 'from_node': 'n1', 'to_node': 'n2', 'length_um': 1000,
         'speed_multiplier': '1', 'curve': False},
        {'id': 'b', 'from_node': 'n2', 'to_node': 'n1', 'length_um': 2000,
         'speed_multiplier': '0.5', 'curve': True}],
        'vehicle_types': [{'id': 'v', 'speed_um_s': '3000'}],
        'policy': {'curve_cap_um_s': '1000'}}


def route(start, end, model=None):
    from fab_sim.routing import free_flow_path
    return free_flow_path(model or ring(), {'rail_id': 'a', 'offset_um': start},
                          {'rail_id': 'a', 'offset_um': end}, 'v')


def test_partial_forward_rail_cost_has_no_handling():
    assert route(100, 400)['seconds'] == Fraction(1, 10)
    assert route(100, 400)['sublinks'] == [
        {'id': 'a@100:400', 'rail_id': 'a', 'start_um': 100, 'end_um': 400}]


def test_destination_behind_requires_directed_return():
    result = route(800, 100)
    assert [s['rail_id'] for s in result['sublinks']] == ['a', 'b', 'a']
    assert result['seconds'] == Fraction(41, 10)


def test_same_pose_has_zero_travel():
    assert route(100, 100) == {'sublinks': [], 'seconds': Fraction(0)}


def test_route_tie_uses_entire_sublink_sequence():
    model = ring()
    model['rails'] += [dict(model['rails'][1], id='c')]
    assert [s['rail_id'] for s in route(800, 100, model)['sublinks']] == ['a', 'b', 'a']


def test_unreachable_and_invalid_offset_reject():
    model = ring(); model['rails'] = model['rails'][:1]
    with pytest.raises(ValueError, match='unreachable'): route(800, 100, model)
    with pytest.raises(ValueError, match='offset'): route(-1, 100)


def test_chord_quantizes_normalized_coordinates_once():
    from fab_sim.geometry import chord_length, spatial_um
    assert spatial_um('0.0005') == 0
    assert spatial_um('0.0015') == 2
    assert spatial_um('-0.0015') == -2
    assert chord_length((0, 0), (3, 4)) == 5
    assert chord_length((0, 0), (1, 1)) == 1
    with pytest.raises(ValueError, match='zero'): chord_length((0, 0), (0, 0))


def test_short_link_sweep_keeps_full_intermediate_rail():
    from fab_sim.geometry import swept_intervals
    model = ring(); model['rails'][1]['length_um'] = 100
    path = route(800, 100, model)['sublinks']
    claims = swept_intervals(model, path, 50)
    assert claims == {'a': [(0, 150), (750, 1000)], 'b': [(0, 100)]}


def test_speed_sections_retain_original_rail_and_exact_partial_cost():
    model=ring(); model['rails'][0]['speed_sections']=[
        {'start_um':0,'end_um':200,'curve':True},
        {'start_um':200,'end_um':800,'curve':False},
        {'start_um':800,'end_um':1000,'curve':True}]
    out=route(100,900,model)
    assert out['seconds']==Fraction(2,5)
    assert [(s['rail_id'],s['start_um'],s['end_um']) for s in out['sublinks']]==[
        ('a',100,200),('a',200,800),('a',800,900)]
    from fab_sim.geometry import swept_intervals
    assert swept_intervals(model,out['sublinks'],50)=={'a':[(50,950)]}


def test_bulk_holding_costs_match_exact_directed_paths_with_speed_sections_and_wrap():
    from fab_sim.routing import free_flow_costs,free_flow_path
    m=ring();m['rails'][0]['speed_sections']=[{'start_um':0,'end_um':500,'curve':False},
                                            {'start_um':500,'end_um':1000,'curve':True}]
    start={'rail_id':'a','offset_um':700}
    poses={str(i):{'rail_id':rid,'offset_um':offset} for i,(rid,offset) in enumerate(
        [('a',700),('a',100),('a',900),('b',300),('b',900)])}
    costs=free_flow_costs(m,start,poses,'v')
    assert costs=={pid:free_flow_path(m,start,pose,'v')['seconds'] for pid,pose in poses.items()}


def test_repeated_static_speed_does_not_reparse_rationals(monkeypatch):
    import fab_sim.routing as routing
    calls=[]
    def counted(value):
        calls.append(value)
        return Fraction(value)
    monkeypatch.setattr(routing,'Fraction',counted)
    rail={'curve':True,'absolute_cap_um_s':'23456','speed_multiplier':'1/7'}
    vehicle={'speed_um_s':'31337'};policy={'curve_cap_um_s':'12345'}
    assert all(routing.effective_speed(rail,vehicle,policy)==Fraction(12345,7) for _ in range(100))
    assert len(calls)<=4


def test_speed_reuse_tracks_every_mutable_input_and_still_rejects_invalid_values():
    from fab_sim.routing import effective_speed
    rail={'curve':False,'speed_multiplier':'1'};vehicle={'speed_um_s':'3000'};policy={'curve_cap_um_s':'1000'}
    assert effective_speed(rail,vehicle,policy)==3000
    rail['curve']=True
    assert effective_speed(rail,vehicle,policy)==1000
    policy['curve_cap_um_s']='900'
    assert effective_speed(rail,vehicle,policy)==900
    rail['absolute_cap_um_s']='800'
    assert effective_speed(rail,vehicle,policy)==800
    rail['speed_multiplier']='1/2'
    assert effective_speed(rail,vehicle,policy)==400
    vehicle['speed_um_s']='600'
    assert effective_speed(rail,vehicle,policy)==300
    rail['speed_multiplier']='0'
    with pytest.raises(ValueError,match='invalid rail speed'):effective_speed(rail,vehicle,policy)
    rail['speed_multiplier']='1';vehicle['speed_um_s']='-1'
    with pytest.raises(ValueError,match='invalid rail speed'):effective_speed(rail,vehicle,policy)
    # Straight rails never require a curve cap, even after a curved cache entry.
    rail['curve']=False;vehicle['speed_um_s']='3000'
    assert effective_speed(rail,vehicle,{})==800
