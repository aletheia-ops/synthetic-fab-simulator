"""Artificial extra processing: selection table, probability, extra time, ground truth, stream use."""
import pytest
from fab_sim.perturbations import ExtraProcessing, validate
from fab_sim.rng import Streams

ROUTES = {'R3': {'steps': [{'source_step_id': '1', 'tool_group_id': 'Litho_A'},
                           {'source_step_id': '2', 'tool_group_id': 'Etch_A'}]},
          'R4': {'steps': [{'source_step_id': '1', 'tool_group_id': 'Litho_A'}]}}
GROUPS = {'Litho_A': {'area': 'Litho'}, 'Etch_A': {'area': 'Dry_Etch'}}


def rule(**kw):
    base = {'id': 'r', 'routes': ['R3'], 'probability': '1',
            'extra': {'kind': 'relative', 'factor': {'distribution': 'constant', 'mean': '0.5'}}}
    return {**base, **kw}


def make(*rules):
    events = []
    return ExtraProcessing(list(rules), ROUTES, GROUPS, Streams(7), lambda k, p: events.append((k, p))), events


def test_selection_is_precomputed_by_route_and_area():
    x, _ = make(rule(areas=['Litho']))
    assert set(x.table) == {('R3', '1')}
    x, _ = make(rule(routes=['R3', 'R4']))
    assert set(x.table) == {('R3', '1'), ('R3', '2'), ('R4', '1')}


def test_relative_and_absolute_extra_with_ground_truth_record():
    x, events = make(rule())
    assert x.extra_us('R3', '2', 1_000_000, execution_id='e', tool_id='T', lot_ids=['L']) == 500_000
    assert x.extra_us('R4', '1', 1_000_000, execution_id='e', tool_id='T', lot_ids=['L']) == 0
    [(kind, p)] = events
    assert kind == 'processing_perturbed' and p['extra_us'] == 500_000 and p['rule_id'] == 'r' and p['base_us'] == 1_000_000
    x, _ = make(rule(extra={'kind': 'absolute', 'seconds': {'distribution': 'constant', 'mean': '30'}}))
    assert x.extra_us('R3', '1', 5, execution_id='e', tool_id='T', lot_ids=['L']) == 30_000_000


def test_probability_zero_never_delays_and_share_tracks_probability():
    x, events = make(rule(probability='0'))
    assert all(x.extra_us('R3', '1', 10, execution_id=str(i), tool_id='T', lot_ids=['L']) == 0 for i in range(200))
    x, events = make(rule(probability='0.25'))
    hits = sum(x.extra_us('R3', '1', 10, execution_id=str(i), tool_id='T', lot_ids=['L']) > 0 for i in range(4000))
    assert 900 < hits < 1100 and len(events) == hits


def test_uses_only_the_perturbation_stream():
    streams = Streams(7)
    ExtraProcessing([rule()], ROUTES, GROUPS, streams, lambda k, p: None).extra_us('R3', '1', 10, execution_id='e', tool_id='T', lot_ids=['L'])
    assert set(streams._streams) == {('perturbation', 'r')}


@pytest.mark.parametrize('bad', [[], [rule(probability='1.5')], [rule(routes=[])], [rule(extra={'kind': 'x'})],
                                 [rule(), rule()], [rule(unknown=1)]])
def test_invalid_rules_are_rejected(bad):
    with pytest.raises(ValueError):
        validate(bad)


def test_unknown_route_is_rejected():
    with pytest.raises(ValueError, match='unknown route'):
        make(rule(routes=['R9']))
