"""Finite exchanges and transfer assignment; Traffic owns movement permission."""
from .native_actions import native_action

from .service import SerializedHandler, dispatch_service
from heapq import heapify, heappop, heappush

ASSIGNMENT_POLICY = 'live_lot_count_then_free_flow_v1'


class EquipmentExchange:
    """One physical port/rack lane per tool, sharing manufacturing carrier claims.

    The transfer pass owns dispatch. A prior downstream admission owner may
    explicitly hand off its rack claim; OHT custody is outside this component.
    """
    def __init__(self, manufacturing, emit, *, exchange_time_us=None):
        self.manufacturing, self.emit = manufacturing, emit
        self.scheduler, self.custody = manufacturing.scheduler, manufacturing.custody
        self.requests, self.handlers, self.durations = {}, {}, {}
        self.history_archive=None;self._history_lookup=None
        self.port_causes={}
        # Down-tool output (declared project policy, docs/settings.md): a
        # carrier may leave a broken or maintained tool through its port.
        self.down_tool_output=False
        self._pending_tools=set();self._dispatch_frontier=None
        overrides = exchange_time_us or {}
        if set(overrides) - set(manufacturing.tools):
            raise ValueError('unknown exchange duration tool')
        for tid in manufacturing.tools:
            duration = overrides.get(tid, 1_000_000)
            if type(duration) is not int or duration <= 0:
                raise ValueError('exchange duration must be a positive integer')
            self.durations[tid] = duration
            self.handlers[tid] = SerializedHandler(self.scheduler,'exchange:'+tid,
                manufacturing.claims,self.dirty,emit)
        self.rebuild_derived()
        self.custody.subscribe(self.dirty)

    def dirty(self):
        self.scheduler.mark_dirty('transfer')

    def _lot(self, carrier):
        lookup=getattr(self.manufacturing,'carrier_lot_lookup',None)
        if lookup is not None:return lookup(carrier)
        matches = [(lid,lot) for lid,lot in self.manufacturing.lots.items() if lot.get('carrier_id') == carrier]
        if len(matches) != 1:
            raise ValueError('exchange requires a unique LOT carrier')
        return matches[0]

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

    def _input_valid(self, r):
        m = self.manufacturing
        lot = m.lots[r['lot_id']]
        vid = lot.get('current_visit_id')
        if vid != r['visit_id'] or vid is None:
            return False
        visit = m.rules.visits[vid]
        if (visit['status'] != 'pending' or visit.get('execution_id')
                or m._step(visit)['tool_group_id'] != m.tools[r['tool_id']]['tool_group_id']
                or not m.rules.eligible_tools(vid,[r['tool_id']])):
            return False
        if m._step(visit).get('batch_min'):
            aid = visit.get('assembly_id')
            if not aid or aid != r['reservation_owner']:
                return False
            a = m.batches.assemblies[aid]
            return a['status'] == 'assembling' and a['tool_id'] == r['tool_id'] and any(
                member['carrier_id'] == r['carrier_id'] and member['rack_slot_id'] == r['destination_id']
                and not member['arrived'] for member in a['members'])
        if m.campaigns and not m.campaigns.valid_admission(vid, r['tool_id'],
                r['destination_id'], r['reservation_owner']):
            return False
        return True

    def request(self, request_id, carrier_id, origin_id, destination_id, *, reservation_owner=None, origin_owner=None, return_owner=None, on_start=None, valid=None, on_complete=None):
        on_start=native_action(self,'noop','',None,lambda:None) if on_start is None else on_start
        valid=native_action(self,'true','',None,lambda:True) if valid is None else valid
        on_complete=native_action(self,'noop','',None,lambda:None) if on_complete is None else on_complete
        if (not isinstance(request_id,str) or not request_id or request_id in self.requests
                or not callable(on_complete) or not callable(on_start) or not callable(valid)
                or any(r['carrier_id']==carrier_id and r['status'] in ('pending','active') for r in self.requests.values())):
            raise ValueError('invalid/duplicate carrier exchange request')
        for owner in (reservation_owner,origin_owner,return_owner):
            if owner is not None and (not isinstance(owner,str) or not owner):
                raise ValueError('exchange requires a nonempty owner ID')
        origin, destination = self.custody.slots[origin_id], self.custody.slots[destination_id]
        tid = origin.get('equipment_id')
        if tid not in self.handlers or destination.get('equipment_id') != tid:
            raise ValueError('exchange must stay within one equipment')
        inbound = origin['kind']=='port' and destination['kind']=='rack'
        outbound = origin['kind']=='rack' and destination['kind']=='port'
        if not inbound and not outbound:
            raise ValueError('exchange requires an external port and internal rack')
        port = origin if inbound else destination
        if port.get('role') not in (('IN','BOTH') if inbound else ('OUT','BOTH')):
            raise ValueError('exchange port role forbids direction')
        rack = destination_id if inbound else origin_id
        if rack not in self.manufacturing.tools[tid]['rack_slots']:
            raise ValueError('exchange rack belongs to another tool')
        lid, lot = self._lot(carrier_id)
        r = {'id':request_id,'owner_id':'exchange:'+tid+':'+request_id,'tool_id':tid,
             'carrier_id':carrier_id,'origin_id':origin_id,'destination_id':destination_id,
             'lot_id':lid,'visit_id':lot.get('current_visit_id'),'inbound':inbound,
             'reservation_owner':reservation_owner,'origin_owner':origin_owner,'return_owner':return_owner,'status':'pending','queued_at_us':self.scheduler.now_us,
             'started_at_us':None,'completed_at_us':None,'on_start':on_start,'valid':valid,'on_complete':on_complete}
        if inbound and not self._input_valid(r):
            raise ValueError('input exchange requires eligible committed rack admission')
        self.requests[request_id] = r
        self.handlers[tid].request(request_id,'exchange',[carrier_id],native_action(self,'exchange:duration',request_id,None,lambda:self.durations[tid]),
            native_action(self,'exchange:complete',request_id,None,lambda:self._complete(r)),eligible=native_action(self,'exchange:eligible',request_id,None,lambda:self._eligible(r)),on_start=native_action(self,'exchange:start',request_id,None,lambda:self._start(r)))
        return request_id

    def _eligible(self, r):
        m = self.manufacturing
        return ((m.tools[r['tool_id']].get('available',True) and not m.causes[r['tool_id']]
                 or self.down_tool_output and not r['inbound'])
                and not self.port_causes.get(r['origin_id'] if r['inbound'] else r['destination_id'])
                and r['valid']() and (not r['inbound'] or self._input_valid(r))
                and self.custody.can_exchange(r['carrier_id'],r['origin_id'],r['destination_id'],
                                             r['owner_id'],r['reservation_owner'],r['origin_owner']))

    def _event(self, r):
        return {k:v for k,v in r.items() if not callable(v)}

    def _start(self, r):
        with self.custody.batch_notifications():
            if not self.custody.claim_exchange(r['carrier_id'],r['origin_id'],r['destination_id'],
                                              r['owner_id'],r['reservation_owner'],r['origin_owner']):
                raise RuntimeError('exchange start ownership changed')
            r.update(status='active',started_at_us=self.scheduler.now_us)
            r['on_start']()
        self.emit('carrier_exchange_started',self._event(r))

    def _complete(self, r):
        if (r['status'] != 'active' or self.custody.locations.get(r['carrier_id']) != r['origin_id']
                or any(self.custody.slots[sid]['reservation'] != r['owner_id'] for sid in (r['origin_id'],r['destination_id']))
                or (r['inbound'] and not self._input_valid(r))):
            raise RuntimeError('exchange completion ownership changed')
        with self.custody.batch_notifications():
            self.custody.move(r['carrier_id'],r['origin_id'],r['destination_id'],r['owner_id'])
            if r['return_owner'] is not None:
                if not self.custody.reserve_many([],r['return_owner'],origins={r['destination_id']:r['carrier_id']}):
                    raise RuntimeError('exchange return ownership changed')
            r.update(status='complete',completed_at_us=self.scheduler.now_us)
            r['on_complete']()
            if r['inbound']:
                self.manufacturing.delivered(r['carrier_id'],r['destination_id'])
            else:
                finished = [a for a in self.manufacturing.batches.assemblies.values()
                            if a['status']=='finished' and a['tool_id']==r['tool_id'] and any(
                                member['carrier_id']==r['carrier_id'] and not member['departed'] for member in a['members'])]
                if finished:
                    self.manufacturing.departed(r['carrier_id'])
        self.emit('carrier_exchange_completed',self._event(r))
        self.dirty()

    def rebuild_derived(self):
        self._pending_tools=set();self._dispatch_frontier=None
        for tid,handler in self.handlers.items():
            handler._pending_changed=self._pending_changed
            handler.rebuild_derived()

    def _pending_changed(self,handler):
        tid=handler.id[len('exchange:'):]
        if not handler._pending:
            self._pending_tools.discard(tid)
            return
        self._pending_tools.add(tid)
        frontier=self._dispatch_frontier
        if frontier is not None:
            heap,seen,cursor=frontier
            # The former sorted all-tool pass saw work enqueued on later tools
            # immediately, but did not revisit tools already passed.
            if tid>cursor and tid not in seen:
                seen.add(tid);heappush(heap,tid)

    def dispatch(self):
        heap=list(self._pending_tools);heapify(heap)
        frontier=[heap,set(heap),''];self._dispatch_frontier=frontier
        changed=False
        try:
            while heap:
                tid=heappop(heap);frontier[2]=tid
                if tid in self._pending_tools:
                    changed=dispatch_service(self.handlers[tid]) or changed
            return changed
        finally:
            self._dispatch_frontier=None

    def cancel(self, request_id):
        if request_id not in self.requests:
            raise ValueError('exchange already completed')
        r=self.requests[request_id]
        if r['status'] not in ('pending','active'):raise ValueError('exchange already completed')
        handler=self.handlers[r['tool_id']];phase=handler.requests[request_id]
        active=r['status']=='active'
        if active and (handler.active is not phase or self.manufacturing.claims.get(r['carrier_id'])!=r['owner_id']
                or any(self.custody.slots[sid]['reservation']!=r['owner_id'] for sid in (r['origin_id'],r['destination_id']))):
            raise RuntimeError('exchange cancellation ownership changed')
        with self.custody.batch_notifications():
            if active:
                phase['timer'].cancel();handler.active=None
                del self.manufacturing.claims[r['carrier_id']]
                self.custody.release(r['origin_id'],r['owner_id'])
                self.custody.release(r['destination_id'],r['owner_id'])
                if r['origin_owner'] is not None:
                    if not self.custody.reserve_many([],r['origin_owner'],origins={r['origin_id']:r['carrier_id']}):
                        raise RuntimeError('exchange origin restoration failed')
                if r['reservation_owner'] is not None:self.custody.reserve(r['destination_id'],r['reservation_owner'])
            r['status']='cancelled';handler.set_request_status(request_id,'cancelled')
        self.emit('carrier_exchange_cancelled',self._event(r));self.dirty()


