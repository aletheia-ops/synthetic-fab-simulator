import json
import pytest
from copy import deepcopy
from test_runner import fixture


def material(tmp_path):
    from fab_sim.runner import load_config
    path = fixture(tmp_path)
    return path, load_config(path), json.loads((tmp_path/'manufacturing.json').read_text()), json.loads((tmp_path/'topology.json').read_text())


def test_preflight_preserves_inputs_and_registers_topology_without_running(tmp_path):
    from fab_sim.preflight import preflight
    path = fixture(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    first = preflight(path)
    assert first == preflight(path)
    assert first['schema'] == 'fab-preflight-v1' and first['status'] == 'ready'
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    from fab_sim.runner import load_config
    from pathlib import Path
    directory=Path(load_config(path)['output']['topology_directory'])
    topology=json.loads((tmp_path/'topology.json').read_text())
    assert (directory/(topology['validation']['content_sha256']+'.json')).is_file()
    assert not (tmp_path/'result').exists()


def test_mandatory_batch_cannot_fit_cohort_but_sampling_is_conditional(tmp_path):
    from fab_sim.preflight import assess
    path, config, m, t = material(tmp_path)
    step = m['model']['routes'][0]['steps'][0]
    step.update(batch_min=75, batch_max=75, processing_unit='batch')
    result = assess(config, m, t)
    finding = next(x for x in result['findings'] if x['code'] == 'batch_cohort_shortage')
    assert result['status'] == 'rejected' and finding['evidence']['quota'] == 2
    step['sampling_probability'] = '0.5'
    result = assess(config, m, t)
    assert next(x for x in result['findings'] if x['code'] == 'conditional_batch_shortage')['severity'] == 'warning'


def test_campaign_capacity_and_unsupported_combination(tmp_path):
    from fab_sim.preflight import assess
    path, config, m, t = material(tmp_path)
    step = m['model']['routes'][0]['steps'][0]
    step['setup'] = {'id': 'B', 'when': 'need', 'fallback': None}
    m['model']['setup_transitions'] = [{'current': None, 'new': 'B', 'duration_us': 4, 'minimum_runs': 7}]
    config['workload']['count'] = 14
    result = assess(config, m, t)
    assert any(x['code'] == 'campaign_staging_capacity' and x['severity'] == 'error' for x in result['findings'])
    step.update(batch_min=25, batch_max=25, processing_unit='batch')
    assert any(x['code'] == 'unsupported_batch_setup_campaign' for x in assess(config, m, t)['findings'])


def test_missing_dock_and_same_tool_clearance_are_reported(tmp_path):
    from fab_sim.preflight import assess
    path, config, m, t = material(tmp_path)
    tool = next(e for e in t['model']['equipment'] if e['kind'] == 'ordinary')
    for dock in t['model']['docks']:
        if dock.get('equipment_id') == tool['id']:
            dock['role'] = 'OUT'
    assert any(x['code'] == 'missing_input_dock' for x in assess(config, m, t)['findings'])
    for dock in t['model']['docks']:
        if dock.get('equipment_id') == tool['id']:
            dock['role'] = 'IN'
    m['model']['routes'][0]['steps'].append(deepcopy(m['model']['routes'][0]['steps'][0]))
    m['model']['routes'][0]['steps'][1]['source_step_id'] = '2'
    assert any(x['code'] == 'missing_output_dock' for x in assess(config, m, t)['findings'])


def test_generated_defaults_keep_source_and_campaign_handoff_spare(tmp_path):
    from fab_sim.generation import generate_topology
    path, config, m, t = material(tmp_path)
    from fab_sim.inputs import bind_validation
    from fab_sim.sources.smt import VALIDATOR
    m['model']['routes'][0]['steps'][0]['setup'] = {'id': 'B', 'when': 'need', 'fallback': None}
    m['model']['setup_transitions'] = [{'current': None, 'new': 'B', 'duration_us': 4, 'minimum_runs': 7}]
    m = bind_validation(m, {'tool_groups': ['A']}, VALIDATOR)
    ref = json.loads((tmp_path/'reference.json').read_text())
    from fab_sim.sources.smat import traffic_requirements, VALIDATOR as PV
    ref = bind_validation(ref, traffic_requirements(m), PV)
    result = generate_topology(m, ref, {'improvement_limit': 1})
    assert next(e['rack_capacity'] for e in result['model']['equipment'] if e['kind'] == 'ordinary') == 8
    explicit = generate_topology(m, ref, {'rack_capacities': {'A': 2}, 'improvement_limit': 1})
    assert next(e['rack_capacity'] for e in explicit['model']['equipment'] if e['kind'] == 'ordinary') == 2


def test_canonical_run_rejects_mandatory_shortage_before_creating_output(tmp_path):
    import pytest
    from fab_sim.runner import run
    from test_route_rules import step
    group = {'id': 'A', 'area': 'a', 'batching': True, 'cascading': False,
        'initial_setup': None, 'loading_us': 2, 'unloading_us': 3}
    path = fixture(tmp_path, model_updates={'tool_groups': [group],
        'routes': [{'id': 'r', 'product_id': 'p', 'steps': [step(1, processing_unit='batch',
            batch_min=75, batch_max=75)], 'dedication_components': []}]})
    with pytest.raises(ValueError, match='batch_cohort_shortage'):
        run(path)
    assert not (tmp_path/'result').exists()


def test_preflight_cli_keeps_json_stdout_and_does_not_run(tmp_path):
    from test_canonical_cli import invoke
    result = invoke('preflight', '--config', fixture(tmp_path))
    assert result.returncode == 0 and json.loads(result.stdout)['status'] == 'ready'
    assert not (tmp_path/'result').exists()


def test_directed_disconnection_is_rejected_with_route_evidence(tmp_path):
    from fab_sim.preflight import assess
    path, config, m, t = material(tmp_path)
    t['model']['rails'] = [rail for rail in t['model']['rails'] if not rail['id'].endswith(':entrance')]
    result = assess(config, m, t)
    assert result['status'] == 'rejected'
    assert any(x['code'] == 'unreachable_route_delivery' and x['evidence']['from_step'] == 'entry'
               for x in result['findings'])


def test_dedicated_same_tool_successor_requires_buffer_clearance(tmp_path):
    from fab_sim.preflight import assess
    path, config, m, t = material(tmp_path)
    route = m['model']['routes'][0]
    route['steps'].append(deepcopy(route['steps'][0]))
    route['steps'][1].update(source_step_id='2', setup={'id': 'other', 'when': 'need', 'fallback': None})
    m['model']['setup_transitions'] = [{'current': None, 'new': 'other', 'duration_us': 4, 'minimum_runs': 1}]
    route['dedication_components'] = [{'id': 'same', 'source_step_ids': ['1', '2']}]
    t['model']['docks'] = [dock for dock in t['model']['docks'] if dock['kind'] != 'track_buffer']
    result = assess(config, m, t)
    assert any(x['code'] == 'no_same_tool_clearance' for x in result['findings'])


def test_generated_producer_capacity_covers_distinct_downstream_batch_tails(tmp_path):
    from test_route_rules import step
    from fab_sim.generation import generate_topology
    from fab_sim.inputs import bind_validation
    from fab_sim.sources.smat import traffic_requirements, VALIDATOR as PV
    from fab_sim.sources.smt import VALIDATOR
    path, config, m, t = material(tmp_path)
    m['model']['required_tool_group_ids'] = ['A', 'B']
    m['model']['tool_groups'].append({'id': 'B', 'batching': True, 'cascading': False})
    route = m['model']['routes'][0]
    route['steps'] = [step(1), step(2, tool_group_id='B', processing_unit='batch', batch_min=75, batch_max=75),
        step(3), step(4, tool_group_id='B', processing_unit='batch', batch_min=100, batch_max=100)]
    m = bind_validation(m, {'tool_groups': ['A', 'B']}, VALIDATOR)
    ref = bind_validation(json.loads((tmp_path/'reference.json').read_text()), traffic_requirements(m), PV)
    out = generate_topology(m, ref, {'improvement_limit': 1})
    # Two distinct batch keys can retain 2+3 unfinished LOTs; allow one next LOT
    # to complete either cohort and one physical handoff spare.
    assert next(e['rack_capacity'] for e in out['model']['equipment'] if e.get('tool_group_id') == 'A') == 7


@pytest.mark.parametrize('minimum, successor', [(0, 'B'), (1, None)])
def test_same_tool_reusable_setup_reuses_rack_without_buffer(tmp_path, minimum, successor):
    from fab_sim.preflight import preflight
    from fab_sim.runner import run
    from fab_sim.inputs import bind_validation, canonical_text
    from fab_sim.sources.smat import traffic_requirements, VALIDATOR
    from test_route_rules import step
    route = {'id': 'r', 'product_id': 'p', 'steps': [
        step(1, setup={'id': 'A', 'when': 'need', 'fallback': None}),
        step(2, setup={'id': successor, 'when': 'need', 'fallback': None} if successor else None)],
        'dedication_components': [{'id': 'same', 'source_step_ids': ['1', '2']}]}
    transitions = [{'current': None, 'new': target, 'duration_us': 4, 'minimum_runs': minimum}
                   for target in ('A', 'B')]
    path = fixture(tmp_path, model_updates={'routes': [route], 'setup_transitions': transitions})
    manufacturing = json.loads((tmp_path/'manufacturing.json').read_text())
    topology = json.loads((tmp_path/'topology.json').read_text())
    topology['model']['docks'] = [dock for dock in topology['model']['docks'] if dock['kind'] != 'track_buffer']
    topology = bind_validation(topology, traffic_requirements(manufacturing), VALIDATOR)
    (tmp_path/'topology.json').write_text(canonical_text(topology))
    assert preflight(path)['status'] == 'ready'
    result = run(path)
    assert result['outcome'] == 'Completed' and result['exited_count'] == 2
