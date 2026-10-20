from fab_sim.probes import Probes,domain_digest
from fab_sim.rng import Streams
from fractions import Fraction


def test_probes_sample_with_separate_counter_and_never_change_domain_or_draws():
    def run(enabled):
        streams=Streams(17);state={'values':[],'pose':Fraction(2,3)};ticks=iter(range(100))
        probes=Probes(enabled=enabled,sample_every=2,recent_limit=2,clock=lambda:next(ticks))
        for i in range(6):
            with probes.handler('processing'):state['values'].append(streams.get('processing','lot').random())
        state['rng']=streams.get('processing','lot').getstate()
        return domain_digest(state),probes
    a,on=run(True);b,off=run(False)
    assert a==b and on.counts['processing']==6 and on.wall_ns['processing']==6
    assert [r['handler_sequence'] for r in on.traces]==[4,6] and not off.traces


def test_probe_clock_failure_cannot_change_domain_exception_or_result():
    import pytest
    def broken():raise OSError('clock')
    p=Probes(clock=broken)
    with p.handler('test'):pass
    with pytest.raises(ValueError,match='domain'):
        with p.handler('test'):raise ValueError('domain')
    assert p.unavailable==2


def test_digest_canonicalizes_order_and_preserves_exact_rational_state():
    assert domain_digest({'a':1,'b':Fraction(1,3)})==domain_digest({'b':Fraction(1,3),'a':1})
    assert domain_digest({'a':Fraction(1,3)})!=domain_digest({'a':0.3333333333333333})
