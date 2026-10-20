from fab_sim.probes import Probes

def test_probe_cpu_is_measured_independently_of_elapsed_wall_time():
    wall=iter((100,1000));cpu=iter((10,70))
    p=Probes(clock=lambda:next(wall),cpu_clock=lambda:next(cpu))
    with p.handler('traffic'):p.count_work('unchanged_blocked_attempts',3)
    assert p.wall_ns['traffic']==900 and p.cpu_ns['traffic']==60
    assert p.work_counts['unchanged_blocked_attempts']==3
    assert p.traces[-1]['cpu_ns']==60

def test_failed_cpu_clock_cannot_change_domain_result_or_wall_measurement():
    def broken():raise OSError('unavailable')
    wall=iter((0,10));p=Probes(clock=lambda:next(wall),cpu_clock=broken)
    with p.handler('traffic'):value=42
    assert value==42 and p.wall_ns['traffic']==10 and not p.cpu_ns
    assert p.cpu_unavailable==1


def test_work_timings_are_reporting_only_not_physical_progress(tmp_path):
    from test_runner import fixture
    from fab_sim.runner import run
    records=[];result=run(fixture(tmp_path),progress=records.append,progress_interval=15)
    assert result["outcome"]!="Failed"
    assert "work_observation" not in result["progress"]
    assert records and "work_observation" in records[-1]
    assert "dispatch:traffic" in records[-1]["work_observation"]["cpu_ns"]


def test_no_event_dispatch_cpu_is_visible_without_changing_physical_state():
    from fab_sim.scheduler import Scheduler
    from fab_sim.probes import state_projection,domain_digest
    def execute(enabled):
        s=Scheduler();p=Probes(enabled=enabled);s.probes=p
        s.register_pass('traffic',lambda:False);s.mark_dirty('traffic');s.settle(0)
        return domain_digest(state_projection(s)),p,s.export_state()
    a,on,sa=execute(True);b,off,sb=execute(False)
    assert a==b and sa==sb
    assert on.counts['dispatch:traffic']==1 and 'dispatch:traffic' in on.cpu_ns
    assert on.work_counts['dispatch_no_change:traffic']==1 and not off.cpu_ns
