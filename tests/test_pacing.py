import pytest
from test_publisher import Clock,Adapter,publisher,record
from fab_sim.publisher import Pacer
from fab_sim.probes import domain_digest
from fab_sim.rng import Streams


def test_240_simulated_seconds_take_two_wall_seconds_at_rate120():
    c=Clock();p=Pacer(rate=120,clock=c,sleep=c.sleep)
    p.wait_for(240_000_000);p.wait_for(240_000_000)
    assert c.now==2 and c.sleeps==[2]


def test_output_pause_shifts_future_targets_instead_of_causing_catchup_burst():
    c=Clock();p=Pacer(rate=120,clock=c,sleep=c.sleep)
    p.wait_for(120_000_000);c.now+=3;p.add_pause(3);p.wait_for(240_000_000)
    assert c.now==5 and c.sleeps==[1,1] and p.pause_s==3


def test_publication_interval_and_blocking_shift_pace_without_changing_domain_digest():
    def run(paced):
        c=Clock();a=Adapter(clock=c,cost=.2);pub,a,c=publisher(a,c,batch_records=10,flush_interval_s=.5)
        pace=Pacer(rate=1 if paced else None,publisher=pub)
        rng=Streams(9);state={'values':[]}
        for i in range(1,4):
            pace.wait_for(i*1_000_000);state['values'].append(rng.get('processing','lot').random());pub.submit([record(i)])
        pub.drain();return domain_digest(state),pace,pub,c
    a,pace,pub,c=run(True);b,_,_,_=run(False)
    assert a==b and pace.pause_s==pytest.approx(.6) and pub.accepted_count==3 and c.now==pytest.approx(3.6)


def test_engine_overrun_is_reported_without_resetting_timeline():
    c=Clock();p=Pacer(rate=1,clock=c,sleep=c.sleep);c.now=2
    assert p.wait_for(1_000_000)==1 and not c.sleeps
    p.wait_for(3_000_000);assert c.now==3 and c.sleeps==[1]


@pytest.mark.parametrize('rate',[0,-1,float('nan'),float('inf'),True])
def test_invalid_replay_rate_rejects(rate):
    with pytest.raises(ValueError):Pacer(rate=rate)


def test_bound_pacer_rejects_a_different_clock_or_sleep_owner():
    pub,a,c=publisher();other=Clock()
    with pytest.raises(ValueError,match='share'):Pacer(rate=1,publisher=pub,clock=other)
    with pytest.raises(ValueError,match='share'):Pacer(rate=1,publisher=pub,sleep=other.sleep)
    pace=Pacer(rate=1,publisher=pub,clock=c,sleep=c.sleep);pace.wait_for(1_000_000)
    assert c.now==1