def _vehicle_slot_ids(owner):
    # Vehicles and their carrier slots are fixed for a run.
    cached=getattr(owner,'_vehicle_slots',None);vehicles=owner.vehicles
    if cached is None or cached[0] is not vehicles or len(cached[1])!=len(vehicles):
        cached=(vehicles,tuple(v['carrier_slot_id'] for v in vehicles.values()))
        owner._vehicle_slots=cached
    return cached[1]


class Transfers:
    """Atomic assignment and timed hoisting; Traffic supplies actual arrivals.

    Movement records carry directed paths, never free-flow arrival timers.
    Prepared dock IDs must also identify their finite custody slots. A fleet
    uses one vehicle type so equipment ranking has one loaded-time metric.
    """
    def __init__(self, manufacturing, topology, vehicles, emit, *, exchange_time_us=None, buffer_selection='nearest_origin'):
        from .topology import index
        self.manufacturing, self.scheduler, self.custody = manufacturing, manufacturing.scheduler, manufacturing.custody
        self.model = topology.get('model',topology)
        self.docks = index(self.model['docks'],'transfer docks')
        # Internal stocker slots share a physical service dock, never its storage.
        self.slot_docks = {};physical_docks=set(self.docks)
        for sid,slot in self.custody.slots.items():
            dock_id=slot.get('dock_id')
            if dock_id is None:continue
            dock=self.docks.get(dock_id)
            if (sid in self.docks or dock_id not in physical_docks or dock is None or dock_id not in self.custody.slots
                    or dock['kind'] not in ('entry','exit','stocker')
                    or slot['kind'] not in ('entry','exit','storage')
                    or slot.get('equipment_id')!=dock.get('equipment_id')):
                raise ValueError('invalid internal stocker slot/dock mapping')
            self.slot_docks[sid]=dock_id
            self.docks[sid]={**dock,'id':sid,'dock_id':dock_id}
        self.internal_docks=set(self.slot_docks.values())
        self.vehicles,self._event_sink=vehicles,emit
        self.emit=self._emit
        self._load_dirty=True;self._resident_lots_by_tool={};self._incoming_lots_by_tool={}
        self._tool_loads={};self._incoming_by_tool=None;self._rack_slots_by_tool=None
        self._cost_router=None;self._tool_docks=None;self._tool_docks_key=None;self._destination_layout={};self._sorted_racks={};self._vehicle_slots=None
        self._vehicle_starved=True
        manufacturing.subscribe_resources(self._invalidate_loads)
        types = {v['type_id'] for v in vehicles.values()}
        if len(types) > 1: raise ValueError('mixed vehicle-type assignment is unsupported')
        self.vehicle_type = next(iter(types),self.model['vehicle_types'][0]['id'])
        self.handling = {phase:self.model['policy'][phase+'_us'] for phase in ('pickup','drop')}
        if any(type(x) is not int or x <= 0 for x in self.handling.values()):
            raise ValueError('transfer handling durations must be positive integers')
        for v in vehicles.values():
            slot = self.custody.slots[v['carrier_slot_id']]
            if slot['kind'] != 'vehicle': raise ValueError('OHT requires a finite vehicle carrier slot')
            v.setdefault('assignment_id',None);v.setdefault('movement_id',None)
        self.demands, self.active, self.movements = {}, {}, {}
        self.history_archive=None;self.movements_archive=None;self._history_lookup=None
        self.overflow_visits = set(); self.sequence = 0
        self.clearance=None;self.handoffs=None
        # Overflow buffer choice (declared project policy, docs/settings.md):
        # 'nearest_origin' ranks every ordinary track buffer from the origin;
        # 'next_group_bays' ranks buffers in the bays of the next step's tool
        # group first, then in neighbouring bays, then all buffers.
        if buffer_selection not in ('nearest_origin','next_group_bays'):raise ValueError('unknown buffer selection')
        self.buffer_selection=buffer_selection;self._buffer_tiers={}
        self._super_hot_lots=None
        # Congestion-aware routing (declared project policy, docs/settings.md):
        # None, or seconds added per waiting vehicle on a rail when planning legs.
        self.congestion_routing_s=None
        # Re-plan the loaded leg at pickup completion on current congestion
        # (declared project policy; requires congestion_routing_s).
        self.dispatch_metering=None;self._metered=False;self._meter_waiting=None
        # Busy-vehicle cap (declared policy): None, or the number of vehicles that may
        # hold a transfer at once; further non-super-hot dispatches wait at their origin.
        self.busy_vehicle_cap=None;self._cap_engaged=None
        self.super_hot_expedite=False
        # Down-tool avoidance (declared policy): None, or a downtime length (us);
        # no LOT is delivered to a tool down for a downtime at least this long.
        self.down_tool_avoid_us=None;self.long_downtime=None
        self._open_version=None;self._open_memo={};self._meter_counts={}
        # Negative-result cache (speed only): demands whose destination search
        # found nothing are not searched again until something they read changes.
        self._neg=NegativeCache(self)
        # Congestion-aware destination choice (declared project policy): None, or
        # seconds per vehicle waiting in a candidate destination's zone.
        self.congestion_destination_s=None;self._dock_zones=None
        from .resource_changes import DirtyResources
        self._dirty_handling=DirtyResources()
        self._handling_frontiers={};self._handling_waiters={}
        self._admission_retries={}
        self.exchange = EquipmentExchange(manufacturing,emit,exchange_time_us=exchange_time_us)
        self.scheduler.register_pass('transfer',self.dispatch)
        self.custody.subscribe_resources(self.resource_changed)

    def dirty(self):
        self._dirty_handling.mark()
        self.scheduler.mark_dirty('transfer')

    def traffic_changed(self,vehicle_id):
        # Pose changes affect shared assignment costs and clearance. They do
        # not repair native port/OHT faults, the only handling blockers.
        # An assigned vehicle's pose enters no other decision (only unassigned
        # vehicles are ranked), and its endpoint arrival marks dirty itself.
        vehicle=self.vehicles.get(vehicle_id)
        if vehicle is not None and vehicle.get('assignment_id') and not self._metered and type(self) is Transfers:return
        # An unassigned vehicle's move can only matter to a demand that last
        # found no available or reachable vehicle.
        if vehicle is not None and not self._vehicle_starved and not self._metered and type(self) is Transfers:return
        if (type(self) is Transfers
                and getattr(self._handling_blocked,'__func__',None) is _HANDLING_BLOCKED_NATIVE
                and getattr(self.dirty,'__func__',None) is _DIRTY_NATIVE
                and getattr(self.dock_id,'__func__',None) is _DOCK_ID_NATIVE):
            self.scheduler.mark_dirty('transfer')
        else:self.dirty()

    def resource_changed(self,change):
        self._load_dirty=True
        self._neg.slots_changed(change.slot_ids)
        if change.tool_ids or change.group_ids or change.rail_ids:self._neg.resources_changed(change)
        if change.group_ids or change.tool_ids or change.rail_ids:
            self.dirty();return
        affected=set()
        for sid in change.slot_ids:
            affected.update(self._handling_waiters.get(('slot',sid),()))
            affected.update(self._handling_waiters.get(('slot',self.dock_id(sid)),()))
        for vid in change.vehicle_ids:affected.update(self._handling_waiters.get(('vehicle',vid),()))
        for carrier in change.carrier_ids:
            did=self.active.get(carrier)
            if did is not None:affected.add(did)
        self._dirty_handling.mark(affected)
        # Assignment/exchange/clearance retain their complete shared pools.
        self.scheduler.mark_dirty('transfer')

    def _forget_handling(self,did):
        prior=self._handling_frontiers.pop(did,None)
        if prior is None:return
        for resource in prior[1]:
            waiters=self._handling_waiters[resource];waiters.discard(did)
            if not waiters:del self._handling_waiters[resource]

    def _remember_handling(self,r,phase,key):
        resources=frozenset({('vehicle',r['vehicle_id']),('slot',r['origin_id']),
            ('slot',r['destination_id']),('slot',self.dock_id(r['origin_id'] if phase=='pickup' else r['destination_id']))})
        self._handling_frontiers[r['id']]=(key,resources)
        for resource in resources:self._handling_waiters.setdefault(resource,set()).add(r['id'])

    def dock_id(self, sid):
        return self.slot_docks.get(sid,sid)

    def _pose(self, sid):
        dock = self.docks[sid]
        return {k:dock[k] for k in ('rail_id','offset_um')}

    def _congestion_penalties(self):
        """{rail_id: seconds} = congestion_routing_s x vehicles waiting on that rail."""
        traffic=self.traffic;weight=self.congestion_routing_s;counts={}
        for vid,rid in traffic.active.items():
            r=traffic.requests[rid]
            if r['status']=='pending' and r['blockers']:
                rail=traffic.vehicles[vid]['rail_id'];counts[rail]=counts.get(rail,0)+1
        return {rail:n*weight for rail,n in counts.items()}

    def _zone_rails(self, sid):
        """Rails whose waiting vehicles count against a destination dock: its bay's
        rails (intrabay docks) or its own rail, plus up to six interbay rails
        upstream of the bay's entrances. Static topology, built once."""
        zones=self._dock_zones
        if zones is None:
            traffic=self.traffic;rails=traffic.rails
            incoming={}
            for rid,rail in rails.items():incoming.setdefault(rail['to_node'],[]).append(rid)
            kind=traffic._rail_bay_kind;by_bay={}
            for rid,rail in rails.items():by_bay.setdefault(rail.get('bay_id'),set()).add(rid)
            bay_zone={}
            for bay,members in by_bay.items():
                if not members or kind.get(next(iter(members)))!='INTRABAY':continue
                zone=set(members);frontier=[rid for m in members for rid in incoming.get(rails[m]['from_node'],())
                    if kind.get(rid)=='INTERBAY']
                for _ in range(6):
                    nxt=[]
                    for rid in frontier:
                        if rid in zone:continue
                        zone.add(rid);nxt.extend(p for p in incoming.get(rails[rid]['from_node'],()) if kind.get(p)=='INTERBAY')
                    frontier=nxt
                bay_zone[bay]=frozenset(zone)
            zones=self._dock_zones={}
            for dock_id,dock in self.docks.items():
                rail=dock.get('rail_id');bay=rails[rail].get('bay_id') if rail in rails else None
                zones[dock_id]=bay_zone.get(bay,frozenset((rail,)) if rail else frozenset())
        return zones.get(self.dock_id(sid),frozenset())

    def _waiting_by_rail(self):
        traffic=self.traffic;counts={}
        for vid,rid in traffic.active.items():
            r=traffic.requests[rid]
            if r['status']=='pending' and r['blockers']:
                rail=traffic.vehicles[vid]['rail_id'];counts[rail]=counts.get(rail,0)+1
        return counts

    def _leg_path(self, start, end, penalties):
        if not penalties:return self._path(start,end)
        try: return self._router().path(start,end,penalties)
        except ValueError as error:
            if str(error) == 'unreachable directed destination': return None
            raise

    def _path(self, start, end):
        try: return self._router().path(start,end)
        except ValueError as error:
            if str(error) == 'unreachable directed destination': return None
            raise

    _native_path = _path

    def _reachable(self,start,end):
        # Extension path behavior must remain authoritative. The native router
        # is immutable geometry; no physical admission/permission is cached.
        from .routing_index import FreeFlowCosts
        if getattr(self._path,'__func__',None) is Transfers._native_path:
            router=self._router()
            if (type(router) is FreeFlowCosts
                    and getattr(router.path,'__func__',None) is FreeFlowCosts._native_path):
                return router.reachable(start,end)
        return self._path(start,end) is not None

    def _visit_valid(self, r):
        lot = self.manufacturing.lots[r['lot_id']]
        vid = r['visit_id']
        return (lot.get('current_visit_id') == vid and
                (vid is None and lot.get('route_complete_at_us') is not None or
                 vid is not None and self.manufacturing.rules.visits[vid]['status']=='pending'
                 and not self.manufacturing.rules.visits[vid].get('execution_id')))

    def super_hot_destinations(self):
        """Tools that a super-hot LOT is being transferred to (derived, never stored)."""
        lots=self.manufacturing.lots;cached=self._super_hot_lots
        key=(len(lots),next(reversed(lots),None))
        if cached is None or cached[0]!=key:
            cached=self._super_hot_lots=(key,tuple(lid for lid,lot in lots.items() if lot.get('super_hot')))
        tools=set()
        for lid in cached[1]:
            lot=lots.get(lid)
            did=self.active.get(lot.get('carrier_id')) if lot else None
            if did is None:continue
            r=self.demands[did];sid=r['rack_slot_id'] or r['destination_id']
            tid=self.custody.slots[sid].get('equipment_id') if sid else None
            if tid in self.manufacturing.tools:tools.add(tid)
        return tools

    def request(self, carrier_id: str, visit_id: str, destinations: tuple[str,...]) -> str:
        lid,lot = self.exchange._lot(carrier_id)
        if (not isinstance(destinations,tuple) or not destinations or len(set(destinations))!=len(destinations)
                or any(s not in self.docks or s not in self.custody.slots or s in self.internal_docks for s in destinations)
                or lot.get('current_visit_id') != visit_id or carrier_id not in self.custody.locations):
            raise ValueError('invalid transfer demand')
        if carrier_id in self.active:
            r = self.demands[self.active[carrier_id]]
            if r['visit_id'] != visit_id or r['destinations'] != destinations:
                raise ValueError('conflicting active carrier demand')
            return r['id']
        probe={'visit_id':visit_id,'lot_id':lid}
        if any(self.docks[sid].get('role') not in ('IN','BOTH') or not self._target_valid(probe,sid) for sid in destinations):
            raise ValueError('invalid transfer destination')
        return self._request(carrier_id,lid,visit_id,destinations,self.scheduler.now_us)

    def _request(self, carrier, lid, visit, destinations, age):
        self.sequence += 1; did = f'transfer:{self.sequence:08}'
        self.demands[did] = {'id':did,'carrier_id':carrier,'lot_id':lid,'visit_id':visit,
            'destinations':destinations,'original_request_time_us':age,'status':'pending',
            'origin_id':None,'destination_id':None,'rack_slot_id':None,'rack_owner':None,
            'vehicle_id':None,'overflow':False,'output_exchange_id':None}
        self.active[carrier] = did;self.dirty();return did

    def _rack(self, r, sid, _eligible=None):
        # _eligible: the visit's eligible tools among a caller's candidates;
        # eligibility filters each tool independently.
        m = self.manufacturing;dock = self.docks[sid];tid = dock.get('equipment_id')
        if r['visit_id'] is None:
            return (None,None) if dock['kind']=='exit' else None
        if tid not in m.tools: return None
        visit = m.rules.visits[r['visit_id']];step = m._step(visit)
        if (step['tool_group_id'] != m.tools[tid]['tool_group_id']
                or not (tid in _eligible if _eligible is not None else m.rules.eligible_tools(visit['id'],[tid]))): return None
        if step.get('batch_min'):
            aid = visit.get('assembly_id')
            if not aid: return None
            a = m.batches.assemblies[aid]
            if a['status']!='assembling' or a['tool_id']!=tid: return None
            member = next((x for x in a['members'] if x['carrier_id']==r['carrier_id'] and not x['arrived']),None)
            if member is None: return None
            slot = self.custody.slots[member['rack_slot_id']]
            if slot['carrier_id'] is None and slot['reservation']==aid: return member['rack_slot_id'],aid
            return None
        if m.campaigns:
            if not m.campaigns.allows_admission(visit['id'],tid):return None
            admission=m.campaigns.admission(visit['id'],tid)
            if admission:
                slot=m.custody.slots[admission[0]]
                return admission if slot['carrier_id'] is None and slot['reservation']==admission[1] else None
        if m.tools[tid].get('pm_pending'):return None
        if self.down_tool_avoid_us and m.causes[tid] and self.long_downtime(tid,self.down_tool_avoid_us):return None
        ordered=self._sorted_racks.get(tid)
        if ordered is None:ordered=self._sorted_racks[tid]=tuple(sorted(m.tools[tid]['rack_slots']))
        rack = next((sid for sid in ordered if self.custody.available(sid)),None)
        return (rack,r['id']) if rack else None

    def _emit(self, kind, payload):
        self._load_dirty=True
        self._event_sink(kind,payload)

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

    def _invalidate_loads(self, change):
        self._load_dirty=True;self._neg.slots_changed(change.slot_ids);self._neg.resources_changed(change)

    def note_event(self, kind, payload):
        self._neg.event(kind,payload)

    def rebuild_derived_loads(self):
        self._resident_lots_by_tool={};self._incoming_lots_by_tool={}
        current=getattr(self.manufacturing,'current_lot_items',self.manufacturing.lots.items)
        for lot_id,lot in current():
            visit=self.manufacturing.rules.visits.get(lot.get('current_visit_id'))
            if not visit or visit['status'] not in ('pending','processing'):continue
            carrier=lot['carrier_id'];slot=self.custody.slots.get(self.custody.locations.get(carrier),{})
            tid=slot.get('equipment_id')
            if slot.get('kind')=='rack' and tid in self.manufacturing.tools:
                self._resident_lots_by_tool.setdefault(tid,set()).add(lot_id)
            demand=self.demands.get(self.active.get(carrier),{})
            target=self.docks.get(demand.get('destination_id'),{}).get('equipment_id')
            if demand.get('status') not in (None,'pending','cancelled') and target in self.manufacturing.tools:
                self._incoming_lots_by_tool.setdefault(target,set()).add(lot_id)
        self._tool_loads={};self._incoming_by_tool=None
        self._load_dirty=False

    def destination_loads(self,r,tool_ids):
        """Union of live residents and assigned LOTs; never double count."""
        visit=self.manufacturing.rules.visits.get(r['visit_id'])
        if not visit or self.manufacturing._step(visit).get('batch_min'):
            return {tid:0 for tid in tool_ids}
        lot_id=getattr(self.manufacturing,'carrier_lot_id',None)
        if lot_id is None:
            if self._load_dirty:self.rebuild_derived_loads()
            return {tid:len(self._resident_lots_by_tool.get(tid,set()) |
                self._incoming_lots_by_tool.get(tid,set())) for tid in tool_ids}
        # Same sets as rebuild_derived_loads, derived only for the asked tools:
        # residents from the tool's rack slots, incoming from active demands.
        if self._load_dirty:self._tool_loads={};self._incoming_by_tool=None;self._load_dirty=False
        loads=self._tool_loads
        return {tid:loads[tid] if tid in loads else self._tool_load(tid,lot_id) for tid in tool_ids}

    def _tool_load(self, tid, lot_id):
        m=self.manufacturing;visits=m.rules.visits;lots=m.lots
        def live(lid):
            visit=visits.get(lots[lid].get('current_visit_id'))
            return visit and visit['status'] in ('pending','processing')
        if self._incoming_by_tool is None:
            incoming=self._incoming_by_tool={};tools=m.tools;demands=self.demands;docks=self.docks
            for carrier,did in self.active.items():
                lid=lot_id(carrier)
                if lid is None:continue
                demand=demands.get(did,{})
                target=docks.get(demand.get('destination_id'),{}).get('equipment_id')
                if demand.get('status') not in (None,'pending','cancelled') and target in tools and live(lid):
                    incoming.setdefault(target,set()).add(lid)
        lots_at=set(self._incoming_by_tool.get(tid,()))
        if tid in m.tools:
            slots=self.custody.slots;locations=self.custody.locations
            for sid in self._tool_rack_slots().get(tid,()):
                carrier=slots[sid]['carrier_id']
                if carrier is None or locations.get(carrier)!=sid:continue
                lid=lot_id(carrier)
                if lid is not None and live(lid):lots_at.add(lid)
        self._tool_loads[tid]=load=len(lots_at)
        return load

    def _tool_rack_slots(self):
        slots=self.custody.slots;cached=self._rack_slots_by_tool
        if cached is None or cached[0] is not slots or cached[1]!=len(slots):
            index={}
            for sid,slot in slots.items():
                if slot.get('kind')=='rack':index.setdefault(slot.get('equipment_id'),[]).append(sid)
            cached=self._rack_slots_by_tool=(slots,len(slots),index)
        return cached[2]

    def available_vehicle_ids(self):
        # Re-evaluate the finite fleet after every same-pass claim/yield.
        return tuple(vid for vid,v in self.vehicles.items() if v.get('available',True)
            and not v['assignment_id'] and self.custody.available(v['carrier_slot_id'])
            and (not v['movement_id'] or getattr(self,'traffic',None) is not None
                and self.traffic.can_yield(vid)))

    def _router(self):
        from .routing_index import FreeFlowCosts
        if self._cost_router is None:
            traffic=getattr(self,'traffic',None)
            self._cost_router=(traffic._router(self.vehicle_type) if traffic is not None else
                FreeFlowCosts(self.model,self.vehicle_type))
        return self._cost_router

    def free_flow_costs(self, origin, destinations):
        return self._router().costs(origin,destinations)

    def congested_cost_key(self, count):
        """(cost, destination) -> sort key: cost_key, plus congestion_destination_s per
        vehicle waiting in the destination's zone when there is a choice."""
        base=self.cost_key()
        weight=self.congestion_destination_s
        if not weight or count<2 or getattr(self,'traffic',None) is None:
            return lambda cost,sid:base(cost)
        waiting=self._waiting_by_rail()
        if not waiting:return lambda cost,sid:base(cost)
        router=self._router();key=getattr(router,'cost_key',None)
        unit=router._scale if key and key() is not None else 1
        counts={}
        def keyed(cost,sid):
            # Zones are shared frozensets; their count is fixed for this snapshot.
            zone=self._zone_rails(sid);n=counts.get(id(zone))
            if n is None:n=counts[id(zone)]=sum(waiting.get(rail,0) for rail in zone)
            return base(cost)+n*weight*unit
        return keyed

    def cost_key(self):
        """Sort key equivalent to comparing free_flow_costs() results; cheaper than Fractions."""
        key=getattr(self._router(),'cost_key',None)
        return (key() if key else None) or (lambda cost:cost)

    def _destination_candidates(self, r):
        # Origin-independent part of _choices: available destinations with
        # their rack admission. Callers ranking several origins share it.
        candidates=[];eligible=None
        open_docks=self._open_destinations(self._layout(r['destinations']))
        if not open_docks:return candidates
        if r['visit_id'] is not None:
            tools=self.manufacturing.tools
            eligible=set(self.manufacturing.rules.eligible_tools(r['visit_id'],
                [tid for tid in dict.fromkeys(self.docks[sid].get('equipment_id') for sid in r['destinations']) if tid in tools]))
        # With a visit, _rack depends on the dock only through its tool.
        racks={}
        for sid,tid in open_docks:
            if eligible is None:rack=self._rack(r,sid,eligible)
            else:
                if tid not in racks:racks[tid]=self._rack(r,sid,eligible)
                rack=racks[tid]
            if rack is not None:candidates.append((sid,tid or '',rack,False))
        return candidates

    def _open_destinations(self, layout):
        # (sid, tool) of a layout's docks that are free (and whose internal dock
        # is free), in layout order. Pure in custody state: memoized until the
        # next custody change (every slot change bumps custody.version).
        version=self.custody.version
        if self._open_version!=version:self._open_memo={};self._open_version=version
        key=id(layout);hit=self._open_memo.get(key)
        if hit is not None and hit[0] is layout:return hit[1]
        available=self.custody.available
        result=tuple((sid,tid) for sid,dock,internal,tid in layout
                     if not (internal and not available(dock)) and available(sid))
        self._open_memo[key]=(layout,result)
        return result

    def _layout(self, destinations):
        # Fixed dock facts of a destination list, in order: (sid, dock id,
        # dock is internal, tool id) for input-capable docks only.
        if type(destinations) is not tuple:destinations=tuple(destinations)
        layout=self._destination_layout.get(destinations)
        if layout is None:
            layout=tuple((sid,self.dock_id(sid),self.dock_id(sid) in self.internal_docks,self.docks[sid].get('equipment_id'))
                for sid in destinations if self.docks[sid].get('role') in ('IN','BOTH'))
            if len(self._destination_layout)>4096:self._destination_layout.clear()
            self._destination_layout[destinations]=layout
        return layout

    def _choices(self, r, origin, *, with_paths=True, _candidates=None):
        if _candidates is None:_candidates=self._destination_candidates(r)
        candidates=[c for c in _candidates if c[0]!=origin]
        def ranked(candidates):
            costs=self.free_flow_costs(self._pose(origin),
                {sid:self._pose(sid) for sid,_,_,_ in candidates})
            return [(costs[sid],tid,sid,rack,
                     self._path(self._pose(origin),self._pose(sid)) if with_paths else None,overflow)
                    for sid,tid,rack,overflow in candidates if sid in costs]
        choices=ranked(candidates) if candidates else []
        cost=self.congested_cost_key(len(choices))
        if choices:
            loads=self.destination_loads(r,[choice[1] for choice in choices])
            m=self.manufacturing
            if r['visit_id'] is not None and m.campaigns is not None and m.campaigns.policy=='group_least_setup':
                # SMT2020 tool wake-up: among equally loaded tools, the least setup first.
                step=m._step(m.rules.visits[r['visit_id']])
                setup={tid:m.setups[tid].decision(step)['expected_us'] if tid in m.setups else 0
                       for tid in {choice[1] for choice in choices}}
                return sorted(choices,key=lambda choice:(loads[choice[1]],setup[choice[1]],cost(choice[0],choice[2]),*choice[1:4]))
            # Same order as (load, choice): destination ids are unique.
            return sorted(choices,key=lambda choice:(loads[choice[1]],cost(choice[0],choice[2]),*choice[1:4]))
        if (r['visit_id'] is None or self.docks[origin]['kind']=='track_buffer'
                or (r['carrier_id'],r['visit_id']) in self.overflow_visits):return []
        if (self.manufacturing._step(self.manufacturing.rules.visits[r['visit_id']]).get('batch_min')
                and not (self.buffer_selection=='next_group_bays' and self.custody.slots[origin]['kind'] in ('rack','port'))):return []
        self._neg.used_buffers=True
        index=getattr(self,'resource_index',None)
        buffer_ids=(index.buffer_candidates(None,protected=False) if index is not None else self.docks)
        if self.buffer_selection=='next_group_bays':
            group=self.manufacturing._step(self.manufacturing.rules.visits[r['visit_id']])['tool_group_id']
            tiers=self._group_buffer_tiers(group,buffer_ids)
        else:tiers=(buffer_ids,)
        product=self.manufacturing.lots[r['lot_id']].get('product_id')
        for tier in tiers:
            candidates=[]
            for sid in tier:
                dock=self.docks[sid]
                if (dock['kind']!='track_buffer' or self.clearance and sid in self.clearance.protected_slot_ids
                        or sid==origin or not self.custody.available(sid)
                        or dock.get('role','BOTH') not in ('IN','BOTH')):continue
                if dock.get('compatible_products') is not None and product not in dock['compatible_products']:continue
                candidates.append((sid,'',(None,None),True))
            if candidates:
                cost=self.congested_cost_key(len(candidates))
                return sorted(ranked(candidates),key=lambda choice:(cost(choice[0],choice[2]),*choice[1:4]))
        return []

    def _group_buffer_tiers(self, group, buffer_ids):
        """Static buffer tiers for a tool group: buffers in the bays hosting its
        tools, then in those bays' neighbours, then every ordinary buffer."""
        tiers=self._buffer_tiers.get(group)
        if tiers is None:
            rail_bay={r['id']:r.get('bay_id') for r in self.model['rails']}
            m=self.manufacturing
            bays={e.get('bay_id') for e in self.model['equipment'] if e['id'] in m.tools
                  and m.tools[e['id']]['tool_group_id']==group}
            neighbours=set()
            for bay in self.model.get('bays',()):
                if bay['id'] in bays:
                    neighbours.update(x.strip() for x in str(bay.get('source_neighbors') or '').split(',') if x.strip())
            neighbours-=bays
            own=tuple(sid for sid in buffer_ids if rail_bay.get(self.docks[sid]['rail_id']) in bays)
            near=tuple(sid for sid in buffer_ids if rail_bay.get(self.docks[sid]['rail_id']) in neighbours)
            tiers=self._buffer_tiers[group]=(own,near,tuple(buffer_ids))
        return tiers

    def _movement(self, r, phase, path):
        mid=r['id']+':'+phase
        self.movements[mid]={'id':mid,'demand_id':r['id'],'vehicle_id':r['vehicle_id'],
                             'phase':phase,'target_pose':dict(r[phase+'_pose']),
                             'path':path,'status':'requested','cancelled':False,'queued_at_us':self.scheduler.now_us}
        self.vehicles[r['vehicle_id']]['movement_id']=mid
        self.emit('transfer_movement_requested',dict(self.movements[mid]))
        if getattr(self,'traffic',None) is not None:self.traffic.dirty()

    def _target_valid(self, r, sid):
        dock=self.docks[sid];m=self.manufacturing;tid=dock.get('equipment_id')
        if r['visit_id'] is None:return dock['kind']=='exit'
        visit=m.rules.visits[r['visit_id']]
        return (tid in m.tools and m.tools[tid]['tool_group_id']==m._step(visit)['tool_group_id']
                and bool(m.rules.eligible_tools(visit['id'],[tid])))

    def _admission_signature(self, r):
        """Exact local inputs for native pure validation; unknown contracts retry."""
        m=self.manufacturing
        if (type(self) is not Transfers or type(m) is not _AdmissionManufacturing
                or type(m.rules) is not _AdmissionRules
                or m.rules.lots is not m.lots
                or any(getattr(getattr(obj,name),'__func__',None) is not native
                    for obj,name,native in (
                        (self,'_admission_valid',_ADMISSION_NATIVE),
                        (self,'_visit_valid',_VISIT_NATIVE),
                        (self,'_target_valid',_TARGET_NATIVE),
                        (m,'_step',_STEP_NATIVE),
                        (m.rules,'eligible_tools',_ELIGIBLE_NATIVE),
                        (m.rules,'_components',_COMPONENTS_NATIVE)))):
            return None
        def record(mapping,key):
            if type(mapping) is not dict:raise TypeError
            value=mapping[key]
            if type(value) is not dict:raise TypeError
            return value
        def fields(value,names):
            if type(value) is not dict:raise TypeError
            return tuple(value.get(name) for name in names)
        try:
            if type(r) is not dict:return None
            lot=record(m.lots,r['lot_id'])
            _admission_value(fields(r,('id','lot_id','visit_id','carrier_id','destination_id','rack_slot_id','rack_owner','overflow')))
            key=[fields(r,('lot_id','visit_id','carrier_id','destination_id',
                'rack_slot_id','rack_owner','overflow')),
                fields(lot,('current_visit_id','route_complete_at_us'))]
            vid=r['visit_id']
            if vid is not None:
                visit=record(m.rules.visits,vid)
                _admission_value(fields(visit,('id','lot_id','source_step_id')))
                key.append(fields(visit,('id','lot_id','source_step_id','status',
                    'execution_id','assembly_id')))
            if r['overflow']:
                # Clearance leases and extension handoffs retain their original
                # validation contract; no proven dependency signature here.
                return None
            else:
                dock=record(self.docks,r['destination_id'])
                key.append(fields(dock,('kind','equipment_id')))
                if vid is not None:
                    _admission_value(dock.get('equipment_id'))
                    target=record(m.tools,dock.get('equipment_id'))
                    owner_lot=record(m.lots,visit['lot_id'])
                    _admission_value(owner_lot['route_id'])
                    route=record(m.rules.routes,owner_lot['route_id'])
                    steps=record(m.rules.steps,owner_lot['route_id'])
                    step=record(steps,visit['source_step_id'])
                    components=route['dedication_components']
                    if type(components) not in (list,tuple):raise TypeError
                    bindings=owner_lot['dedication']
                    if type(bindings) is not dict:raise TypeError
                    dedication=[]
                    for component in components:
                        cid,source_steps=fields(component,('id','source_step_ids'))
                        _admission_value((cid,source_steps))
                        if type(source_steps) not in (list,tuple):raise TypeError
                        dedication.append((cid,source_steps,cid in bindings,bindings.get(cid)))
                    key.extend((target['tool_group_id'],owner_lot['route_id'],
                        fields(step,('tool_group_id','batch_min')),dedication))
                    if r['rack_slot_id'] and step.get('batch_min'):
                        if type(m.batches) is not _AdmissionBatch or type(m.batches.assemblies) is not dict:raise TypeError
                        assembly=m.batches.assemblies.get(r['rack_owner'])
                        if assembly is None:key.append(None)
                        else:
                            members=assembly['members']
                            if type(members) not in (list,tuple):raise TypeError
                            key.append((fields(assembly,('status','tool_id')),
                                tuple(fields(member,('carrier_id','visit_id','rack_slot_id','arrived'))
                                    for member in members)))
                    elif r['rack_slot_id'] and m.campaigns is not None:
                        campaigns=m.campaigns
                        if (type(campaigns) is not _AdmissionCampaigns
                                or campaigns.policy != 'staged_campaign'
                                or campaigns.manufacturing is not m
                                or any(getattr(getattr(campaigns,name),'__func__',None) is not native
                                    for name,native in _CAMPAIGN_NATIVE)):
                            return None
                        if (type(campaigns.visit_owners) is not dict
                                or type(campaigns.managed_tools) is not set):
                            return None
                        owner=campaigns.visit_owners.get(vid)
                        _admission_value(owner)
                        key.extend((owner,dock['equipment_id'] in campaigns.managed_tools))
                        # These native branches depend only on owned campaign membership.
                        campaign_owner=r['rack_owner']
                        if (campaign_owner and campaign_owner.startswith('setup-campaign:')) or (
                                dock['equipment_id'] in campaigns.managed_tools and owner):
                            if owner:
                                campaign=record(campaigns.records,owner)
                                members=campaign['members']
                                if type(members) not in (list,tuple):raise TypeError
                                key.append((campaign['tool_id'],
                                    tuple(fields(member,('visit_id','rack_slot_id','arrived'))
                                        for member in members)))
                        elif dock['equipment_id'] in campaigns.managed_tools:
                            if type(campaigns.active_by_tool) is not dict or type(m.setups) is not dict:return None
                            tid=dock['equipment_id']
                            state=m.setups[tid]
                            if (type(state) is not _AdmissionSetup
                                    or any(getattr(getattr(state,name),'__func__',None) is not native
                                        for name,native in _SETUP_NATIVE)):
                                return None
                            # Only this finite tool's recipe state and transition
                            # table affect the native setup decision.
                            key.append((tid in campaigns.active_by_tool,
                                state.current,state.minimum_runs,state.completed_lots,
                                state.transitions,step.get('setup')))
            return _admission_value(tuple(key))
        except (KeyError,TypeError,AttributeError,RecursionError):
            return None

    def _retry_admission_valid(self, r):
        key=self._admission_signature(r)
        prior=self._admission_retries.get(r['id'])
        if key is not None and prior is not None and prior[0]==key:
            return prior[1]
        valid=self._admission_valid(r)
        if key is None:self._admission_retries.pop(r['id'],None)
        else:self._admission_retries[r['id']]=(key,valid)
        return valid

    def _admission_valid(self, r):
        if not self._visit_valid(r):return False
        if r['overflow']:
            plan=self.handoffs.records.get(r['id']) if self.handoffs else None
            if plan and plan['lease_id'] and plan['phase'] in ('output_reserved','output_active'):
                return self.clearance.valid_output(r['visit_id'],plan['lease_id'],plan['source_rack_id'])
            return True
        if not self._target_valid(r,r['destination_id']):return False
        if r['rack_slot_id'] and self.manufacturing._step(self.manufacturing.rules.visits[r['visit_id']]).get('batch_min'):
            visit=self.manufacturing.rules.visits[r['visit_id']]
            a=self.manufacturing.batches.assemblies.get(r['rack_owner'])
            return bool(visit.get('assembly_id')==r['rack_owner'] and a and a['status']=='assembling'
                and a['tool_id']==self.docks[r['destination_id']]['equipment_id'] and any(x['carrier_id']==r['carrier_id']
                and x['visit_id']==r['visit_id']
                and x['rack_slot_id']==r['rack_slot_id'] and not x['arrived'] for x in a['members']))
        m = self.manufacturing
        if r['rack_slot_id'] and m.campaigns:
            return m.campaigns.valid_admission(r['visit_id'],
                self.docks[r['destination_id']]['equipment_id'], r['rack_slot_id'], r['rack_owner'])
        return True

    def _docks_by_tool(self):
        # Prepared docks are immutable; rebuild only when the mapping changes.
        key=(id(self.docks),len(self.docks))
        if self._tool_docks_key!=key:
            index={}
            for sid,dock in self.docks.items():index.setdefault(dock.get('equipment_id'),[]).append(sid)
            self._tool_docks=index;self._tool_docks_key=key
        return self._tool_docks

    def _valid_tool_destination(self, r, tid):
        candidates=self._docks_by_tool().get(tid,())
        if not candidates:return False
        destinations=set(r['destinations'])
        return any(sid in destinations and self._target_valid(r,sid) for sid in candidates)

    def _rack_origin(self, r, origin):
        m=self.manufacturing;slot=self.custody.slots[origin];tid=slot.get('equipment_id');vid=r['visit_id']
        if tid not in m.tools:return False
        if r['output_exchange_id'] is not None and self.exchange.requests[r['output_exchange_id']]['status'] in ('pending','active'):return False
        if vid is not None and m.lots[r['lot_id']].get('current_visit_id')==vid:
            visit=m.rules.visits[vid]
            aid=visit.get('assembly_id');a=m.batches.assemblies.get(aid)
            if (a and a['tool_id']==tid and a['status'] in ('assembling','processing','processed')
                    and any(x['visit_id']==vid and x['carrier_id']==r['carrier_id']
                        and x['rack_slot_id']==origin and x['arrived'] and not x['departed'] for x in a['members'])):
                r['status']='reused';del self.active[r['carrier_id']]
                self.emit('transfer_rack_reused',self._event(r));return True
            if (not m.tools[tid].get('assembly_id') and not m._step(visit).get('batch_min')
                    and self._valid_tool_destination(r,tid)):
                execution=m.executions.get(visit.get('execution_id'))
                if (m._rack_ready(vid) and (m.campaigns is None or m.campaigns.can_reside(vid,tid))
                        or execution and execution['rack_slot_id']==origin):
                    r['status']='reused';del self.active[r['carrier_id']];m.dirty()
                    self.emit('transfer_rack_reused',self._event(r));return True
        if (not self._visit_valid(r) or r['output_exchange_id'] is not None
                or slot['reservation'] is not None or r['carrier_id'] in m.claims):return False
        if self.handoffs is not None:return self.handoffs.prepare(r['id'],origin)
        # Distinct pending exchanges need distinct planned destinations. A
        # faulted port must not block feasible service through another port.
        planned={x['destination_id'] for x in self.exchange.requests.values()
                 if x['tool_id']==tid and not x['inbound'] and x['status']=='pending'}
        ports=[]
        index=getattr(self,'resource_index',None)
        for sid in (index.output_ports(tid) if index is not None else self.docks):
            dock=self.docks[sid]
            if (dock.get('equipment_id')!=tid or dock.get('role') not in ('OUT','BOTH')
                    or sid in planned or not self.custody.available(sid)):continue
            costs=[path['seconds'] for target in r['destinations'] if self._target_valid(r,target)
                   for path in [self._path(self._pose(sid),self._pose(target))] if path is not None]
            if costs:ports.append((min(costs),sid))
        if not ports:return False
        _,sid=min(ports);eid=r['id']+':output'
        self.exchange.request(eid,r['carrier_id'],origin,sid)
        r['output_exchange_id']=eid;return True

    def _local_input(self, r, origin):
        if (origin not in r['destinations'] or self.docks[origin].get('role') not in ('IN','BOTH')
                or self.custody.slots[origin]['reservation'] is not None
                or r['carrier_id'] in self.manufacturing.claims):return False
        rack=self._rack(r,origin)
        if rack is None or rack[0] is None:return False
        sid,owner=rack
        with self.custody.batch_notifications():
            if owner==r['id'] and not self.custody.reserve(sid,owner):return False
            self.exchange.request(r['id']+':input',r['carrier_id'],origin,sid,reservation_owner=owner,
                                  on_complete=native_action(self,'transfer:exchanged',r['id'],None,lambda:self._exchanged(r)))
            r.update(status='delivered',origin_id=origin,destination_id=origin,rack_slot_id=sid,rack_owner=owner)
        self.emit('transfer_delivered',self._event(r));return True

    def assign(self) -> bool:
        changed = False
        metering=bool(self.dispatch_metering) and getattr(self,'traffic',None) is not None
        if metering:self._meter_waiting=None;self._metered=False
        current=(self.demands[did] for did in self.active.values())
        # Travelling/handling demands cannot claim another assignment.
        # Keep the original rank and recheck status after earlier callbacks.
        pending=(r for r in current if r['status']=='pending')
        if self.super_hot_expedite:
            # Transport jobs carry the LOT priority: super-hot, then hot, then regular.
            lots=self.manufacturing.lots
            order=lambda r:(-lots[r['lot_id']].get('priority',0),r['original_request_time_us'],r['id'])
        else:order=lambda r:(r['original_request_time_us'],r['id'])
        for r in sorted(pending,key=order):
            if r['status']!='pending':continue
            if self.manufacturing.lots[r['lot_id']].get('current_visit_id')!=r['visit_id']:
                self.cancel(r['id']);changed=True;continue
            origin=self.custody.locations[r['carrier_id']]
            if self.custody.slots[origin]['kind']=='rack':
                changed=self._rack_origin(r,origin) or changed
                continue
            if not self._visit_valid(r):continue
            if origin not in self.docks or origin in self.internal_docks:continue
            if origin in self.slot_docks and not self.custody.available(self.dock_id(origin)):continue
            if self._local_input(r,origin):changed=True;continue
            buffer_owner=self.clearance.buffer_owner(r['carrier_id'],origin) if self.clearance else None
            if (self.docks[origin].get('role','BOTH') not in ('OUT','BOTH')
                    or r['carrier_id'] in self.manufacturing.claims
                    or self.custody.slots[origin]['reservation'] not in (None,buffer_owner)):continue
            # Distinct destination IDs settle rank before the path field.
            neg=self._neg
            if neg.hit(r['id'],origin):continue
            stamp=neg.begin()
            choices=self._choices(r,origin,with_paths=False)
            if not choices:neg.store(r,origin,stamp,(),None);continue
            if metering:
                choices=[c for c in choices if not self._meter_hold(r,c[2])]
                if not choices:continue
            _,_,destination,(rack,rack_owner),loaded,overflow=choices[0]
            if self.busy_vehicle_cap and self._cap_hold(r):continue
            eligible={vid:self.vehicles[vid] for vid in self.available_vehicle_ids()}
            if not eligible:self._vehicle_starved=True;continue
            costs=self._router().costs_to(eligible,self._pose(origin))
            if not costs:self._vehicle_starved=True;continue
            _,vehicle_id=min((seconds,vid) for vid,seconds in costs.items())
            # Keep the original full-path tie breaker for the chosen vehicle.
            penalties=self._congestion_penalties() if self.congestion_routing_s and getattr(self,'traffic',None) is not None else None
            empty=self._leg_path(eligible[vehicle_id],self._pose(origin),penalties)
            if empty is None:raise RuntimeError('ranked pickup path is unreachable')
            vehicle=self.vehicles[vehicle_id]
            loaded=self._leg_path(self._pose(origin),self._pose(destination),penalties)
            if loaded is None:raise RuntimeError('ranked delivery path is unreachable')
            destinations=[destination,vehicle['carrier_slot_id']]
            if rack and rack_owner==r['id']:destinations.append(rack)
            dock_claims=sorted({self.slot_docks[sid] for sid in (origin,destination) if sid in self.slot_docks})
            destinations.extend(dock_claims)
            with self.custody.batch_notifications():
                if not self.custody.reserve_many(destinations,r['id'],origins={origin:r['carrier_id']},
                        reservation_owners={origin:buffer_owner} if buffer_owner else None):continue
                if vehicle['movement_id']:self.traffic.yield_for_assignment(vehicle_id)
                r.update(status='assigned',origin_id=origin,destination_id=destination,rack_slot_id=rack,
                         rack_owner=rack_owner,vehicle_id=vehicle_id,overflow=overflow,dock_claims=dock_claims,
                         pickup_pose=self._pose(origin),drop_pose=self._pose(destination),loaded_path=loaded)
                vehicle['assignment_id']=r['id']
                if overflow:self.overflow_visits.add((r['carrier_id'],r['visit_id']))
            self._movement(r,'pickup',empty)
            self.emit('transfer_assigned',self._event(r));changed=True
        self._meter_waiting=None
        return changed

    def _cap_hold(self, r):
        """Busy-vehicle cap: hold a non-super-hot dispatch while busy_vehicle_cap
        vehicles already hold a transfer. Engage/release is reported to diagnostics."""
        if self.manufacturing.lots[r['lot_id']].get('super_hot'):return False
        busy=sum(1 for v in self.vehicles.values() if v['assignment_id'])
        engaged=busy>=self.busy_vehicle_cap
        if self._cap_engaged is None:
            # First evaluation in this process (fresh start or resume): record the
            # state silently, so a resume does not report a change that did not happen.
            self._cap_engaged=engaged
        elif engaged!=self._cap_engaged:
            self._cap_engaged=engaged
            self.emit('dispatch_cap_changed',{'engaged':engaged,'busy_vehicles':busy,'cap':self.busy_vehicle_cap})
        return engaged

    def _meter_hold(self, r, destination):
        """Dispatch metering: a non-super-hot transfer may not be dispatched to a
        destination while dispatch_metering or more vehicles wait in that
        destination's zone (_zone_rails). Another destination may be chosen;
        with none left the transfer is held at its origin and re-evaluated on
        vehicle movement."""
        if not self.dispatch_metering or getattr(self,'traffic',None) is None:return False
        if self.manufacturing.lots[r['lot_id']].get('super_hot'):return False
        waiting=self._meter_waiting
        if waiting is None:waiting=self._meter_waiting=self._waiting_by_rail();self._meter_counts={}
        if not waiting:return False
        zone=self._zone_rails(destination);n=self._meter_counts.get(id(zone))
        if n is None:n=self._meter_counts[id(zone)]=sum(waiting.get(rail,0) for rail in zone)
        if n<self.dispatch_metering:return False
        self._metered=True
        return True

    def _event(self, r):
        return {k:v for k,v in r.items() if k!='timer'}

    def movement_started(self, movement_id):
        movement=self.movements[movement_id]
        if movement['status']!='requested' or movement['cancelled']:raise ValueError('invalid movement start')
        movement['status']='moving'

    def arrived(self, demand_id, phase):
        r=self.demands[demand_id]
        if phase not in ('pickup','drop'):raise ValueError('invalid arrival phase')
        v=self.vehicles[r['vehicle_id']];mid=demand_id+':'+phase;movement=self.movements[mid]
        traffic=getattr(self,'traffic',None)
        if traffic is not None and (mid not in traffic.requests or traffic.requests[mid]['status']!='complete'):
            raise ValueError('bound transfer arrival requires completed traffic permission')
        if (movement['status'] not in ('requested','moving')
                or any(v[k]!=r[phase+'_pose'][k] for k in ('rail_id','offset_um'))):
            raise ValueError('arrival requires actual movement endpoint')
        if r['status']=='cancelled':
            movement['status']='cancelled';v['movement_id']=None;self.dirty();return
        expected='assigned' if phase=='pickup' else 'picked_up'
        if r['status']!=expected or v['assignment_id']!=demand_id:raise ValueError('invalid transfer arrival')
        movement['status']='complete';v['movement_id']=None
        if phase=='pickup' and not self._admission_valid(r):
            self.cancel(demand_id);return
        r['status']=phase+'_wait'
        self.dirty()

    def _handling_blocked(self, r, phase):
        traffic=getattr(self,'traffic',None)
        return traffic is not None and (traffic.causes['oht'][r['vehicle_id']] or
            traffic.causes['port'][self.dock_id(r['origin_id'] if phase=='pickup' else r['destination_id'])])

    def _start_handling(self, r, phase):
        from .service import Timer
        self._forget_handling(r['id'])
        r['status']=phase+'_handling'
        demand_id=r['id']
        r['timer']=Timer(self.scheduler,demand_id+':'+phase,self.handling[phase],native_action(self,'transfer:handled',r['id'],phase,lambda:self._handled(r,phase)))
        self.emit('transfer_handling_started',self._event(r))

    def _handled(self, r, phase):
        v=self.vehicles[r['vehicle_id']];vehicle_slot=v['carrier_slot_id'];did=r['id']
        if r['status']!=phase+'_handling' or v['assignment_id']!=did:raise RuntimeError('transfer handling ownership changed')
        if phase=='pickup' and not self._admission_valid(r):
            self.cancel(did);return
        if phase=='drop' and not self._admission_valid(r):
            raise RuntimeError('transfer completion eligibility changed')
        origin,destination=(r['origin_id'],vehicle_slot) if phase=='pickup' else (vehicle_slot,r['destination_id'])
        slots=self.custody.slots
        if (self.custody.locations.get(r['carrier_id'])!=origin or slots[origin]['reservation']!=did
                or slots[destination]['reservation']!=did or slots[destination]['carrier_id'] is not None
                or r['rack_slot_id'] and slots[r['rack_slot_id']]['reservation']!=r['rack_owner']
                or any(slots[sid]['reservation']!=did or slots[sid]['carrier_id'] is not None for sid in r.get('dock_claims',()))):
            raise RuntimeError('transfer custody ownership changed')
        with self.custody.batch_notifications():
            self.custody.move(r['carrier_id'],origin,destination,did)
            service_slot=r['origin_id'] if phase=='pickup' else r['destination_id']
            if service_slot in self.slot_docks:
                dock=self.slot_docks[service_slot]
                self.custody.release(dock,did);r['dock_claims'].remove(dock)
            if phase=='pickup':
                if self.clearance:self.clearance.departed(r['carrier_id'],r['origin_id'])
                self.custody.reserve_many([],did,origins={vehicle_slot:r['carrier_id']})
                r['status']='picked_up'
                if self.handoffs and did in self.handoffs.records:self.handoffs.records[did]['phase']='transporting'
            else:
                r['status']='delivered';v['assignment_id']=None
                if r['rack_slot_id']:
                    self.exchange.request(did+':input',r['carrier_id'],destination,r['rack_slot_id'],reservation_owner=r['rack_owner'],
                                          on_complete=native_action(self,'transfer:exchanged',r['id'],None,lambda:self._exchanged(r)))
                elif r['overflow']:
                    if self.clearance:self.clearance.buffered(r['carrier_id'],destination)
                    del self.active[r['carrier_id']]
                    self._request(r['carrier_id'],r['lot_id'],r['visit_id'],r['destinations'],r['original_request_time_us'])
                else:del self.active[r['carrier_id']]
                if self.handoffs and not r['rack_slot_id']:self.handoffs.completed(did)
        self.emit('transfer_'+('picked_up' if phase=='pickup' else 'delivered'),self._event(r))
        if phase=='pickup':
            self._movement(r,'drop',r['loaded_path'])
        self.dirty()
        if getattr(self,'traffic',None) is not None:self.traffic.dirty()

    def _exchanged(self, r):
        del self.active[r['carrier_id']]
        r['exchanged_at_us']=self.scheduler.now_us
        if self.handoffs:self.handoffs.completed(r['id'])
        self.emit('transfer_rack_delivered',self._event(r))

    def cancel(self, demand_id: str) -> None:
        self._admission_retries.pop(demand_id,None)
        if demand_id not in self.demands:
            raise ValueError('cannot cancel terminal transfer')
        r=self.demands[demand_id]
        if self.handoffs and demand_id in self.handoffs.records:
            self.handoffs.cancel(demand_id);return
        if r['status'] not in ('pending','assigned','pickup_wait','pickup_handling'):raise ValueError('cannot cancel after pickup')
        owned=[sid for sid in (r['origin_id'],r['destination_id'],r['rack_slot_id'] if r['rack_owner']==demand_id else None)
               if sid is not None]
        owned.extend(r.get('dock_claims',()))
        if r['vehicle_id'] is not None:
            v=self.vehicles[r['vehicle_id']];owned.append(v['carrier_slot_id'])
            if v['assignment_id']!=demand_id:raise RuntimeError('cancel vehicle ownership changed')
            movement=self.movements[demand_id+':pickup']
            coupled=getattr(self,'traffic',None) is not None and movement['id'] in self.traffic.requests
            if coupled and self.traffic.requests[movement['id']]['status'] in ('pending','moving'):
                self.traffic._check_owner(self.traffic.requests[movement['id']])
        if any(self.custody.slots[sid]['reservation']!=demand_id for sid in owned):raise RuntimeError('cancel custody ownership changed')
        with self.custody.batch_notifications():
            eid=r['output_exchange_id']
            if eid is not None and self.exchange.requests[eid]['status'] in ('pending','active'):
                self.exchange.cancel(eid)
            if r.get('timer'):r['timer'].cancel()
            for sid in owned:self.custody.release(sid,demand_id)
            buffer_owner=self.clearance.buffer_owner(r['carrier_id'],r['origin_id']) if self.clearance else None
            if buffer_owner:
                if not self.custody.reserve_many([],buffer_owner,origins={r['origin_id']:r['carrier_id']}):
                    raise RuntimeError('cancelled pickup lost clearance buffer')
            if r['vehicle_id'] is not None:
                v['assignment_id']=None
                movement=self.movements[demand_id+':pickup'];movement['cancelled']=True
                if not coupled and movement['status']!='moving':movement['status']='cancelled';v['movement_id']=None
            r['status']='cancelled';del self.active[r['carrier_id']]
            self._forget_handling(demand_id)
            if r['overflow']:self.overflow_visits.discard((r['carrier_id'],r['visit_id']))
        if r['vehicle_id'] is not None and coupled:
            request=self.traffic.requests[movement['id']]
            if request['status'] in ('pending','moving'):self.traffic.cancel(movement['id'])
        self.emit('transfer_cancelled',self._event(r));self.dirty()
        if getattr(self,'traffic',None) is not None:self.traffic.dirty()

    def cancelled_arrived(self, movement_id):
        movement=self.movements[movement_id];r=self.demands[movement['demand_id']]
        if r['status']!='cancelled' or not movement['cancelled']:
            raise RuntimeError('cancelled traffic movement has no cancelled transfer')
        movement['status']='cancelled';self.dirty()

    def transfer_counts(self):
        demands=self.demands;slots=self.custody.slots
        pending=0
        for did in self.active.values():
            if demands[did]['status']=='pending':pending+=1
        loaded=0
        for sid in _vehicle_slot_ids(self):
            if slots[sid]['carrier_id'] is not None:loaded+=1
        return {'pending_transfer_count':pending,
            'committed_transfer_count':len(self.active)-pending,
            'loaded_transfer_count':loaded}

    def _assignment_dispatch_required(self):
        # Query after handling callbacks: they can enqueue or retarget work.
        return (type(self) is not Transfers
            or getattr(self.assign,'__func__',None) is not _ASSIGN_NATIVE
            or type(self.active) is not dict or type(self.demands) is not dict
            or any(self.demands[did]['status']=='pending' for did in self.active.values()))

    def dispatch(self):
        # Set again below by any demand that reaches vehicle selection and
        # finds no available, reachable vehicle (see traffic_changed).
        self._vehicle_starved=False
        handling=self._dirty_handling.take()
        # Derived retry entries never retain completed or retired demands.
        active_ids=set(self.active.values())
        self._admission_retries={did:value for did,value in self._admission_retries.items()
            if did in active_ids}
        changed=False
        if self.clearance:changed=self.clearance.dispatch() or changed
        if self.handoffs:
            active_ids=set(self.active.values())
            self.handoffs.waits={did:wait for did,wait in self.handoffs.waits.items() if did in active_ids}
            for plan in self.handoffs.current_records():
                did=plan['demand_id']
                if plan['phase'] in ('output_reserved','output_active') and not self._retry_admission_valid(self.demands[did]):
                    self.cancel(did);changed=True
        for r in [self.demands[did] for did in self.active.values()]:
            if r['status'] not in ('pickup_wait','drop_wait'):continue
            phase=r['status'].split('_')[0]
            if phase=='pickup' and not self._retry_admission_valid(r):
                self.cancel(r['id']);changed=True;continue
            key=(phase,r['vehicle_id'],r['origin_id'],r['destination_id'])
            prior=self._handling_frontiers.get(r['id'])
            if handling is not None and r['id'] not in handling and prior is not None and prior[0]==key:
                continue
            self._forget_handling(r['id'])
            if getattr(self,'probes',None):self.probes.count_work('transfer_handling_considered')
            if self._handling_blocked(r,phase):self._remember_handling(r,phase,key)
            else:self._start_handling(r,phase);changed=True
        if self._assignment_dispatch_required():changed=self.assign() or changed
        elif getattr(self,'probes',None):self.probes.count_work('transfer_assignment_empty_exits')
        changed=self.exchange.dispatch() or changed
        active_ids=set(self.active.values())
        self._admission_retries={did:value for did,value in self._admission_retries.items()
            if did in active_ids}
        self._neg.retain(active_ids)
        return changed

    def fault_changed(self, kind, target_id, token, *, paused):
        if kind=='port':self.exchange.port_causes=self.traffic.causes['port']
        for r in [self.demands[did] for did in self.active.values()]:
            if r['status'] not in ('pickup_handling','drop_handling'):continue
            port=self.dock_id(r['origin_id'] if r['status']=='pickup_handling' else r['destination_id'])
            if kind=='oht' and r['vehicle_id']==target_id or kind=='port' and port==target_id:
                if paused:r['timer'].pause(self.scheduler.now_us,token)
                elif token in r['timer'].causes:r['timer'].resume(self.scheduler.now_us,token)
        if kind=='port':
            for r in self.exchange.requests.values():
                port=r['origin_id'] if r['inbound'] else r['destination_id']
                if r['status']=='active' and port==target_id:
                    timer=self.exchange.handlers[r['tool_id']].active['timer']
                    if paused:timer.pause(self.scheduler.now_us,token)
                    elif token in timer.causes:timer.resume(self.scheduler.now_us,token)
        self.dirty()


