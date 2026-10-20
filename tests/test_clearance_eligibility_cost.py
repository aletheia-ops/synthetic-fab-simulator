from test_clearance import lease_fixture
from test_transfers import add_idle_replica


def fixture():
    pool,t,m,s,c,lots=lease_fixture()
    add_idle_replica(t,m,c)
    member={'visit_id':lots['low']['current_visit_id'],'origin_id':'rack'}
    return pool,t,m,c,lots,member


def test_full_fleet_eligibility_is_computed_once_per_compatibility_check(monkeypatch):
    pool,t,m,c,lots,member=fixture()
    original=m.rules.eligible_tools
    full_fleet_calls=[]
    def eligible(visit_id,tools):
        if tools==list(m.tools):full_fleet_calls.append(visit_id)
        return original(visit_id,tools)
    monkeypatch.setattr(m.rules,'eligible_tools',eligible)
    assert pool._compatible(member,'protected:0')
    assert full_fleet_calls==[member['visit_id']]


def test_compatibility_rechecks_dedication_and_input_role_between_calls():
    pool,t,m,c,lots,member=fixture()
    m.rules.routes['r']['dedication_components']=[{'id':'ded','source_step_ids':['2']}]
    lots['low']['dedication']['ded']='t'
    t.docks['other:port']['role']='OUT'
    assert pool._compatible(member,'protected:0')
    lots['low']['dedication']['ded']='other'
    assert not pool._compatible(member,'protected:0')
    t.docks['other:port']['role']='BOTH'
    assert pool._compatible(member,'protected:0')


def test_target_validity_and_directed_paths_stay_fresh(monkeypatch):
    pool,t,m,c,lots,member=fixture()
    assert pool._compatible(member,'protected:0')
    original=t._target_valid
    monkeypatch.setattr(t,'_target_valid',lambda r,sid:False)
    assert not pool._compatible(member,'protected:0')
    monkeypatch.setattr(t,'_target_valid',original)
    monkeypatch.setattr(t,'_path',lambda a,b:None)
    assert not pool._compatible(member,'protected:0')

def test_native_compatibility_computes_dedication_once_for_all_input_ports():
    import sys
    from fab_sim.manufacturing import RouteRules
    pool,t,m,c,lots,member=fixture()
    calls=[]
    def count(frame,event,arg):
        if event=='call' and frame.f_code is RouteRules.eligible_tools.__code__:
            calls.append(frame.f_locals['tool_ids'])
    previous=sys.getprofile()
    try:
        sys.setprofile(count)
        assert pool._compatible(member,'protected:0')
    finally:sys.setprofile(previous)
    assert calls==[['t','other']]


def test_class_target_override_is_rechecked_between_compatibility_calls(monkeypatch):
    from fab_sim.transfers import Transfers
    pool,t,m,c,lots,member=fixture()
    assert pool._compatible(member,'protected:0')
    monkeypatch.setattr(Transfers,'_target_valid',lambda self,r,sid:False)
    assert not pool._compatible(member,'protected:0')


def test_native_compatibility_rechecks_tool_group_between_calls():
    pool,t,m,c,lots,member=fixture()
    assert pool._compatible(member,'protected:0')
    m.tools['t']['tool_group_id']='foreign'
    m.tools['other']['tool_group_id']='foreign'
    assert not pool._compatible(member,'protected:0')
    m.tools['other']['tool_group_id']='A'
    assert pool._compatible(member,'protected:0')

def test_native_unbound_eligibility_skips_per_tool_predicates():
    import sys
    from collections import Counter
    from fab_sim.manufacturing import RouteRules
    pool,t,m,c,lots,member=fixture()
    tools=['other','t','other']
    lines=[]
    def count(frame,event,arg):
        if event=='line' and frame.f_code is RouteRules.eligible_tools.__code__:
            lines.append(frame.f_lineno)
        return count
    previous=sys.gettrace()
    try:
        sys.settrace(count)
        assert m.rules.eligible_tools(member['visit_id'],tools)==['other','t','other']
    finally:sys.settrace(previous)
    assert max(Counter(lines).values())==1


def test_unbound_eligibility_result_is_independent_and_bindings_stay_live():
    pool,t,m,c,lots,member=fixture()
    tools=['other','t','other']
    result=m.rules.eligible_tools(member['visit_id'],tools)
    result.clear()
    assert tools==['other','t','other']
    m.rules.routes['r']['dedication_components']=[{'id':'ded','source_step_ids':['2']}]
    lots['low']['dedication']['ded']='other'
    assert m.rules.eligible_tools(member['visit_id'],tools)==['other','other']
    lots['low']['dedication']['ded']='t'
    assert m.rules.eligible_tools(member['visit_id'],tools)==['t']
    m.rules.routes['r']['dedication_components'][0]['source_step_ids']=['1']
    assert m.rules.eligible_tools(member['visit_id'],tools)==['other','t','other']


