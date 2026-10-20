from fractions import Fraction
import pytest


def model():
    return {'model':{'releases':[
        {'id':'a','product_id':'P3','route_id':'R3','priority':1,'super_hot':False,'wafer_count':25,
         'first_at_us':0,'interval_seconds':'0.0000015','lots_per_release':1,'due_allowance_us':10},
        {'id':'b','product_id':'P3','route_id':'R3','priority':2,'super_hot':True,'wafer_count':25,
         'first_at_us':0,'interval_seconds':'0.000003','lots_per_release':1,'due_allowance_us':20},
        {'id':'c','product_id':'P4','route_id':'R4','priority':1,'super_hot':False,'wafer_count':25,
         'first_at_us':0,'interval_seconds':'0.0000015','lots_per_release':1,'due_allowance_us':30}]}}


def source(settings):
    from fab_sim.workload import ReleaseSource
    return ReleaseSource(model(),settings)


def test_largest_remainder_ties_zero_weights_and_exact_total():
    from fab_sim.workload import allocate_quotas
    assert allocate_quotas(5,{'P4':Fraction(1,2),'P3':Fraction(1,2)})=={'P3':3,'P4':2}
    assert allocate_quotas(7,{'P3':Fraction(0),'P4':Fraction(1)})=={'P3':0,'P4':7}
    with pytest.raises(ValueError): allocate_quotas(0,{'P3':1})


def test_default_intervals_and_no_accumulated_rounding_drift():
    s=source({'count':10,'selected_streams':['a']})
    intents=[s.next_intent() for _ in range(10)]
    assert [i['scheduled_at_us'] for i in intents]==[0,2,3,4,6,8,9,10,12,14]
    assert s.next_intent() is None
    assert s.effective_intervals['a']==Fraction(3,2000000)
    assert intents[9]['due_at_us']==24
    assert [i['lot_id'] for i in intents]==[f'a:{i}' for i in range(10)]


def test_product_override_preserves_aggregate_and_class_rate_proportions():
    s=source({'count':20,'product_weights':{'P3':'0.2','P4':'0.8'}})
    rates={stream['id']:Fraction(stream['lots_per_release'])/s.effective_intervals[stream['id']] for stream in model()['model']['releases']}
    assert sum(rates.values())==sum(Fraction(st['lots_per_release'])/Fraction(st['interval_seconds']) for st in model()['model']['releases'])
    assert rates['a']/rates['b']==2
    assert (rates['a']+rates['b'])/sum(rates.values())==Fraction(1,5)
    assert s.quotas=={'P3':4,'P4':16}


def test_stream_order_and_occurrence_are_stable_and_incremental():
    s=source({'count':4})
    first=[s.next_intent() for _ in range(4)]
    assert [i['lot_id'] for i in first[:3]]==['a:0','b:0','c:0']
    assert [i['release_sequence'] for i in first]==[1,2,3,4]
    assert sum(s.realized_classes.values())==4
    assert len(s.pending_streams)<=3


def test_zero_weight_omits_product_and_dual_validation_rejects_zero_quota():
    s=source({'count':3,'product_weights':{'P3':0,'P4':1}})
    assert {s.next_intent()['product_id'] for _ in range(3)}=={'P4'}
    with pytest.raises(ValueError,match='dual'): source({'count':1,'dual_route_validation':True})


@pytest.mark.parametrize('settings',[
    {'count':True}, {'count':2,'release_time_scale':0}, {'count':2,'product_weights':{'P3':-1}},
    {'count':2,'selected_streams':['a'],'product_weights':{'P4':1}},
    {'count':2,'surprise':1},
])
def test_invalid_release_settings_reject(settings):
    with pytest.raises(ValueError): source(settings)


def test_multiple_lots_per_release_share_timestamp_with_unique_lot_ids():
    from fab_sim.workload import ReleaseSource
    m=model(); m['model']['releases']=m['model']['releases'][:1]
    m['model']['releases'][0]['lots_per_release']=2
    s=ReleaseSource(m,{'count':4})
    intents=[s.next_intent() for _ in range(4)]
    assert [i['scheduled_at_us'] for i in intents]==[0,0,2,2]
    assert [i['source_occurrence_index'] for i in intents]==[0,0,1,1]
    assert len({i['lot_id'] for i in intents})==4


def test_ongoing_source_remains_incremental_and_has_no_finite_quota():
    s=source({})
    assert s.quotas is None
    for _ in range(1000): assert s.next_intent() is not None
    assert len(s.pending_streams)==3


def test_ongoing_original_intervals_are_exact_with_no_weight_override():
    s=source({'release_time_scale':'1'})
    assert s.effective_intervals==s.source_intervals
    assert s.count is None and s.manifest()['quotas'] is None
