"""Exact travel reuse must not hide changed models or malformed partitions."""
from fractions import Fraction
import pytest
from test_routing import ring


def test_repeated_travel_does_not_repeat_rational_distance_arithmetic(monkeypatch):
    import fab_sim.routing as routing
    rail=ring()['rails'][0];rail['speed_multiplier']='17/19'
    vehicle={'speed_um_s':'31339'};calls=[]
    def counted(*args):
        calls.append(args)
        return Fraction(*args)
    monkeypatch.setattr(routing,'Fraction',counted)
    for _ in range(30):
        assert routing.travel_seconds(rail,23,927,vehicle,{})==Fraction(17176,532763)
    assert len(calls)<10


def test_travel_reuse_observes_speed_partition_mutation_and_boolean_rejection():
    from fab_sim.routing import travel_seconds
    rail=ring()['rails'][0];v={'speed_um_s':'3000'};p={'curve_cap_um_s':'1000'}
    rail['speed_sections']=[{'start_um':0,'end_um':500,'curve':False},
                            {'start_um':500,'end_um':1000,'curve':True}]
    assert travel_seconds(rail,0,1000,v,p)==Fraction(2,3)
    rail['speed_sections'][1]['curve']=False
    assert travel_seconds(rail,0,1000,v,p)==Fraction(1,3)
    rail['speed_sections'][0]['start_um']=False
    with pytest.raises(ValueError,match='partition'):travel_seconds(rail,0,1000,v,p)


def test_travel_cache_observes_offsets_speed_caps_and_multiplier_edits():
    from fab_sim.routing import travel_seconds
    rail=ring()['rails'][0];v={'speed_um_s':'3000'};p={'curve_cap_um_s':'1000'}
    assert travel_seconds(rail,0,1000,v,p)==Fraction(1,3)
    rail['absolute_cap_um_s']='500'
    assert travel_seconds(rail,0,1000,v,p)==2
    rail['curve']=True;p['curve_cap_um_s']='400'
    assert travel_seconds(rail,0,1000,v,p)==Fraction(5,2)
    rail['speed_multiplier']='1/2'
    assert travel_seconds(rail,0,1000,v,p)==5
    v['speed_um_s']='200'
    assert travel_seconds(rail,100,300,v,p)==2
    rail['speed_multiplier']='0'
    with pytest.raises(ValueError,match='invalid rail speed'):travel_seconds(rail,100,300,v,p)


def test_travel_reuse_evicts_without_changing_exact_costs():
    from fab_sim.routing import travel_seconds,_travel_for_values
    rail=ring()['rails'][0];v={'speed_um_s':'3337'}
    for high in range(1,17001):
        rail['length_um']=high
        assert travel_seconds(rail,0,high,v,{})==Fraction(high,3337)
    assert _travel_for_values.cache_info().currsize<=16384
    rail['length_um']=1
    assert travel_seconds(rail,0,1,v,{})==Fraction(1,3337)