def test_custom_step_lookup_keeps_per_dock_callback_semantics(monkeypatch):
    from fab_sim.manufacturing import Manufacturing
    pool,t,m,c,lots,member=fixture()
    calls=[]
    def step(self,visit):
        calls.append(visit['id'])
        return {'tool_group_id':'foreign' if len(calls)==1 else 'A'}
    monkeypatch.setattr(Manufacturing,'_step',step)
    assert pool._compatible(member,'protected:0')
    assert calls==[member['visit_id'],member['visit_id']]

def test_assignment_resolves_native_member_eligibility_once_for_all_buffers():
    import sys
    from fab_sim.manufacturing import RouteRules
    pool,t,m,c,lots,member=fixture()
    t._router()  # Cold geometry construction retains the ordinary call path.
    members=(member,{'visit_id':lots['hot']['current_visit_id'],'origin_id':'rack-extra:1'})
    calls=[]
    def count(frame,event,arg):
        if event=='call' and frame.f_code is RouteRules.eligible_tools.__code__:
            calls.append(frame.f_locals['visit_id'])
    previous=sys.getprofile()
    try:
        sys.setprofile(count)
        assert pool._first_assignment(members,('protected:0','protected:1'))==('protected:0','protected:1')
    finally:sys.setprofile(previous)
    assert calls==[member['visit_id'],members[1]['visit_id']]


def test_assignment_custom_compatibility_still_runs_for_every_member_buffer(monkeypatch):
    from fab_sim.clearance import ClearancePool
    pool,t,m,c,lots,member=fixture()
    calls=[]
    def compatible(self,member,sid):
        calls.append(sid)
        return sid=='protected:1'
    monkeypatch.setattr(ClearancePool,'_compatible',compatible)
    assert pool._first_assignment((member,),('protected:0','protected:1'))==('protected:1',)
    assert calls==['protected:0','protected:1']


def test_assignment_context_does_not_survive_dedication_or_dock_mutation():
    pool,t,m,c,lots,member=fixture()
    assert pool._first_assignment((member,),('protected:0','protected:1'))==('protected:0',)
    m.rules.routes['r']['dedication_components']=[{'id':'ded','source_step_ids':['2']}]
    lots['low']['dedication']['ded']='other'
    t.docks['other:port']['role']='OUT'
    assert pool._first_assignment((member,),('protected:0','protected:1')) is None
    t.docks['other:port']['role']='BOTH'
    assert pool._first_assignment((member,),('protected:0','protected:1'))==('protected:0',)

def test_assignment_custom_product_membership_cannot_reuse_member_context():
    pool,t,m,c,lots,member=fixture()
    t._router()
    m.rules.routes['r']['dedication_components']=[{'id':'ded','source_step_ids':['2']}]
    lots['low']['dedication']['ded']='t'
    lots['hot']['dedication']['ded']='t'
    t.docks['other:port']['role']='OUT'
    lots['low']['product_id']='low-product'
    lots['hot']['product_id']='hot-product'
    class Products(list):
        def __contains__(self,product):
            lots['low']['dedication']['ded']='other'
            return product=='low-product'
    t.docks['protected:1']['compatible_products']=Products()
    members=(member,{'visit_id':lots['hot']['current_visit_id'],'origin_id':'rack-extra:1'})
    assert pool._first_assignment(members,('protected:0','protected:1')) is None
    assert lots['low']['dedication']['ded']=='other'

def test_assignment_empty_members_or_slots_skips_eligibility():
    pool,t,m,c,lots,member=fixture()
    t._router()
    assert pool._first_assignment((),('protected:0','protected:1'))==()
    assert pool._first_assignment((member,),()) is None


def test_assignment_product_rejection_stays_before_conflicting_dedication():
    pool,t,m,c,lots,member=fixture()
    t._router()
    m.rules.routes['r']['dedication_components']=[
        {'id':'one','source_step_ids':['2']},{'id':'two','source_step_ids':['2']}]
    lots['low']['dedication'].update(one='t',two='other')
    t.docks['protected:0']['compatible_products']=['another-product']
    t.docks['protected:1']['compatible_products']=['another-product']
    assert pool._first_assignment((member,),('protected:0','protected:1')) is None


def test_assignment_empty_eligibility_does_not_evaluate_step_lookup():
    pool,t,m,c,lots,member=fixture()
    t._router()
    m.rules.routes['r']['dedication_components']=[{'id':'ded','source_step_ids':['2']}]
    lots['low']['dedication']['ded']='absent-tool'
    m.rules.steps.clear()
    assert not pool._compatible(member,'protected:0')
    assert pool._first_assignment((member,),('protected:0','protected:1')) is None
