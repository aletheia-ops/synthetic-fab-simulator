import pytest
from fab_sim.rng import Streams


def draws(streams, scope, n=3):
    rng = streams.get('processing', scope)
    return [rng.random() for _ in range(n)]


def test_no_salt_keeps_the_default_streams():
    assert draws(Streams(7), 'lot:1') == draws(Streams(7, None), 'lot:1')


def test_salts_change_new_streams_and_are_reproducible():
    assert draws(Streams(7, 1), 'lot:1') == draws(Streams(7, 1), 'lot:1')
    assert draws(Streams(7, 1), 'lot:1') != draws(Streams(7, 2), 'lot:1') != draws(Streams(7), 'lot:1')


def test_restored_streams_continue_unchanged_under_a_salt():
    base = Streams(7); draws(base, 'tool:a', 2)
    state = base.export_state()
    expected = draws(base, 'tool:a')
    salted = Streams(7, 3); salted.restore_state(state)
    assert draws(salted, 'tool:a') == expected                 # existing stream: paired
    assert draws(salted, 'lot:new') != draws(Streams(7), 'lot:new')  # new stream: replicated


@pytest.mark.parametrize('bad', [0, -1, True, 1.5, '1'])
def test_invalid_salts_are_rejected(bad):
    with pytest.raises(ValueError):Streams(7, bad)


def test_salt_is_a_policy_setting():
    from fab_sim.checkpoint import POLICY_SETTINGS
    assert 'replication_salt' in POLICY_SETTINGS
