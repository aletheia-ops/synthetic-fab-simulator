"""Area-clustered column placement, overflow buffer tiers and bay-entry divert guards."""
from types import SimpleNamespace
from fab_sim.area_placement import assign_columns
from fab_sim.transfers import Transfers


def site(sid, bay, x, height=1000):
    return {'id': sid, 'bay_id': bay, 'x_um': x, 'y_um': 0, 'width_um': 500, 'height_um': height}


class Costs:
    """Travel between representative sites: 1 within a bay, 10 across bays."""
    def __init__(self, sites):
        self.bay = {s['id']: s['bay_id'] for s in sites}
    def __getitem__(self, pair):
        return 1 if self.bay[pair[0]] == self.bay[pair[1]] else 10


def tool(tid, group, area, height=1000):
    return {'id': tid, 'tool_group_id': group, 'area': area, 'width_um': 500, 'height_um': height}


def test_area_placement_sorts_areas_into_their_own_bays_within_column_length():
    sites = [site('a1', 'A', 0), site('a2', 'A', 10), site('b1', 'B', 100), site('b2', 'B', 110)]
    tools = [tool('x1', 'gx', 'X'), tool('y1', 'gy', 'Y'), tool('x2', 'gx', 'X'), tool('y2', 'gy', 'Y')]
    # Mixed start: each bay holds one tool of each area.
    initial = {'x1': ('A', 0), 'y1': ('A', 10), 'x2': ('B', 100), 'y2': ('B', 110)}
    spans = {('A', 0): (2200,), ('A', 10): (2200,), ('B', 100): (2200,), ('B', 110): (2200,)}
    placed, history = assign_columns(sites, tools, {('gx', 'gy'): 1}, Costs(sites), clearance_um=100,
        tolerance='0', tool_load={'gx': 1, 'gy': 1}, balance_weight='0', spans=spans, initial=initial, workers=1)
    bay_of = {t: col[0] for t, col in placed.items()}
    assert bay_of['x1'] == bay_of['x2'] and bay_of['y1'] == bay_of['y2'] and bay_of['x1'] != bay_of['y1']
    assert history['final_mixing'] == 0 < history['initial_mixing']
    # Every column still packs its bodies: two 1000 bodies + 100 clearance <= 2200.
    for col in spans:
        assert sum(1 for c in placed.values() if c == col) <= 2


def test_area_placement_never_overfills_a_column_segment():
    sites = [site('a1', 'A', 0), site('b1', 'B', 100)]
    tools = [tool('x1', 'gx', 'X'), tool('x2', 'gx', 'X'), tool('y1', 'gy', 'Y')]
    initial = {'x1': ('A', 0), 'y1': ('A', 0), 'x2': ('B', 100)}
    spans = {('A', 0): (2100,), ('B', 100): (1000,)}
    placed, _ = assign_columns(sites, tools, {('gx', 'gy'): 1}, Costs(sites), clearance_um=100,
        tolerance='0', tool_load={'gx': 1, 'gy': 1}, balance_weight='0', spans=spans, initial=initial, workers=1)
    # Column B fits exactly one body; area sorting cannot put two tools there.
    assert sum(1 for c in placed.values() if c == ('B', 100)) == 1


def test_next_group_buffer_tiers_prefer_group_bays_then_neighbours():
    model = {'rails': [{'id': 'r1', 'bay_id': 'A'}, {'id': 'r2', 'bay_id': 'B'}, {'id': 'r3', 'bay_id': 'C'}],
             'equipment': [{'id': 't1', 'bay_id': 'A'}],
             'bays': [{'id': 'A', 'source_neighbors': 'B'}, {'id': 'B', 'source_neighbors': 'A,C'}, {'id': 'C'}]}
    docks = {'b1': {'rail_id': 'r1'}, 'b2': {'rail_id': 'r2'}, 'b3': {'rail_id': 'r3'}}
    fake = SimpleNamespace(model=model, docks=docks, _buffer_tiers={},
                           manufacturing=SimpleNamespace(tools={'t1': {'tool_group_id': 'G'}}))
    own, near, every = Transfers._group_buffer_tiers(fake, 'G', ('b1', 'b2', 'b3'))
    assert own == ('b1',) and near == ('b2',) and every == ('b1', 'b2', 'b3')


def test_bay_entry_divert_only_from_an_interbay_into_an_intrabay_behind_waiting_vehicles():
    from fab_sim.traffic import Traffic
    t = SimpleNamespace(_rail_bay_kind={'i': 'INTERBAY', 'b': 'INTRABAY'}, hops={}, requests={}, active={})
    divert = Traffic._divert_at_bay_entry
    r = {'route_recovery': {}}
    on_interbay = {'rail_id': 'i'}
    into_bay = [{'rail_id': 'i'}, {'rail_id': 'b'}]
    blocker = [{'kind': 'rail', 'owner': 'w'}]
    # Not on an interbay, or not entering an intrabay: no divert.
    assert divert(t, r, {'rail_id': 'b'}, into_bay, blocker, None) is False
    assert divert(t, r, on_interbay, [{'rail_id': 'i'}], blocker, None) is False
    # Blocker moving (has a hop), or idle (no waiting request): no divert.
    t.hops = {'w': {}}
    assert divert(t, r, on_interbay, into_bay, blocker, None) is False
    t.hops = {}
    assert divert(t, r, on_interbay, into_bay, blocker, None) is False
    # Bounded: four diverts already recorded on this movement.
    t.active = {'w': 'rw'}; t.requests = {'rw': {'status': 'pending', 'blockers': [{'kind': 'rail'}]}}
    many = {'route_recovery': {'recovery_poses': [{}] * 4}}
    assert divert(t, many, on_interbay, into_bay, blocker, None) is False
