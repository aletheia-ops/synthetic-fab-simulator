from copy import deepcopy
from test_traffic import factory,model


def test_busy_traffic_permissions_use_scoped_claims_and_refresh_same_pass(monkeypatch):
    m=model([
        {'id':'v1','type_id':'type','rail_id':'a','offset_um':500},
        {'id':'v2','type_id':'type','rail_id':'a','offset_um':7000}])
    m['docks'].append({'id':'dock2','rail_id':'a','offset_um':9000})
    t,s,vehicles,events=factory(m)
    t.request('v1','dock','one')
    t.request('v2','dock2','two')
    original=t._blockers;checks=[]
    def blockers(owner,claims,resources,*,claim_index=None):
        assert claim_index is not None
        # Every permission query must agree with a fresh exhaustive lookup,
        # including earlier targets/hops created during this dispatch pass.
        expected=original(owner,claims,resources)
        actual=original(owner,claims,resources,claim_index=claim_index)
        assert actual==expected;checks.append(owner)
        return actual
    monkeypatch.setattr(t,'_blockers',blockers)
    assert t.dispatch() and checks==['v1','v2']
    assert set(t.hops)=={'v1','v2'}


def test_clearance_skips_empty_tool_cohorts_but_keeps_resident_tool(monkeypatch):
    from test_clearance import lease_fixture
    from fab_sim.resource_index import ResourceIndex
    pool,t,m,s,c,lots=lease_fixture();add_tool(m,c,'cold','unused')
    m.resource_index=ResourceIndex(c.slots,m.tools,t.model)
    original=pool._cohort;calls=[]
    def cohort(tid):
        calls.append(tid);assert tid!='cold';return original(tid)
    monkeypatch.setattr(pool,'_cohort',cohort)
    assert pool.dispatch() and calls==['t']


def test_static_group_frontier_keeps_global_tool_order_and_unknown_fallback():
    from fab_sim.resource_index import ResourceIndex
    tools={tid:{'tool_group_id':group,'rack_slots':[]} for tid,group in [('z','A'),('a','B'),('c','A')]}
    index=ResourceIndex({},tools,{'docks':[]})
    assert index.dispatch_tools({'A','B'})==('a','c','z')
    assert index.dispatch_tools({'A'})==('c','z')
    assert index.dispatch_tools(set())==()
    assert index.dispatch_tools(None)==('a','c','z')
    assert index.dispatch_tools({'unknown'})==()


def test_service_pending_order_reused_and_invalidated_without_history_retention():
    from test_service import handlers
    s,h,*_=handlers()
    for rid in ('z','a'):h.request(rid,'load',[],lambda:1,lambda:None)
    first=h.pending_requests()
    assert h.pending_requests() is first
    h.requests['z']['queued_at_us']=-1
    assert [r['id'] for r in h.pending_requests()]==['z','a']
    h.set_request_status('z','cancelled');h.set_request_status('a','cancelled')
    assert h.pending_requests()==() and h._pending_order==()
    h.rebuild_derived();assert h.pending_requests()==()


def test_unchanged_denied_requests_share_one_claim_index(monkeypatch):
    m=model([
        {'id':'v1','type_id':'type','rail_id':'a','offset_um':500},
        {'id':'block1','type_id':'type','rail_id':'a','offset_um':1100},
        {'id':'v2','type_id':'type','rail_id':'a','offset_um':7500},
        {'id':'block2','type_id':'type','rail_id':'a','offset_um':8100}])
    m['docks'].append({'id':'dock2','rail_id':'a','offset_um':9000})
    t,s,vehicles,events=factory(m)
    t.request('v1','dock','one');t.request('v2','dock2','two')
    assert not t.dispatch()
    original=t._claim_index;calls=[]
    def build():calls.append(1);return original()
    monkeypatch.setattr(t,'_claim_index',build)
    assert not t.dispatch() and len(calls)==1


def add_tool(m,c,name,group):
    sid=name+':rack'
    c.slots[sid]={'id':sid,'kind':'rack','equipment_id':name,'carrier_id':None,'reservation':None}
    m.tools[name]={'id':name,'tool_group_id':group,'rack_slots':[sid]}
    m.setups[name]=deepcopy(m.setups['t']);m.causes[name]=set()
    return sid