# Capture native methods once: replacing a class or instance method disables reuse.
from .manufacturing import (Manufacturing as _AdmissionManufacturing,
    RouteRules as _AdmissionRules, SetupState as _AdmissionSetup,
    BatchAssembly as _AdmissionBatch)
from fractions import Fraction as _AdmissionFraction
from .campaigns import SetupCampaigns as _AdmissionCampaigns
_ADMISSION_NATIVE=Transfers._admission_valid
_VISIT_NATIVE=Transfers._visit_valid
_TARGET_NATIVE=Transfers._target_valid
_STEP_NATIVE=_AdmissionManufacturing._step
_ELIGIBLE_NATIVE=_AdmissionRules.eligible_tools
_COMPONENTS_NATIVE=_AdmissionRules._components
_SETUP_NATIVE=tuple((name,getattr(_AdmissionSetup,name))
    for name in ('can_use','decision'))
_CAMPAIGN_NATIVE=tuple((name,getattr(_AdmissionCampaigns,name))
    for name in ('valid_admission','admission','allows_admission'))


def _admission_value(value):
    """Snapshot supported native values without custom equality/iteration hooks."""
    if value is None or type(value) in (str,int,bool,float):return (type(value).__name__,value)
    if type(value) in (tuple,list):
        return (type(value).__name__,tuple(_admission_value(item) for item in value))
    if type(value) is dict:
        return ('dict',tuple((_admission_value(key),_admission_value(item))
            for key,item in value.items()))
    if type(value) is _AdmissionFraction:
        return ('Fraction',value.numerator,value.denominator)
    raise TypeError('unsupported admission dependency')

