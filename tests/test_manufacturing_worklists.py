from test_maintenance import factory, pm

class NoHistory(dict):
    def values(self): raise AssertionError('historical cause scan')

def test_maintenance_recovery_ignores_completed_causes():
    owner,m,s,*_=factory([pm(first=0,repair=3)])
    s.settle(0)
    owner.causes=NoHistory(owner.causes)
    s.settle(3)
    assert not m.tools['t']['pm_pending']
    assert not owner._active_by_tool

def test_bindings_by_tool_preserve_definition_order():
    a,b=pm('counter'),pm('counter');a['id']='a';b['id']='b'
    owner,*_=factory([b,a])
    assert owner._bindings_by_tool['t']==(('t','pm','b'),('t','pm','a'))


def test_rack_resident_candidates_match_full_group():
    from test_manufacturing_facade import factory,place
    m,s,c,lots,_=factory()
    m.enter_visit('hot','1');m.enter_visit('low','1')
    place(c,'clow','rack');place(c,'chot','entry')
    expected=tuple(v for v in m.rules.current_visits('A') if c.locations.get(lots[v['lot_id']]['carrier_id']) in m.tools['t']['rack_slots'])
    assert m.rack_visits('t')==expected
    m.delivered('clow','rack');s.settle(0)
    assert m.rules.visits[lots['low']['current_visit_id']].get('execution_id')
    assert not m.rules.visits[lots['hot']['current_visit_id']].get('execution_id')

def test_known_guard_preserves_same_pass_new_groups():
    from test_manufacturing_facade import factory,place
    from fab_sim.resource_changes import ResourceChange
    m,s,c,lots,_=factory();v=m.enter_visit('low','1');place(c,'clow','rack');m.delivered('clow','rack')
    m._dirty_groups.take()
    m.tools['t']['available']=False
    def guard():
        m.tools['t']['available']=True
        return ResourceChange(group_ids=frozenset({'A'}))
    m.dispatch_guards=[guard];m.dispatch()
    assert m.rules.visits[v].get('execution_id')


def test_known_unchanged_guard_does_not_force_global_tool_scan():
    from test_manufacturing_facade import factory
    from fab_sim.resource_changes import ResourceChange
    from fab_sim.probes import Probes
    m,*_=factory();m.probes=Probes();m._dirty_groups.take()
    m.dispatch_guards=[lambda:ResourceChange()]
    m.dispatch()
    assert not m.probes.work_counts['manufacturing_tools_considered']


def test_campaign_reuses_unchanged_empty_proposals_and_invalidates(monkeypatch):
    from test_campaigns import factory
    campaigns,t,m,s,c,*_=factory(3)
    assert not campaigns.dispatch()
    original=campaigns._candidate
    monkeypatch.setattr(campaigns,'_candidate',lambda *a: (_ for _ in ()).throw(AssertionError('unchanged candidate rescanned')))
    assert not campaigns.dispatch()
    monkeypatch.setattr(campaigns,'_candidate',original)
    for _ in range(10000):campaigns.invalidate_tools(['t'])
    assert len(campaigns._dirty_tools)<=len(campaigns.managed_tools)
    assert not campaigns._proposal_cache
    assert not campaigns.dispatch()
    campaigns.invalidate_tools(None)
    assert campaigns._dirty_tools==campaigns.managed_tools
