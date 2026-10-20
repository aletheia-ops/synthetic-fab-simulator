from decimal import Decimal
import pytest


def test_sha256_compact_array_derivation_and_pinned_draws():
    from fab_sim.rng import rng_for
    r=rng_for(17,'processing','lot:1')
    assert [r.random() for _ in range(3)]==[0.33464594852032903,0.3813671879852888,0.5238629301889877]


def test_channels_and_scope_streams_are_independent():
    from fab_sim.rng import Streams, rng_for
    streams=Streams(17); processing=streams.get('processing','lot:1')
    expected=rng_for(17,'processing','lot:1')
    assert processing.random()==expected.random()
    for channel in ('release','maintenance','injected_faults','topology'):
        assert streams.get(channel,'scope').random()!=streams.get('processing','scope').random()
        for _ in range(100): streams.get(channel,'scope').random()
    assert processing.random()==expected.random()
    assert streams.get('processing','lot:1') is processing


@pytest.mark.parametrize('seed',[-1,True,1.0,'1',None])
def test_invalid_master_seed_rejects(seed):
    from fab_sim.rng import rng_for
    with pytest.raises(ValueError,match='seed'): rng_for(seed,'processing','lot:1')


def test_invalid_channel_or_scope_rejects():
    from fab_sim.rng import rng_for
    with pytest.raises(ValueError,match='channel'): rng_for(0,'publisher','scope')
    with pytest.raises(ValueError,match='scope'): rng_for(0,'processing','')


def test_constant_duration_does_not_consume_stream_and_uniform_bounds_hold():
    from fab_sim.rng import sample_seconds, rng_for
    r=rng_for(17,'processing','lot:1'); before=r.getstate()
    assert sample_seconds({'distribution':'constant','mean_seconds':'2','offset_seconds':'0'},r)==Decimal(2)
    assert r.getstate()==before
    for _ in range(100):
        assert 1<=sample_seconds({'distribution':'uniform','mean_seconds':'2','offset_seconds':'1'},r)<=3
