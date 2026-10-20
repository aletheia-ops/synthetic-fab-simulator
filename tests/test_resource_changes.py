"""Committed resource notifications retain physical and legacy semantics."""
from dataclasses import FrozenInstanceError
import pytest
from test_custody import custody


def test_notification_unions_slots_and_carriers_at_commit_boundary():
    from fab_sim.resource_changes import ResourceChange
    c=custody();changes=[];legacy=[]
    c.subscribe_resources(changes.append);c.subscribe(lambda:legacy.append(dict(c.locations)))
    with c.batch_notifications():
        c.reserve('entry:0','a');c.place('carrier','entry:0','a')
        with c.batch_notifications():
            c.reserve_many(['port'],'move',origins={'entry:0':'carrier'})
            c.move('carrier','entry:0','port','move')
        assert changes==[] and legacy==[]
    assert changes==[ResourceChange(slot_ids=frozenset({'entry:0','port'}),carrier_ids=frozenset({'carrier'}))]
    assert legacy==[{'carrier':'port'}]
    with pytest.raises(FrozenInstanceError):changes[0].slot_ids=frozenset()


@pytest.mark.parametrize('action',['reserve','release','place','move','remove','claim_exchange'])
def test_mutation_reports_complete_resource_identity(action):
    c=custody();c.reserve('entry:0','a')
    if action!='place':c.place('carrier','entry:0','a')
    if action=='release':c.reserve_many([],'owner',origins={'entry:0':'carrier'})
    elif action=='remove':c.reserve_many([],'owner',origins={'entry:0':'carrier'})
    elif action=='move':c.reserve_many(['port'],'owner',origins={'entry:0':'carrier'})
    changes=[];c.subscribe_resources(changes.append)
    if action=='reserve':c.reserve_many([],'owner',origins={'entry:0':'carrier'})
    elif action=='release':c.release('entry:0','owner')
    elif action=='place':c.place('carrier','entry:0','a')
    elif action=='move':c.move('carrier','entry:0','port','owner')
    elif action=='remove':c.remove('carrier','entry:0','owner')
    else:assert c.claim_exchange('carrier','entry:0','port','owner')
    assert len(changes)==1 and changes[0].carrier_ids==frozenset({'carrier'})
    assert changes[0].slot_ids==frozenset({'entry:0','port'} if action in ('move','claim_exchange') else {'entry:0'})


def test_noop_and_failed_claims_do_not_publish():
    c=custody();c.reserve('port','a');changes=[];c.subscribe_resources(changes.append)
    assert c.reserve('port','a')
    assert not c.reserve_many(['entry:0','port'],'b')
    with pytest.raises(ValueError):c.move('missing','entry:0','port','b')
    assert changes==[]


def test_reservation_owner_handoff_reports_occupied_carrier():
    c=custody();c.reserve('entry:0','a');c.place('carrier','entry:0','a')
    c.reserve_many([],'prior',origins={'entry:0':'carrier'})
    changes=[];c.subscribe_resources(changes.append)
    assert c.reserve_many([],'next',origins={'entry:0':'carrier'},reservation_owners={'entry:0':'prior'})
    assert changes[0].carrier_ids==frozenset({'carrier'})


def test_failed_batch_retains_existing_partial_mutation_without_notifications():
    c=custody();changes=[];c.subscribe_resources(changes.append)
    with pytest.raises(RuntimeError):
        with c.batch_notifications():
            c.reserve('entry:0','partial');raise RuntimeError('domain failure')
    assert changes==[] and c.slots['entry:0']['reservation']=='partial'
    c.reserve('port','next')
    assert changes[0].slot_ids==frozenset({'port'})


def test_reentrant_legacy_callback_cannot_corrupt_resource_snapshot():
    c=custody();changes=[]
    def legacy():
        if c.available('port'):c.reserve('port','nested')
    c.subscribe(legacy);c.subscribe_resources(changes.append)
    c.reserve('entry:0','outer')
    assert {r.slot_ids for r in changes}=={frozenset({'entry:0'}),frozenset({'port'})}


def test_dirty_worklist_unions_clears_and_full_mark_dominates():
    from fab_sim.resource_changes import DirtyResources
    dirty=DirtyResources();assert dirty.take() is None
    dirty.mark(['a']);dirty.mark(['b','a']);assert dirty.take()==frozenset({'a','b'})
    assert dirty.take()==frozenset()
    dirty.mark(['c']);dirty.mark();dirty.mark(['d']);assert dirty.take() is None
    dirty.mark(['new']);assert dirty.take()==frozenset({'new'})
    with pytest.raises(ValueError):dirty.mark(['valid',''])
    assert dirty.take()==frozenset()


def test_change_rejects_mutable_or_invalid_identity_sets():
    from fab_sim.resource_changes import ResourceChange
    with pytest.raises(ValueError):ResourceChange(slot_ids={'mutable'})
    with pytest.raises(ValueError):ResourceChange(carrier_ids=frozenset({''}))

