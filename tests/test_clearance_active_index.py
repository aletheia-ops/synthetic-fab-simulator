from itertools import product
from fab_sim.clearance import ClearancePool
from test_clearance import lease_fixture


def test_first_assignment_equals_reference_dfs():
    pool=ClearancePool.__new__(ClearancePool)
    members=tuple(range(3));slots=('a','b','c')
    def reference(index,remaining,adj):
        if index==len(members):return ()
        for sid in remaining:
            if sid not in adj[index]:continue
            tail=reference(index+1,tuple(s for s in remaining if s!=sid),adj)
            if tail is not None:return (sid,)+tail
        return None
    for bits in product((False,True),repeat=9):
        adj={m:{s for n,s in enumerate(slots) if bits[m*3+n]} for m in members}
        pool._compatible=lambda member,sid:sid in adj[member]
        assert pool._first_assignment(members,slots)==reference(0,slots,adj)


def test_active_description_ignores_completed_history_and_is_defensive():
    pool,t,m,s,c,lots=lease_fixture()
    pool.dispatch();record=next(iter(pool.records.values()))
    expected=pool.describe()
    for n in range(10000):
        pool.records['past'+str(n)]={**record,'id':'past'+str(n),'status':'completed'}
    pool.rebuild_derived()
    class NoHistoryScan(dict):
        def values(self):raise AssertionError('clearance history scanned')
    pool.records=NoHistoryScan(pool.records)
    assert pool.describe_active()==expected
    description=pool.describe_active();description[0]['members'][0]['status']='bad'
    assert record['members'][0]['status']!='bad'
    assert pool.admission(record['members'][0]['visit_id'])


def test_retirement_removes_all_active_memberships():
    pool,t,m,s,c,lots=lease_fixture()
    for n in range(10000):
        rid=str(n)
        record={'id':rid,'tool_id':'t','status':'active','slots':['protected:0'],
                'members':[{'visit_id':'v','carrier_id':'c','slot_id':'protected:0','status':'planned'}]}
        pool.records[rid]=record;pool._index_record(record)
        assert rid in pool._active_by_tool['t']
        record['status']='completed';pool._retire_record(record)
        assert not pool._active_records
        assert not pool._active_by_tool and not pool._active_by_visit and not pool._active_by_carrier_slot
