"""Declared policy variants preserve prepared source and physical constraints."""
import json
import pytest
from test_runner import fixture
from test_route_rules import step


def scenario(tmp_path,value=4):
    return fixture(tmp_path,model_updates={
        'routes':[{'id':'r','product_id':'p','steps':[step(1,setup={'id':'B','when':'need','fallback':None})],
                   'dedication_components':[]}],
        'setup_transitions':[{'id':'setup:1','current':None,'new':'B','duration_us':4,'minimum_runs':7}]},
        inputs={'manufacturing_path':'manufacturing.json','physical_reference_path':'reference.json'},
        generation={'rack_capacities':{'A':8},'entry_capacity':4,'exit_capacity':1,'improvement_limit':1},
        workload={'count':4},execution={'horizon_us':10000000000,'setup_minimum_runs':{'B':value}})


def test_setup_variant_drains_and_records_original_rule_without_editing_inputs(tmp_path):
    from fab_sim.preflight import preflight
    from fab_sim.runner import run
    path=scenario(tmp_path);source=(tmp_path/'manufacturing.json').read_bytes()
    finding=preflight(path)
    assert finding['scenario']['kind']=='setup_minimum_runs_sensitivity_v1'
    assert finding['scenario']['changes']==[{'transition_id':'setup:1','new_setup':'B','source_minimum_runs':7,'effective_minimum_runs':4}]
    result=run(path)
    assert result['outcome']=='Completed' and result['exited_count']==4
    assert result['scenario']==finding['scenario']
    assert (tmp_path/'manufacturing.json').read_bytes()==source
    rows=[json.loads(x) for x in (tmp_path/'result/events.jsonl').read_text().splitlines()]
    campaigns=[r for r in rows if r['event_type']=='setup_campaign_committed']
    assert len(campaigns)==1 and len(campaigns[0]['payload']['members'])==4


@pytest.mark.parametrize('value',[0,-1,True,'4',4.5])
def test_invalid_setup_policy_override_rejects_before_output(tmp_path,value):
    from fab_sim.runner import run
    with pytest.raises(ValueError,match='setup_minimum_runs'):run(scenario(tmp_path,value))
    assert not (tmp_path/'result').exists()


def test_unknown_setup_override_rejects_before_output(tmp_path):
    from fab_sim.runner import run
    path=scenario(tmp_path);c=json.loads(path.read_text());c['execution']['setup_minimum_runs']={'missing':4};path.write_text(json.dumps(c))
    with pytest.raises(ValueError,match='unknown setup'):run(path)
    assert not (tmp_path/'result').exists()
