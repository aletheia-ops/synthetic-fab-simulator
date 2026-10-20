"""Congestion-aware leg planning: penalties steer path choice; returned seconds stay free-flow."""
from fractions import Fraction
from fab_sim.topology import compile_traffic
from fab_sim.routing_index import FreeFlowCosts


def model():
    # x -> y -> z (short) and x -> w -> z (longer), then z -> x to close the loop.
    rails = [('a', 'x', 'y', 1000), ('b', 'y', 'z', 1000), ('c', 'x', 'w', 1500), ('d', 'w', 'z', 1500), ('e', 'z', 'x', 1000)]
    raw = {'nodes': [{'id': n, 'zcu_id': None, 'zcu_type': 'NONE'} for n in 'xyzw'],
           'rails': [{'id': r, 'from_node': f, 'to_node': t, 'length_um': n, 'bay_id': 'bay', 'curve': False,
                      'speed_multiplier': '1'} for r, f, t, n in rails],
           'zcus': [], 'docks': [], 'vehicle_types': [{'id': 'type', 'length_um': 100, 'gap_um': 0, 'speed_um_s': '1000'}],
           'vehicles': [], 'policy': {'holding_pitch_um': 1000, 'raw_dock_clamp': False, 'curve_cap_um_s': '1000'}}
    return compile_traffic(raw)


def rails_of(path): return [p['rail_id'] for p in path['sublinks']]


def test_penalty_on_the_short_branch_selects_the_detour_with_free_flow_seconds():
    router = FreeFlowCosts(model(), 'type')
    start, end = {'rail_id': 'e', 'offset_um': 500}, {'rail_id': 'e', 'offset_um': 400}
    free = router.path(start, end)
    assert rails_of(free) == ['e', 'a', 'b', 'e']
    detour = router.path(start, end, {'b': 10})
    assert rails_of(detour) == ['e', 'c', 'd', 'e']
    assert detour['seconds'] == Fraction(500 + 1500 + 1500 + 400, 1000)
    # A small penalty does not outweigh the 1 s longer detour.
    assert rails_of(router.path(start, end, {'b': 1})) == ['e', 'a', 'b', 'e']
    # No penalties: identical to the cached free-flow path.
    assert router.path(start, end, {}) == free
