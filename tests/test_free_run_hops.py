"""Declared free-run hop policy: extend one hop through clear holding centres."""
from copy import deepcopy
import pytest
from fab_sim.scheduler import Scheduler
from test_traffic import model, drain


def factory(lookahead, m=None):
    from fab_sim.traffic import Traffic
    m=m or model();s=Scheduler();events=[];vehicles={v['id']:deepcopy(v) for v in m['vehicles']}
    t=Traffic(m,s,vehicles,lambda k,p:events.append((k,deepcopy(p))),hop_lookahead_um=lookahead)
    return t,s,vehicles,events


def test_clear_run_is_one_hop_with_the_same_arrival_time():
    arrivals={}
    for lookahead in (0,100000):
        t,s,v,e=factory(lookahead);t.request('v1','dock','move',on_complete=lambda:arrivals.setdefault(lookahead,s.now_us))
        drain(s)
        starts=[p for k,p in e if k=='traffic_hop_started']
        assert v['v1']['offset_um']==5000 and t.occupancy['v1']=={'a':[(4900,5100)]}
        assert not t.hops and not t.resources
        if lookahead:
            assert len(starts)==1 and starts[0]['claims']=={'a':[(400,5100)]}
            assert [p['start_um'] for p in starts[0]['parts']]==[500]
        else:assert len(starts)==5
    assert arrivals[0]==arrivals[100000]==4500000


def test_lookahead_bounds_the_extra_distance():
    t,s,v,e=factory(2000);t.request('v1','dock','move');s.settle(0)
    # First hop to the 1100 holding centre plus at most 2000 um more.
    assert t.hops['v1']['target_pose']=={'rail_id':'a','offset_um':3100}
    drain(s);assert v['v1']['offset_um']==5000


def test_extension_stops_before_another_body():
    vehicles=[{'id':'v1','type_id':'type','rail_id':'a','offset_um':500},
              {'id':'v2','type_id':'type','rail_id':'a','offset_um':4000}]
    t,s,v,e=factory(100000,model(vehicles));t.request('v1','dock','move');s.settle(0)
    hop=t.hops['v1']
    assert hop['target_pose']['offset_um']==3100
    assert all(high<=3900 for high in (iv[1] for iv in hop['claims']['a']))


def test_extension_never_needs_more_passage_resources():
    m=model(length=3000,dock=2000)
    t,s,v,e=factory(100000,m)
    # Route a->b->a crosses node y (a junction resource) once; the hop may not
    # extend past the junction region the first hop already required.
    target=next(h['id'] for h in t.holdings.values() if h['rail_id']=='b' and h['offset_um']>=2000)
    t.request('v1',target,'move');s.settle(0)
    first=set(t.hops['v1']['resources'])
    drain(s)
    for k,p in e:
        if k=='traffic_hop_started':assert len(p['resources'])<=max(1,len(first))
    assert v['v1']['rail_id']=='b'


def test_faulted_rail_is_not_entered_by_extension():
    t,s,v,e=factory(100000);t.causes['rail']['b'].add('fault')
    target=next(h['id'] for h in t.holdings.values() if h['rail_id']=='a' and h['offset_um']>8000)
    t.request('v1',target,'move');s.settle(0)
    assert t.requests['move']['status']=='moving' and 'b' not in t.hops['v1']['claims']


def test_invalid_lookahead_rejected():
    with pytest.raises(ValueError):factory(-1)
