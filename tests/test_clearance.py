from copy import deepcopy
from pathlib import Path
import pytest
from test_generation import inputs,generate


def test_ordinary_only_requires_no_clearance_pool():
    from fab_sim.clearance import clearance_requirements
    m,_=inputs();topology=generate()
    result=clearance_requirements(m,topology)
    assert result['required_capacity']==0 and result['protected_slot_ids']==[]
    assert not result['findings']


def test_dataset1_protects_eight_of_fourteen_existing_buffers():
    from fab_sim.clearance import clearance_requirements
    from fab_sim.preflight import prepare
    from fab_sim.runner import load_config
    m,t=prepare(load_config(Path('tests/fixtures/dataset1-default-layout.json')))
    before=deepcopy(t)
    result=clearance_requirements(m,t)
    assert result['batch_bound']==6 and result['setup_bound']==7
    assert result['required_capacity']==len(result['protected_slot_ids'])==8
    assert len(set(result['protected_slot_ids']))==8
    assert len([d for d in t['model']['docks'] if d['kind']=='track_buffer'])==14
    assert t==before and not result['findings']


@pytest.mark.parametrize('incompatible',[False,True])
def test_clearance_count_cannot_substitute_for_compatible_directed_access(incompatible):
    from fab_sim.clearance import clearance_requirements
    m,_=inputs();m['model']['routes'][0]['steps'][0].update(batch_min=25,batch_max=25)
    t=generate();buffers=[d for d in t['model']['docks'] if d['kind']=='track_buffer']
    for d in buffers:
        if incompatible:d['compatible_products']=['different-product']
        else:d['role']='IN'
    result=clearance_requirements(m,t)
    assert result['required_capacity']==2
    assert result['findings'] and result['protected_slot_ids']==[]


def lease_fixture():
    from test_transfers import factory
    from test_route_rules import step
    from test_manufacturing_facade import place
    from fab_sim.clearance import ClearancePool
    t,m,s,c,lots,vehicles,events=factory([step(1),step(2,setup={'id':'B','when':'need'})],rack_count=2)
    m.setups['t'].current='A';m.setups['t'].transitions=({'current':'A','new':'B','minimum_runs':2,'duration_us':1},)
    for lid in lots:
        vid=m.enter_visit(lid,'2');place(c,lots[lid]['carrier_id'],'rack' if lid=='low' else 'rack-extra:1')
        t.request(lots[lid]['carrier_id'],vid,('port',))
    for sid,offset in [('protected:0',650),('protected:1',700)]:
        c.slots[sid]={'id':sid,'kind':'track_buffer','carrier_id':None,'reservation':None}
        t.docks[sid]={'id':sid,'kind':'track_buffer','role':'BOTH','rail_id':'a','offset_um':offset}
    pool=ClearancePool(m,t,['protected:0','protected:1'],lambda k,p:events.append((k,deepcopy(p))))
    return pool,t,m,s,c,lots


def test_missing_one_cohort_slot_leaves_every_claim_untouched():
    pool,t,m,s,c,lots=lease_fixture();c.reserve('protected:1','foreign');before=deepcopy(c.slots)
    assert not pool.dispatch()
    assert c.slots==before and not pool.records
    assert pool.describe()[0]['required_capacity']==2
    c.release('protected:1','foreign');assert pool.dispatch()
    record=next(iter(pool.records.values()))
    assert len(record['members'])==2 and len(set(record['slots']))==2
    assert {c.slots[x]['reservation'] for x in record['slots']}=={record['id']}
    assert all(pool.admission(lot['current_visit_id']) for lot in lots.values())
    assert not pool.dispatch()


def test_ordinary_overflow_cannot_take_protected_storage():
    pool,t,m,s,c,lots=lease_fixture()
    r=t.demands[t.active['clow']]
    assert all(choice[2] not in pool.protected_slot_ids for choice in t._choices(r,'port'))


