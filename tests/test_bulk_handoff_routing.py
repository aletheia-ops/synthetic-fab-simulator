from copy import deepcopy
from test_handoffs import exit_fixture
from test_transfers import factory
from test_manufacturing_facade import place


def test_handoff_builds_only_selected_vehicle_and_loaded_routes(monkeypatch):
    plans,t,m,s,c,lots,vehicles,events,did=exit_fixture()
    for i in range(20):
        vid=f'other:{i:02}'
        vehicles[vid]={**vehicles['v1'],'id':vid,'offset_um':0,'carrier_slot_id':'vehicle:'+vid}
        c.slots['vehicle:'+vid]={'id':'vehicle:'+vid,'kind':'vehicle','carrier_id':None,'reservation':None}
    calls=[];original=t._path
    def counted(start,end):calls.append((dict(start),dict(end)));return original(start,end)
    monkeypatch.setattr(t,'_path',counted)
    assert plans.prepare(did,'rack')
    assert plans.records[did]['vehicle_id']=='other:00'
    assert len(calls)==2
    assert t.demands[did]['loaded_path']['seconds']==original(t._pose('port'),t._pose('exit'))['seconds']
    assert t.demands[did]['loaded_path']['sublinks']


def test_cost_only_destination_choices_preserve_ranking_and_live_capacity(monkeypatch):
    t,m,s,c,lots,vehicles,events=factory()
    vid=m.enter_visit('low','1');place(c,'clow','entry');did=t.request('clow',vid,('port',))
    r=t.demands[did];expected=t._choices(r,'entry')
    def forbidden(*args):raise AssertionError('materialized a candidate full path')
    monkeypatch.setattr(t,'_path',forbidden)
    costs=t._choices(r,'entry',with_paths=False)
    assert [row[:4]+row[5:] for row in costs]==[row[:4]+row[5:] for row in expected]
    assert all(row[4] is None for row in costs)
    c.slots['rack']['reservation']='another-owner'
    overflow=t._choices(r,'entry',with_paths=False)
    assert len(overflow)==1 and overflow[0][2]=='buffer' and overflow[0][-1] is True


def test_bulk_handoff_excludes_unavailable_nearest_vehicle():
    plans,t,m,s,c,lots,vehicles,events,did=exit_fixture()
    vehicles['nearest']={**vehicles['v1'],'id':'nearest','offset_um':400,
        'carrier_slot_id':'vehicle:nearest','available':False}
    c.slots['vehicle:nearest']={'id':'vehicle:nearest','kind':'vehicle','carrier_id':None,'reservation':None}
    assert plans.prepare(did,'rack')
    assert plans.records[did]['vehicle_id']=='v1'
    assert c.slots['vehicle:nearest']['reservation'] is None
