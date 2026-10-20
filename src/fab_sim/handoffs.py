"""Committed onward transport before finite rack-to-port evacuation."""
from copy import deepcopy
from .native_actions import native_action


POLICY='reserved_handoff_clearance_v2'


class HandoffPlans:
    def __init__(self,transfers,clearance,emit):
        if transfers.handoffs is not None or transfers.clearance is not clearance:
            raise ValueError('handoffs require one shared clearance coordinator')
        self.transfers,self.emit=transfers,emit;self.records={};self.waits={}
        self._active_records={};self.history_archive=None;self._history_lookup=None
        transfers.handoffs=self

    def _archive_lookup(self):
        if self._history_lookup is None:
            actions=getattr(self,'actions',None)
            lookup=getattr(actions,'history_lookup',None) if actions else None
            if lookup is None:
                from .history_lookup import HistoryLookup
                lookup=HistoryLookup()
                if actions:actions.history_lookup=lookup
            self._history_lookup=lookup
        return self._history_lookup

    def _lease_member(self,lease,visit):
        return next(x for x in self.transfers.clearance.records[lease]['members'] if x['visit_id']==visit)

    def prepare(self,demand_id,origin_id):
        if demand_id not in self.transfers.demands:return False
        t=self.transfers;m=t.manufacturing;c=t.custody;r=t.demands[demand_id]
        if demand_id in self.records or r['status']!='pending' or not t._visit_valid(r):return False
        slot=c.slots[origin_id];tid=slot.get('equipment_id')
        if (tid not in m.tools or slot['kind']!='rack' or c.locations.get(r['carrier_id'])!=origin_id
                or slot['reservation'] is not None or r['carrier_id'] in m.claims):return False
        if (not m.tools[tid].get('available',True) or m.causes[tid]) and not t.exchange.down_tool_output:
            self.waits[demand_id]={'reason':'source_equipment_unavailable','source_rack_id':origin_id}
            return False
        def free_ports():
            candidates=(t.resource_index.output_ports(tid) if getattr(t,'resource_index',None) else
                (sid for sid,d in t.docks.items() if d.get('equipment_id')==tid and d.get('role') in ('OUT','BOTH')))
            return sorted(sid for sid in candidates if c.available(sid) and not t.exchange.port_causes.get(sid))
        lease=t.clearance.admission(r['visit_id']) if r['visit_id'] is not None else None
        if lease:
            member=self._lease_member(lease[1],r['visit_id'])
            if not t.clearance._compatible(member,lease[0]):return False
        neg=t._neg
        # The recorded wait copies the clearance wait of the source tool, which
        # follows buffer capacity: a hit also needs that record unchanged.
        clearance_wait=t.clearance.waits.get(tid)
        # The free output ports are needed only on a miss (or to verify a hit).
        if (neg.hit(demand_id,origin_id,check=lambda:self._verify_empty(demand_id,origin_id,r,tid,free_ports(),lease),
                    same=lambda payload:payload[1]==clearance_wait)):
            self.waits[demand_id]=dict(neg.payload(demand_id)[0]);return False
        stamp=neg.begin()
        options=self._options(demand_id,origin_id,r,tid,free_ports(),lease)
        if not options:
            neg.used_buffers=True
            neg.store(r,origin_id,stamp,(tid,),(dict(self.waits[demand_id]),
                deepcopy(t.clearance.waits.get(tid))));return False
        return self._commit(demand_id,origin_id,r,options,lease)

    def _verify_empty(self,demand_id,origin_id,r,tid,ports,lease):
        cached=self.transfers._neg.payload(demand_id)[0]
        options=self._options(demand_id,origin_id,r,tid,ports,lease)
        if not options and self.waits[demand_id]!=cached:
            raise RuntimeError(f'negative cache stale hand-off wait for {demand_id}')
        return options

    def _options(self,demand_id,origin_id,r,tid,ports,lease):
        t=self.transfers;c=t.custody
        self.waits[demand_id]={'reason':'destination_admission_unavailable','source_rack_id':origin_id}
        options=[];destinations=t._destination_candidates(r) if ports else None
        for port in ports:
            choices=t._choices(r,port,with_paths=False,_candidates=destinations)
            if lease:
                sid,owner=lease;member=self._lease_member(owner,r['visit_id'])
                if (t.clearance.output_unowned(r['visit_id'])
                        and member['status']=='planned' and member['origin_id']==origin_id
                        and c.slots[sid]['reservation']==owner and c.slots[sid]['carrier_id'] is None):
                    costs=t.free_flow_costs(t._pose(port),{sid:t._pose(sid)})
                    if sid in costs:
                        # A complete leased clearance takes precedence over ordinary
                        # overflow; direct legal delivery remains preferable.
                        direct=[choice for choice in choices if not choice[-1]]
                        choices=direct or [(costs[sid],'',sid,(None,None),None,True)]
            for choice in choices:
                options.append((choice[-1],choice[0],choice[1],choice[2],port,choice))
        if not options:
            index=getattr(t,'resource_index',None)
            ordinary=(index.buffer_candidates(None,protected=False) if index is not None else
                [sid for sid,d in t.docks.items() if d['kind']=='track_buffer' and sid not in t.clearance.protected_slot_ids])
            if tid in t.clearance.waits:
                self.waits[demand_id].update(reason='clearance_lease_unavailable',**t.clearance.waits[tid])
            elif ordinary and not any(c.available(sid) for sid in ordinary):
                self.waits[demand_id]['reason']='ordinary_overflow_exhausted'
        else:self.waits[demand_id]['reason']='handoff_resource_unavailable'
        return options

    def _commit(self,demand_id,origin_id,r,options,lease):
        t=self.transfers;c=t.custody
        loads=t.destination_loads(r,[option[-1][1] for option in options])
        cost=t.congested_cost_key(len(options))
        for _,_,_,_,port,choice in sorted(options,key=lambda option:(option[0],loads[option[-1][1]],cost(option[1],option[3]),*option[2:5])):
            _,_,destination,(rack,rack_owner),loaded,overflow=choice
            if t.dispatch_metering and t._meter_hold(r,destination):continue
            if t.busy_vehicle_cap and t._cap_hold(r):return False
            lease_owner=lease[1] if lease and destination==lease[0] else None
            eligible={vid:t.vehicles[vid] for vid in t.available_vehicle_ids()}
            if not eligible:t._vehicle_starved=True;continue
            costs=t._router().costs_to(eligible,t._pose(port))
            if not costs:t._vehicle_starved=True;continue
            _,vehicle_id=min((seconds,vid) for vid,seconds in costs.items())
            vehicle=t.vehicles[vehicle_id]
            empty=t._path(vehicle,t._pose(port))
            loaded=t._path(t._pose(port),t._pose(destination))
            if empty is None or loaded is None:raise RuntimeError('ranked handoff path is unreachable')
            dock_claims=sorted({t.slot_docks[sid] for sid in (port,destination) if sid in t.slot_docks})
            destinations=[port,destination,vehicle['carrier_slot_id'],*dock_claims]
            if rack and rack_owner==demand_id:destinations.append(rack)
            prior={destination:lease_owner} if lease_owner else {}
            with c.batch_notifications():
                # Existing assembly/campaign rack claims remain with their owner.
                if rack and rack_owner!=demand_id and (c.slots[rack]['reservation']!=rack_owner
                        or c.slots[rack]['carrier_id'] is not None):continue
                if not c.reserve_many(destinations,demand_id,origins={origin_id:r['carrier_id']},reservation_owners=prior):continue
                if vehicle['movement_id']:t.traffic.yield_for_assignment(vehicle_id)
                vehicle['assignment_id']=demand_id
                r.update(status='output_reserved',origin_id=port,destination_id=destination,rack_slot_id=rack,
                    rack_owner=rack_owner,vehicle_id=vehicle_id,overflow=overflow,dock_claims=dock_claims,
                    pickup_pose=t._pose(port),drop_pose=t._pose(destination),loaded_path=loaded)
                record={'demand_id':demand_id,'source_rack_id':origin_id,'output_port_id':port,
                    'destination_id':destination,'rack_slot_id':rack,'rack_owner':rack_owner,'vehicle_id':vehicle_id,
                    'dock_claims':list(dock_claims),'lease_id':lease_owner,'phase':'output_reserved','cancel_requested':False}
                self.records[demand_id]=record
                self._active_records[demand_id]=record
                if lease_owner:self._lease_member(lease_owner,r['visit_id'])['status']='in_transit'
                if overflow:t.overflow_visits.add((r['carrier_id'],r['visit_id']))
                eid=demand_id+':output';r['output_exchange_id']=eid
                t.exchange.request(eid,r['carrier_id'],origin_id,port,origin_owner=demand_id,
                    reservation_owner=demand_id,return_owner=demand_id,
                    on_start=native_action(self,'handoff:start',demand_id,None,lambda did=demand_id:self.output_started(did)),
                    on_complete=native_action(self,'handoff:complete',demand_id,None,lambda did=demand_id:self.output_completed(did)),
                    valid=native_action(self,'transfer:admission-valid',demand_id,None,lambda r=r:t._admission_valid(r)))
            self.waits.pop(demand_id,None)
            self.emit('handoff_committed',dict(record));return True
        return False

    def output_started(self,demand_id):
        record=self.records[demand_id];record['phase']='output_active'
        self.transfers.demands[demand_id]['status']='output_active'

    def output_completed(self,demand_id):
        t=self.transfers;r=t.demands[demand_id];record=self.records[demand_id]
        record['phase']='pickup_ready';r['status']='assigned'
        empty=t._path(t.vehicles[r['vehicle_id']],r['pickup_pose'])
        if empty is None:raise RuntimeError('committed pickup path disappeared')
        t._movement(r,'pickup',empty)
        self.emit('handoff_pickup_ready',dict(record))

    def completed(self,demand_id):
        record=self.records.get(demand_id)
        if record:
            record['phase']='completed';self._active_records.pop(demand_id,None)
            self.emit('handoff_completed',dict(record))

    def cancel(self,demand_id):
        if demand_id not in self.records:
            raise ValueError('handoff already terminal or unknown')
        t=self.transfers;c=t.custody;r=t.demands[demand_id];record=self.records[demand_id]
        if record['phase'] in ('completed','cancelled'):raise ValueError('handoff already terminal')
        if record['phase'] not in ('output_reserved','output_active'):
            record['cancel_requested']=True;self.emit('handoff_cancel_deferred',dict(record));return 'deferred'
        eid=r['output_exchange_id'];exchange=t.exchange.requests[eid]
        owned=sorted({record['source_rack_id'],record['output_port_id'],r['destination_id'],
            t.vehicles[r['vehicle_id']]['carrier_slot_id'],*r.get('dock_claims',()),
            *([r['rack_slot_id']] if r['rack_slot_id'] and r['rack_owner']==demand_id else [])})
        expected={sid:demand_id for sid in owned}
        if exchange['status']=='active':
            expected[record['source_rack_id']]=expected[record['output_port_id']]=exchange['owner_id']
        if (t.vehicles[r['vehicle_id']]['assignment_id']!=demand_id
                or any(c.slots[sid]['reservation']!=owner for sid,owner in expected.items())):
            raise RuntimeError('handoff cancellation ownership changed')
        with c.batch_notifications():
            t.exchange.cancel(eid)
            for sid in owned:c.release(sid,demand_id)
            if record['lease_id']:
                c.reserve(r['destination_id'],record['lease_id'])
                self._lease_member(record['lease_id'],r['visit_id'])['status']='planned'
            t.vehicles[r['vehicle_id']]['assignment_id']=None
            r['status']='cancelled';record['phase']='cancelled';record['cancel_requested']=True
            del self._active_records[demand_id]
            del t.active[r['carrier_id']]
            if r['overflow']:t.overflow_visits.discard((r['carrier_id'],r['visit_id']))
        self.emit('handoff_cancelled',dict(record));t.dirty();return 'cancelled'

    def current_records(self):
        """Snapshot live plans in their original commitment order."""
        return tuple(self._active_records.values())

    def describe(self,*,active_only=False):
        records=((did,self.records[did]) for did in self.transfers.active.values() if did in self.records) if active_only else self.records.items()
        return [dict(record) for _,record in sorted(records)
                if not active_only or record['phase'] not in ('completed','cancelled')]
