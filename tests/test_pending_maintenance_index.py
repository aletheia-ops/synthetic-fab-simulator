from test_maintenance import factory, pm, run_until
from fab_sim.probes import state_projection


class NoHistoryScan(dict):
    def values(self):
        raise AssertionError('dispatch must not scan retained maintenance history')


def test_dispatch_without_pending_work_does_not_scan_history():
    maintenance, *_ = factory([pm(first=100)])
    maintenance.causes = NoHistoryScan(maintenance.causes)
    maintenance.dispatch()


def test_pending_dispatch_avoids_history_scan_and_rechecks_availability():
    maintenance, manufacturing, *rest = factory([pm(first=0)])
    record = next(iter(maintenance.causes.values()))
    maintenance.pending(record['id'])
    maintenance.causes = NoHistoryScan(maintenance.causes)
    manufacturing.tools['t']['available'] = False
    maintenance.dispatch()
    assert record['status'] == 'pending'
    manufacturing.tools['t']['available'] = True
    maintenance.dispatch()
    assert record['status'] == 'active'
    assert maintenance.causes[record['id']] is record


def test_same_due_time_uses_cause_id_order_and_retains_recovered_history():
    z, a = pm(first=0), pm(first=0)
    z['id'], a['id'] = 'z', 'a'
    maintenance, manufacturing, scheduler, custody, lots, events = factory([z, a])
    run_until(scheduler, 6)
    starts = [payload for kind, payload in events if kind == 'maintenance_started']
    assert [(r['definition_id'], r['started_at_us']) for r in starts] == [('a', 0), ('z', 3)]
    recovered = [r for r in maintenance.causes.values() if r['status'] == 'recovered']
    assert len(recovered) == 2
    assert not manufacturing.tools['t']['pm_pending']
    maintenance.dispatch()
    assert len([kind for kind, payload in events if kind == 'maintenance_started']) == 2


def test_derived_pending_index_does_not_change_physical_projection():
    maintenance, *_ = factory([pm(first=100)])
    before = state_projection(maintenance)
    maintenance._pending_by_tool = {'t': {'derived-only': 'cache'}}
    assert state_projection(maintenance) == before
    next(iter(maintenance.causes.values()))['status'] = 'pending'
    assert state_projection(maintenance) != before
