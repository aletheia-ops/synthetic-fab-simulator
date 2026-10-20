"""Finite single-carrier slots, atomic claims and explicit custody transitions."""
from .topology import index
from contextlib import contextmanager
from .resource_changes import ResourceChange


def _owner(owner_id):
    if not isinstance(owner_id,str) or not owner_id: raise ValueError('claim requires a nonempty owner ID')


class Custody:
    def __init__(self,slots,on_change=None):
        self.slots={key:{**record,'carrier_id':None,'reservation':None}
                    for key,record in index(slots,'custody slots').items()}
        self.locations={}; self.on_change=on_change or (lambda:None)
        self._notification_depth=0; self._changed=False; self._listeners=[]
        # Derived change counter: any slot or carrier change bumps it (cache key).
        self.version=0
        self._resource_listeners=[];self._resource_pending_slots=set();self._resource_pending_carriers=set()

    def subscribe(self, listener):
        if not callable(listener): raise ValueError('custody listener must be callable')
        self._listeners.append(listener)

    def subscribe_resources(self,listener):
        if not callable(listener):raise ValueError('custody resource listener must be callable')
        self._resource_listeners.append(listener)

    def _changed_now(self):
        change=ResourceChange(slot_ids=frozenset(self._resource_pending_slots),
                              carrier_ids=frozenset(self._resource_pending_carriers))
        self._resource_pending_slots.clear();self._resource_pending_carriers.clear()
        listeners=tuple(self._resource_listeners)
        self.on_change()
        for listener in tuple(self._listeners): listener()
        for listener in listeners:listener(change)

    def _notify(self,slot_ids,carrier_ids=()):
        self.version+=1
        self._resource_pending_slots.update(slot_ids)
        self._resource_pending_carriers.update(carrier_ids)
        self._resource_pending_carriers.update(self.slots[sid]['carrier_id'] for sid in slot_ids
                                              if self.slots[sid]['carrier_id'] is not None)
        if self._notification_depth: self._changed=True
        else: self._changed_now()

    @contextmanager
    def batch_notifications(self):
        """Only complete committed transitions notify; failures may be partial."""
        self._notification_depth+=1
        try:
            yield
        except BaseException:
            self._changed=False
            self._resource_pending_slots.clear();self._resource_pending_carriers.clear()
            raise
        finally:
            self._notification_depth-=1
            if self._notification_depth==0 and self._changed:
                self._changed=False; self._changed_now()

    def reserve(self,slot_id: str,owner_id: str) -> bool:
        return self.reserve_many([slot_id],owner_id)

    def reserve_many(self,destinations,owner_id,origins=None,reservation_owners=None):
        _owner(owner_id)
        origins=origins or {}; destinations=list(destinations)
        if len(destinations)!=len(set(destinations)) or set(destinations).intersection(origins):
            raise ValueError('duplicate/contradictory slot claim')
        ids=destinations+list(origins)
        reservation_owners=reservation_owners or {}
        if set(reservation_owners)-set(ids): raise ValueError('prior owner for unclaimed slot')
        for prior in reservation_owners.values(): _owner(prior)
        if any(key not in self.slots for key in ids): raise ValueError('unknown custody slot')
        for key in ids:
            slot=self.slots[key]
            if key in reservation_owners:
                if slot['reservation']!=reservation_owners[key]: return False
            elif slot['reservation'] not in (None,owner_id): return False
            if key in origins:
                if slot['carrier_id']!=origins[key] or self.locations.get(origins[key])!=key: return False
            elif slot['carrier_id'] is not None: return False
        changed=[]
        for key in ids:
            if self.slots[key]['reservation']!=owner_id:
                self.slots[key]['reservation']=owner_id; changed.append(key)
        if changed: self._notify(changed)
        return True

    def release(self,slot_id,owner_id):
        _owner(owner_id)
        slot=self.slots[slot_id]
        if slot['reservation']!=owner_id: raise ValueError('slot release by nonowner')
        slot['reservation']=None; self._notify((slot_id,))

    def place(self,carrier_id,slot_id,owner_id):
        _owner(owner_id)
        slot=self.slots[slot_id]
        if not isinstance(carrier_id,str) or not carrier_id or carrier_id in self.locations:
            raise ValueError('missing/duplicate carrier identity')
        if slot['carrier_id'] is not None or slot['reservation']!=owner_id:
            raise ValueError('placement requires empty reserved slot')
        slot.update(carrier_id=carrier_id,reservation=None); self.locations[carrier_id]=slot_id; self._notify((slot_id,),(carrier_id,))

    def move(self,carrier_id: str,origin_id: str,destination_id: str,owner_id: str) -> None:
        _owner(owner_id)
        origin=self.slots[origin_id]; destination=self.slots[destination_id]
        if (self.locations.get(carrier_id)!=origin_id or origin['carrier_id']!=carrier_id
                or origin['reservation']!=owner_id or destination['carrier_id'] is not None
                or destination['reservation']!=owner_id):
            raise ValueError('custody move lacks origin/destination claims or matching carrier')
        origin.update(carrier_id=None,reservation=None)
        destination.update(carrier_id=carrier_id,reservation=None)
        self.locations[carrier_id]=destination_id; self._notify((origin_id,destination_id),(carrier_id,))

    def remove(self,carrier_id,slot_id,owner_id):
        _owner(owner_id)
        slot=self.slots[slot_id]
        if (self.locations.get(carrier_id)!=slot_id or slot['carrier_id']!=carrier_id
                or slot['reservation']!=owner_id): raise ValueError('removal requires matching carrier/origin claim')
        slot.update(carrier_id=None,reservation=None); del self.locations[carrier_id]; self._notify((slot_id,),(carrier_id,))

    def available(self,slot_id):
        slot=self.slots[slot_id]
        return slot['carrier_id'] is None and slot['reservation'] is None

    def can_exchange(self, carrier_id, origin_id, destination_id, owner_id, destination_owner=None, origin_owner=None):
        """Preflight an equipment move, including an explicit admission handoff."""
        _owner(owner_id)
        if destination_owner is not None:
            _owner(destination_owner)
        if origin_owner is not None:
            _owner(origin_owner)
        if origin_id == destination_id:
            raise ValueError('exchange requires distinct slots')
        origin, destination = self.slots[origin_id], self.slots[destination_id]
        allowed = (None, owner_id) if destination_owner is None else (destination_owner, owner_id)
        return (self.locations.get(carrier_id) == origin_id and origin['carrier_id'] == carrier_id
                and origin['reservation'] in ((None, owner_id) if origin_owner is None else (origin_owner, owner_id))
                and destination['carrier_id'] is None and destination['reservation'] in allowed)

    def claim_exchange(self, carrier_id, origin_id, destination_id, owner_id, destination_owner=None, origin_owner=None):
        if not self.can_exchange(carrier_id, origin_id, destination_id, owner_id, destination_owner, origin_owner):
            return False
        origin, destination = self.slots[origin_id], self.slots[destination_id]
        changed = [sid for sid in (origin_id,destination_id) if self.slots[sid]['reservation'] != owner_id]
        origin['reservation'] = destination['reservation'] = owner_id
        if changed:
            self._notify(changed,(carrier_id,))
        return True