def test_competing_tool_cohorts_cannot_share_a_lease_slot():
    from fab_sim.manufacturing import SetupState
    from test_manufacturing_facade import place
    pool,t,m,s,c,lots=lease_fixture()
    m.tools['u']={'id':'u','tool_group_id':'A','rack_slots':['u:0','u:1']}
    m.setups['u']=SetupState('A',m.setups['t'].transitions)
    for i in range(2):
        sid=f'u:{i}';lid=f'other:{i}';carrier=lid+':carrier'
        c.slots[sid]={'id':sid,'kind':'rack','equipment_id':'u','carrier_id':None,'reservation':None}
        lots[lid]={'route_id':'r','priority':10,'wafer_count':25,'carrier_id':carrier,'admitted_at_us':0}
        m.enter_visit(lid,'2');place(c,carrier,sid)
    c.slots['u:port']={'id':'u:port','kind':'port','equipment_id':'u','carrier_id':None,'reservation':None}
    t.docks['u:port']={'id':'u:port','equipment_id':'u','kind':'port','role':'BOTH','rail_id':'a','offset_um':450}
    assert pool.dispatch()
    assert len(pool.records)==1 and pool.describe()[-1]['tool_id']=='u'
    assert pool.describe()[-1]['conflicting_leases']==['clearance:00000001']
    before=deepcopy(c.slots);assert not pool.dispatch();assert c.slots==before


def test_unstarted_stale_lease_releases_only_its_own_slots():
    pool,t,m,s,c,lots=lease_fixture();assert pool.dispatch()
    lots['low']['current_visit_id']=None;lots['hot']['current_visit_id']=None
    assert pool.dispatch()
    assert next(iter(pool.records.values()))['status']=='cancelled'
    assert all(c.slots[sid]['reservation'] is None for sid in pool.protected_slot_ids)
    assert c.locations['clow']=='rack' and c.locations['chot']=='rack-extra:1'


@pytest.mark.parametrize('isolated',[False,True])
def test_clearance_batches_routing_queries_without_changing_directed_selection(monkeypatch,isolated):
    import fab_sim.routing as routing
    from fab_sim.clearance import clearance_requirements
    m,_=inputs()
    m['model']['routes'][0]['steps'][0].update(batch_min=25,batch_max=25)
    t=generate({'counts':{'A':3,'B':17},'improvement_limit':1})
    model=t['model'];buffers=sorted((d for d in model['docks'] if d['kind']=='track_buffer'),key=lambda d:d['id'])
    if isolated:
        rail=deepcopy(model['rails'][0]);rail.update(id='isolated',from_node='island:in',to_node='island:out')
        model['rails'].append(rail);buffers[0]['rail_id']='isolated';buffers[0]['offset_um']=rail['length_um']//2
    vehicle=model['vehicles'][0]['type_id']
    queried_origins=set()
    def reachable(a,b):
        queried_origins.add(a['id'])
        try:routing.free_flow_path(model,a,b,vehicle);return True
        except ValueError as error:
            if str(error)!='unreachable directed destination':raise
            return False
    constraints=[]
    for tool in model['equipment']:
        if tool['tool_group_id']!='A':continue
        ins=[d for d in model['docks'] if d.get('equipment_id')==tool['id'] and d.get('role','BOTH') in ('IN','BOTH')]
        outs=[d for d in model['docks'] if d.get('equipment_id')==tool['id'] and d.get('role','BOTH') in ('OUT','BOTH')]
        constraints.append({d['id'] for d in buffers if any(reachable(a,d) for a in outs) and any(reachable(d,b) for b in ins)})
    from itertools import combinations
    expected=next(list(pair) for pair in combinations([d['id'] for d in buffers],2) if all(set(pair)<=valid for valid in constraints))
    counts={'single':0,'batch':0};single=routing.free_flow_path;batch=routing.free_flow_costs
    def counted_single(*args,**kwargs):counts['single']+=1;return single(*args,**kwargs)
    def counted_batch(*args,**kwargs):counts['batch']+=1;return batch(*args,**kwargs)
    monkeypatch.setattr(routing,'free_flow_path',counted_single)
    monkeypatch.setattr(routing,'free_flow_costs',counted_batch)
    result=clearance_requirements(m,t)
    assert result['protected_slot_ids']==expected and not result['findings']
    assert counts['single']+counts['batch']<=len(queried_origins)
