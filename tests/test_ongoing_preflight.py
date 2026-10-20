"""Ongoing-release feasibility retains physics while future batch supply stays open."""
from copy import deepcopy
import pytest
from test_runner import fixture
from test_route_rules import step


def prepared(path):
    from fab_sim.runner import load_config
    from fab_sim.preflight import prepare
    c=load_config(path);m,t=prepare(c);return c,m,t


def test_ongoing_preflight_retains_physical_findings_without_quota_findings(tmp_path):
    from fab_sim.preflight import assess
    c,m,t=prepared(fixture(tmp_path,workload={}))
    r=assess(c,m,t);assert r['status']=='ready'
    assert not {f['code'] for f in r['findings']}&{'batch_cohort_shortage','conditional_batch_shortage','small_setup_supply'}
    for kind,code in [('tool','missing_tool_group'),('dock','missing_input_dock')]:
        broken=deepcopy(t)
        if kind=='tool':broken['model']['equipment']=[e for e in broken['model']['equipment'] if e['kind']!='ordinary']
        else:broken['model']['docks']=[d for d in broken['model']['docks'] if d.get('role','BOTH') not in ('IN','BOTH') or not (d.get('equipment_id') or '').startswith('A:')]
        assert code in {f['code'] for f in assess(c,m,broken)['findings']}
        assert 'unreachable_route_delivery' in {f['code'] for f in assess(c,m,broken)['findings']}
    m['model']['routes'][0]['steps'][0]['setup']={'id':'recipe'}
    m['model']['setup_transitions']=[{'new':'recipe','minimum_runs':7}]
    assert 'campaign_staging_capacity' in {f['code'] for f in assess(c,m,t)['findings']}


def test_root_batch_staging_warning_uses_open_source_lower_bound(tmp_path):
    from fab_sim.preflight import assess
    c,m,t=prepared(fixture(tmp_path,workload={},generation_settings={'entry_capacity':1},model_updates={
        'routes':[{'id':'r','product_id':'p','steps':[step(1,batch_min=50,batch_max=75,processing_unit='batch')],'dedication_components':[]}],
        'tool_groups':[{'id':'A','area':'a','batching':True,'cascading':False,'initial_setup':None,'loading_us':2,'unloading_us':3}]}))
    r=assess(c,m,t)
    f=next(f for f in r['findings'] if f['code']=='ongoing_root_batch_staging_risk')
    assert f['severity']=='warning' and f['evidence']['minimum_lots']==2 and f['evidence']['entry_capacity']==1
    assert not any('cohort_shortage' in f['code'] for f in r['findings'])


def test_open_batch_population_never_uses_finite_remainder_guard():
    from test_stocker_flow import flow_factory
    flow,traffic,t,m,s,c,lots,v,e=flow_factory(3,[step(1,batch_min=50,batch_max=75)])
    s.settle(0);lot=lots['a:0'];root=m.rules.steps['r']['1']
    flow.source.mode='continuous';flow.source.quotas=None
    assert flow.exact_batch_population(lot,root) is None


def test_unknown_and_zero_weight_target_products_are_rejected(tmp_path):
    from fab_sim.preflight import assess
    c,m,t=prepared(fixture(tmp_path,workload={},execution={'horizon_us':1000000000,'exit_targets':{'unknown':3}}))
    with pytest.raises(ValueError,match='target'):assess(c,m,t)
    release={**m['model']['releases'][0],'id':'inactive','product_id':'q','route_id':'rq'}
    m['model']['releases'].append(release)
    m['model']['routes'].append({**deepcopy(m['model']['routes'][0]),'id':'rq','product_id':'q'})
    c['workload']['product_weights']={'p':'1','q':'0'};c['execution']['exit_targets']={'q':3}
    with pytest.raises(ValueError,match='target'):assess(c,m,t)
