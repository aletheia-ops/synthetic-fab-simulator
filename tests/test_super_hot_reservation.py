"""Super-hot reservation: the destination tool of a super-hot LOT in transfer starts no other LOT."""
from types import SimpleNamespace
from fab_sim.manufacturing import Manufacturing
from fab_sim.transfers import Transfers


def transfers(lots, active, demands):
    slots = {'T1:rack:0': {'equipment_id': 'T1'}, 'buf:0': {'equipment_id': None}}
    t = SimpleNamespace(manufacturing=SimpleNamespace(lots=lots, tools={'T1': {}, 'T2': {}}),
        active=active, demands=demands, custody=SimpleNamespace(slots=slots), _super_hot_lots=None)
    return lambda: Transfers.super_hot_destinations(t)


def test_destination_of_active_super_hot_transfer_is_held_and_released_after_delivery():
    lots = {'hot:0': {'carrier_id': 'c0', 'super_hot': True}, 'reg:0': {'carrier_id': 'c1', 'super_hot': False}}
    demands = {'d0': {'rack_slot_id': 'T1:rack:0', 'destination_id': None},
               'd1': {'rack_slot_id': None, 'destination_id': 'T1:rack:0'}}
    active = {'c0': 'd0', 'c1': 'd1'}
    holds = transfers(lots, active, demands)
    assert holds() == {'T1'}
    del active['c0']
    assert holds() == set()


def test_buffer_destination_is_not_held_and_new_super_hot_lot_is_seen():
    lots = {'reg:0': {'carrier_id': 'c1', 'super_hot': False}}
    demands = {'d0': {'rack_slot_id': None, 'destination_id': 'buf:0'}}
    active = {'c0': 'd0'}
    holds = transfers(lots, active, demands)
    assert holds() == set()
    lots['hot:1'] = {'carrier_id': 'c0', 'super_hot': True}
    assert holds() == set()
    demands['d0']['destination_id'] = 'T1:rack:0'
    assert holds() == {'T1'}


def test_held_tool_admits_only_super_hot_lots():
    m = SimpleNamespace(super_hot_holds=lambda: {'T1'})
    held = lambda tid, lot: Manufacturing._held_out(m, tid, lot)
    assert held('T1', {'super_hot': False}) and not held('T1', {'super_hot': True})
    assert not held('T2', {'super_hot': False})
    m.super_hot_holds = None
    assert not held('T1', {'super_hot': False})
