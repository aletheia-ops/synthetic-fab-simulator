from copy import deepcopy
import pytest


def custody():
    from fab_sim.custody import Custody
    return Custody([{'id':'entry:0','kind':'entry'},{'id':'rack:0','kind':'rack'},{'id':'port','kind':'port'}])


def test_claims_are_atomic_and_conflicting_destinations_do_not_partially_reserve():
    c=custody(); assert c.reserve('port','other')
    assert not c.reserve_many(['entry:0','port'],'transfer')
    assert c.slots['entry:0']['reservation'] is None
    assert c.reserve('entry:0','transfer') and c.reserve('entry:0','transfer')


def test_custody_move_requires_claims_and_preserves_single_location():
    c=custody(); c.reserve('entry:0','admit'); c.place('lot:carrier','entry:0','admit')
    with pytest.raises(ValueError): c.move('lot:carrier','entry:0','port','transfer')
    assert c.reserve_many(['port'],'transfer',origins={'entry:0':'lot:carrier'})
    c.move('lot:carrier','entry:0','port','transfer')
    assert c.locations=={'lot:carrier':'port'}
    assert c.slots['entry:0']['carrier_id'] is None and c.slots['port']['carrier_id']=='lot:carrier'
    assert c.slots['port']['reservation'] is None


def test_failed_move_has_no_partial_mutation_and_duplicate_carrier_rejects():
    c=custody(); c.reserve('entry:0','admit'); c.place('carrier','entry:0','admit')
    c.reserve('port','other'); before=deepcopy(c.slots)
    with pytest.raises(ValueError): c.move('carrier','entry:0','port','transfer')
    assert c.slots==before
    c.reserve('rack:0','admit')
    with pytest.raises(ValueError,match='carrier'): c.place('carrier','rack:0','admit')


def test_full_entry_retains_intent_and_due_date_until_physical_admission():
    from fab_sim.workload import admit
    c=custody(); c.reserve('entry:0','first'); c.place('first-carrier','entry:0','first')
    intent={'lot_id':'a:0','scheduled_at_us':0,'due_at_us':20,'wafer_count':25,'product_id':'P3','route_id':'R3'}
    state={'custody':c,'entry_slots':['entry:0'],'lots':{},'now_us':5}
    before=deepcopy(intent)
    assert not admit(intent,state) and state['lots']=={} and intent==before
    assert c.reserve_many([],'exit',origins={'entry:0':'first-carrier'})
    c.remove('first-carrier','entry:0','exit')
    assert admit(intent,state)
    lot=state['lots']['a:0']
    assert lot['admitted_at_us']==5 and lot['admission_delay_us']==5 and lot['due_at_us']==20
    assert c.locations=={'a:0:carrier':'entry:0'}


def test_remove_requires_origin_owner_and_frees_finite_slot():
    c=custody(); c.reserve('entry:0','admit'); c.place('carrier','entry:0','admit')
    with pytest.raises(ValueError): c.remove('carrier','entry:0','exit')
    assert c.reserve_many([],'exit',origins={'entry:0':'carrier'})
    c.remove('carrier','entry:0','exit')
    assert not c.locations and c.slots['entry:0']['carrier_id'] is None


def test_admission_change_notification_observes_complete_lot_and_carrier():
    from fab_sim.workload import admit
    c=custody(); snapshots=[]
    state={'custody':c,'entry_slots':['entry:0'],'lots':{},'now_us':0}
    c.on_change=lambda:snapshots.append((set(state['lots']),set(c.locations)))
    assert admit({'lot_id':'a:0','scheduled_at_us':0,'due_at_us':20},state)
    assert snapshots==[({'a:0'},{'a:0:carrier'})]


@pytest.mark.parametrize('action',['place','move','remove','release'])
def test_unreserved_sentinel_cannot_bypass_claim_owner(action):
    c=custody()
    if action in ('move','remove'):
        c.reserve('entry:0','admit'); c.place('carrier','entry:0','admit')
    before=deepcopy(c.slots)
    with pytest.raises(ValueError,match='owner'):
        if action=='place': c.place('carrier','entry:0',None)
        elif action=='move': c.move('carrier','entry:0','port',None)
        elif action=='remove': c.remove('carrier','entry:0',None)
        else: c.release('entry:0',None)
    assert c.slots==before


def test_multi_slot_prior_owner_handoff_is_atomic():
    c=custody();c.reserve('rack:0','assembly');before=deepcopy(c.slots)
    assert not c.reserve_many(['port','rack:0'],'handoff',reservation_owners={'rack:0':'wrong'})
    assert c.slots==before and not c.locations
    assert c.reserve_many(['port','rack:0'],'handoff',reservation_owners={'rack:0':'assembly'})
    assert c.slots['port']['reservation']==c.slots['rack:0']['reservation']=='handoff'


def test_prior_owner_map_rejects_unclaimed_slots_and_never_steals_implicit_claims():
    c=custody();c.reserve('rack:0','foreign');before=deepcopy(c.slots)
    with pytest.raises(ValueError):
        c.reserve_many(['port'],'handoff',reservation_owners={'rack:0':'foreign'})
    assert c.slots==before
    assert not c.reserve_many(['port','rack:0'],'handoff')
    assert c.slots==before
    assert not c.reserve_many(['port'],'handoff',reservation_owners={'port':'missing'})
    assert c.slots==before