_ASSIGN_NATIVE=Transfers.assign

_HANDLING_BLOCKED_NATIVE=Transfers._handling_blocked
_DIRTY_NATIVE=Transfers.dirty

_DOCK_ID_NATIVE=Transfers.dock_id


import os as _os
import re as _re
_SLOT_TOOL=_re.compile(r'^(?:[a-z]+:)?([^:]+:tool:\d+)(?::|$)')
# Events that change nothing a destination search reads (vehicle motion, carrier
# custody already reported by custody notifications, wafer units inside a tool).
_NEG_SILENT=('traffic_','transfer_','cascade_','oht_','dispatch_cap_')


class NegativeCache:
    """Remember empty destination searches until an input they read changes.

    Speed only: a cached demand is skipped exactly when a fresh search would
    again be empty. A search reads the custody of its destination tools' docks
    and racks (and the source tool's ports for hand-offs), those tools'
    manufacturing state, buffers when it fell back to overflow, and shared
    state (clearance, lots, visits). Every change bumps a stamp: custody slot
    changes the slot's tool (buffers: the buffer stamp), resource publications
    their tools/groups, and every domain event the tools it names, else the
    global stamp. FAB_NEG_VERIFY=1 recomputes every hit and fails on a mismatch.
    """
    def __init__(self,owner):
        self.owner=owner;self.clock=0;self.global_stamp=0;self.buffer_stamp=0
        self.tool_stamp={};self.group_stamp={};self.tool_groups={};self.entries={}
        self.used_buffers=False;self.verify=_os.environ.get('FAB_NEG_VERIFY')=='1'
        self.hits=0;self.misses=0;self.group_tools=None

    def _tick(self):
        self.clock+=1;return self.clock

    def bump_tool(self,tid):
        now=self._tick();self.tool_stamp[tid]=now
        for g in self.tool_groups.get(tid,()):self.group_stamp[g]=now

    def bump_global(self):self.global_stamp=self._tick()

    def slots_changed(self,slot_ids):
        slots=self.owner.custody.slots
        for sid in slot_ids:
            slot=slots.get(sid)
            if slot is None:self.bump_global();continue
            kind=slot.get('kind')
            if kind=='vehicle':continue
            if kind=='track_buffer':self.buffer_stamp=self._tick();continue
            tid=slot.get('equipment_id')
            if tid:self.bump_tool(tid)
            else:self.bump_global()

    def _group_tools(self,group):
        tools=self.group_tools
        if tools is None:
            tools=self.group_tools={}
            for tid,tool in self.owner.manufacturing.tools.items():tools.setdefault(tool['tool_group_id'],[]).append(tid)
        return tools.get(group)

    def bump_group(self,group):
        tools=self._group_tools(group)
        if tools is None:self.bump_global();return
        for tid in tools:self.bump_tool(tid)

    def bump_peers(self,tid):
        # A tool's availability, setup or admission state is read by searches for
        # its group peers too (switch_on_empty: an available peer holding a recipe
        # releases another tool's switch), so manufacturing-side changes invalidate
        # the whole group. Custody changes stay per tool (slots_changed).
        tool=self.owner.manufacturing.tools.get(tid)
        if tool is None:self.bump_tool(tid)
        else:self.bump_group(tool['tool_group_id'])

    def resources_changed(self,change):
        if change.rail_ids:self.bump_global()
        for group in change.group_ids:self.bump_group(group)
        for tid in change.tool_ids:self.bump_peers(tid)

    def event(self,kind,payload):
        if kind.startswith(_NEG_SILENT):return
        if kind=='visit_entered':
            # A new visit can change admission (campaigns, batches) at its step's tools only.
            m=self.owner.manufacturing;visit=m.rules.visits.get(payload.get('id'))
            if visit is not None:self.bump_group(m._step(visit)['tool_group_id']);return
        # Clearance leases protect buffers from ordinary overflow.
        if kind.startswith('clearance_'):self.buffer_stamp=self._tick()
        named=False
        for key in ('tool_id','equipment_id','target_id','owner_id','origin_id','destination_id',
                    'rack_slot_id','source_rack_id','output_port_id','port_id','slot_id'):
            value=payload.get(key) if isinstance(payload,dict) else None
            if isinstance(value,str):
                m=_SLOT_TOOL.match(value)
                if m:self.bump_peers(m.group(1));named=True
                elif key in ('tool_id','equipment_id'):self.bump_peers(value);named=True
        if not named:self.bump_global()

    def begin(self):
        self.used_buffers=False
        return self._tick()

    def _group(self,r,extra):
        t=self.owner;tools=[]
        for sid in r['destinations']:
            tid=t.docks[sid].get('equipment_id') if sid in t.docks else None
            if tid is None:return None
            tools.append(tid)
        g=tuple(sorted(set(tools)|set(extra)))
        if g not in self.group_stamp:
            self.group_stamp[g]=0
            for tid in g:self.tool_groups.setdefault(tid,[]).append(g)
        return g

    def store(self,r,origin,stamp,extra,payload):
        if r['visit_id'] is None:return
        g=self._group(r,extra)
        if g is None:return
        self.entries[r['id']]=(origin,stamp,g,self.used_buffers,payload,tuple(r['destinations']))

    def hit(self,did,origin,check=None,same=None):
        e=self.entries.get(did)
        if e is None:return False
        o,stamp,g,buffers,payload,dests=e
        if (o!=origin or self.global_stamp>stamp or tuple(self.owner.demands[did]['destinations'])!=dests
                or (same is not None and not same(payload)) or self.group_stamp.get(g,0)>stamp
                or (buffers and self.buffer_stamp>stamp)):
            del self.entries[did];self.misses+=1;return False
        self.hits+=1
        if self.verify:
            t=self.owner;r=t.demands[did]
            if check is None:fresh=t._choices(r,origin,with_paths=False)
            else:fresh=check()
            if fresh:raise RuntimeError(f'negative cache stale for {did} at {t.scheduler.now_us}')
        return True

    def payload(self,did):
        return self.entries[did][4]

    def retain(self,active_ids):
        # Cancelled, reused or completed demands never search again.
        entries=self.entries
        if any(did not in active_ids for did in entries):
            self.entries={did:e for did,e in entries.items() if did in active_ids}
