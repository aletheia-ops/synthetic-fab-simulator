"""Finite release/admission and route-to-transfer wiring at dispatch boundaries."""
from collections import deque
from fractions import Fraction
from .workload import admit
from .resource_changes import DirtyResources


def root_batch_staging_requirement(routes,source,capacities=None):
    """Entry capacity ensuring a full stocker offers some root batch prefix.

    Include every selected cohort, even those without an exact root batch visit
    count, so an unprotected cohort cannot occupy the guard's needed staging.
    """
    keys={(stream['product_id'],stream['route_id']) for stream in source.streams.values()
          if source.quotas[stream['product_id']]>0}
    bounds=[];protected=False
    for product,rid in sorted(keys):
        root=routes[rid]['steps'][0]
        quota=source.quotas[product]
        exact=(root['kind']=='processing' and root.get('batch_min')
            and Fraction(root['sampling_probability'])==1
            and {stream['route_id'] for stream in source.streams.values()
                 if stream['product_id']==product}=={rid}
            and not any(step.get('rework') and Fraction(step['rework']['probability'])>0
                and step['rework']['target_step_id']==root['source_step_id'] for step in routes[rid]['steps']))
        if exact and capacities is not None:
            racks=capacities.get(root['tool_group_id'],[])
            exact=bool(racks) and min(racks)>=root['batch_max']//25
        if exact and quota>=(root['batch_min']+24)//25:
            protected=True;bounds.append(min(root['batch_max']//25,quota)-1)
        else:
            # Unknown or permanently short cohorts may retain their entire
            # quota in entry, including after a sampled root is skipped.
            bounds.append(quota)
    return 1+sum(bounds) if protected else 0


def ongoing_root_staging_requirements(routes,source):
    """Per-root lower bounds; stream interleaving is not a liveness proof."""
    if source.mode!='continuous':raise ValueError('root lower bounds require ongoing release')
    keys={(stream['product_id'],stream['route_id']) for stream in source.streams.values()
          if source.weights[stream['product_id']]>0}
    result=[]
    for product,rid in sorted(keys):
        root=routes[rid]['steps'][0]
        if root['kind']=='processing' and root.get('batch_min') and Fraction(root['sampling_probability'])==1:
            result.append({'product_id':product,'route_id':rid,'minimum_lots':(root['batch_min']+24)//25})
    return tuple(result)


class Flow:
    def __init__(self, source, manufacturing, transfers, entry_slots, exit_slots, emit, *, initialize=True,geometry_cache=None):
        self.source=source;self.manufacturing=manufacturing;self.transfers=transfers
        self.scheduler=manufacturing.scheduler;self.custody=manufacturing.custody
        self.lots=manufacturing.lots;self.emit=emit
        if (source.mode not in ('finite','continuous') or transfers.manufacturing is not manufacturing
                or self.scheduler.now_us!=0 or self.lots or not callable(emit)):
            raise ValueError('physical flow requires shared empty startup state')
        self.entry_slots=tuple(sorted(entry_slots));self.exit_slots=tuple(sorted(exit_slots))
        for slots,kind in ((self.entry_slots,'entry'),(self.exit_slots,'exit')):
            if not slots or len(slots)!=len(set(slots)) or any(
                    sid not in transfers.slot_docks or transfers.docks[sid]['kind']!=kind
                    or not self.custody.available(sid) for sid in slots):
                raise ValueError('flow requires empty entry/exit stocker slots')
        self.pending=deque();self.source_exhausted=False;self.next_intent=None
        self._active_lots={};self._lot_by_carrier={};self._dirty_lots=DirtyResources();self._sorted_active=None
        self.admitted_by_product={}
        from .history_archive import empty_retired_facts
        self.history_archive=None;self.retired_facts=empty_retired_facts();self._history_lookup=None
        self.retirement_metrics={'largest_record_bytes':0,'oversize_attempts':0}
        self._delivery_ports=None;self._delivery_router=None;self._delivery_costs={};self._geometry_cache=geometry_cache
        manufacturing.carrier_lot_lookup=self._carrier_lot
        manufacturing.current_lot_items=self._current_lot_items
        manufacturing.carrier_lot_id=self._carrier_lot_id
        manufacturing.batches.delivery_cost=self.delivery_cost
        manufacturing.batches.exact_population=self.exact_batch_population
        self.scheduler.register('flow:release',self._release)
        self.scheduler.register_pass('admission',self.dispatch)
        self.custody.subscribe_resources(self.resource_changed)
        manufacturing.subscribe_resources(self.resource_changed)
        if initialize:self._schedule_next()

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

    def exact_batch_population(self,lot,step):
        """Exact remaining visits only for a mandatory, non-revisited root.

        Sampled steps, downstream steps and multiple release routes retain the
        conservative runtime shortage assessment; their visit totals are unknown.
        """
        if self.source.mode=='continuous':return None
        rules=self.manufacturing.rules;rid=lot['route_id'];product=lot['product_id']
        root=rules.routes[rid]['steps'][0]
        if (step['source_step_id']!=root['source_step_id'] or root['kind']!='processing'
                or Fraction(root['sampling_probability'])!=1
                or rules._predecessors[rid][root['source_step_id']]
                or {stream['route_id'] for stream in self.source.streams.values()
                    if stream['product_id']==product}!={rid}):return None
        by_group={}
        for tool in self.manufacturing.tools.values():
            by_group.setdefault(tool['tool_group_id'],[]).append(len(tool['rack_slots']))
        if len(self.entry_slots)<root_batch_staging_requirement(rules.routes,self.source,by_group):return None
        capacities=[len(tool['rack_slots']) for tool in self.manufacturing.tools.values()
                    if tool['tool_group_id']==root['tool_group_id']]
        if not capacities or min(capacities)<root['batch_max']//25:return None
        consumed={visit['lot_id'] for visit in rules.visits.values()
            if visit.get('assembly_id') and visit['source_step_id']==root['source_step_id']
            and self.lots[visit['lot_id']]['product_id']==product
            and self.lots[visit['lot_id']]['route_id']==rid}
        return self.source.quotas[product]-len(consumed)-self.retired_facts['root_consumed'].get((product,rid),0)

    def delivery_cost(self,carrier,tool_id):
        """Directed free-flow delivery cost, independent of transient occupancy."""
        origin=self.custody.locations[carrier];key=(origin,tool_id)
        # A function of the fixed topology and handling times only.
        if key not in self._delivery_costs:self._delivery_costs[key]=self._delivery_cost(origin,tool_id)
        return self._delivery_costs[key]

    def _delivery_cost(self,origin,tool_id):
        t=self.transfers;slot=self.custody.slots[origin]
        if origin in self.manufacturing.tools[tool_id]['rack_slots']:return Fraction(0)
        if self._delivery_ports is None:
            inputs={};outputs={};poses={}
            # Physical topology is fixed for the lifetime of a run. Faults and
            # reservations are deliberately absent from free-flow ranking.
            for sid,dock in t.docks.items():
                tid=dock.get('equipment_id');role=dock.get('role')
                if role in ('OUT','BOTH'):outputs.setdefault(tid,[]).append(sid)
                if tid in self.manufacturing.tools and role in ('IN','BOTH'):
                    inputs.setdefault(tid,[]).append(sid);poses[sid]=t._pose(sid)
            self._delivery_ports=(inputs,outputs,poses)
        inputs,outputs,poses=self._delivery_ports
        origins=[origin] if origin in t.docks else outputs.get(slot.get('equipment_id'),()) if slot['kind']=='rack' else ()
        destinations=inputs.get(tool_id,())
        if not origins or not destinations:return None
        from .routing_index import FreeFlowCosts
        if self._delivery_router is None:
            self._delivery_router=t._router() if hasattr(t,'_router') else FreeFlowCosts(t.model,t.vehicle_type,cache=self._geometry_cache)
        targets={pid:poses[pid] for pid in destinations}
        costs=[]
        for sid in origins:
            row=self._delivery_router.costs(t._pose(sid),targets)
            costs.extend(row.values())
        return min(costs)+Fraction(t.handling['pickup']+t.handling['drop'],1_000_000) if costs else None

    def _active_ids(self):
        # Sorted active LOT ids, rebuilt only after an admission or exit.
        if self._sorted_active is None:self._sorted_active=tuple(sorted(self._active_lots))
        return self._sorted_active

    def current_lots(self):
        return tuple(self.lots[lid] for lid in self._active_ids())

    def _selected_lots(self,selected):
        # Same lots and order as filtering current_lots() by `selected`
        # (None means all), without sorting every active LOT.
        if selected is None:return self.current_lots()
        active=self._active_lots
        return tuple(self.lots[lid] for lid in sorted(selected) if lid in active)

    def _current_lot_items(self):
        return tuple((lid,self.lots[lid]) for lid in self._active_ids())

    def _carrier_lot_id(self,carrier):
        return self._lot_by_carrier.get(carrier)

    def _carrier_lot(self,carrier):
        lid=self._lot_by_carrier.get(carrier)
        if lid is None:raise ValueError('carrier requires a unique live LOT')
        return lid,self.lots[lid]

    def pending_admission_summary(self,now_us):
        if type(now_us) is not int or now_us<0 or self.pending and now_us<self.pending[0]['scheduled_at_us']:
            raise ValueError('invalid admission observation tick')
        return {'pending_admission_count':len(self.pending),'oldest_pending_admission_age_us':
            now_us-self.pending[0]['scheduled_at_us'] if self.pending else None}

    def dirty(self):
        self._dirty_lots.mark();self.scheduler.mark_dirty('admission')

    def resource_changed(self,change):
        index=getattr(self.manufacturing,'resource_index',None)
        if (index is not None and (change.tool_ids or change.group_ids) and not change.vehicle_ids
                and not change.rail_ids and all(tid in index.group_by_tool for tid in change.tool_ids)
                and all(gid in index.tools_by_group for gid in change.group_ids)
                and all(sid in self.custody.slots for sid in change.slot_ids)):
            # A tool or group change (also a LOT entering, starting or
            # finishing a visit, or an execution loading/unloading) concerns
            # LOTs whose current visit uses the group (readiness, setup credits,
            # campaigns are group-level), LOTs sitting at the tool's racks or
            # ports, and the named carriers and slots.
            m=self.manufacturing;slots=self.custody.slots;lots=set()
            groups={index.group_by_tool[tid] for tid in change.tool_ids}|set(change.group_ids)
            for group in groups:
                for visit in m.rules._active_by_group.get(group,{}).values():lots.add(visit['lot_id'])
            for tid in change.tool_ids:
                for sid in (*m.tools[tid]['rack_slots'],*index.input_ports(tid),*index.output_ports(tid)):
                    carrier=slots[sid]['carrier_id'] if sid in slots else None
                    if carrier in self._lot_by_carrier:lots.add(self._lot_by_carrier[carrier])
            lots.update(self._lot_by_carrier[c] for c in change.carrier_ids if c in self._lot_by_carrier)
            for sid in change.slot_ids:
                carrier=slots[sid]['carrier_id']
                if carrier in self._lot_by_carrier:lots.add(self._lot_by_carrier[carrier])
            self._dirty_lots.mark(lots);self.scheduler.mark_dirty('admission');return
        if index is None or (not change.slot_ids and not change.carrier_ids) or change.tool_ids or change.group_ids or change.vehicle_ids or change.rail_ids:
            # Recipe/run credits and shared campaign decisions still use a full
            # active pass until every contender dependency has been audited.
            self.dirty();return
        if any(sid not in self.custody.slots for sid in change.slot_ids):
            self.dirty();return
        self._dirty_lots.mark(self._lot_by_carrier[carrier] for carrier in change.carrier_ids
                              if carrier in self._lot_by_carrier)
        self.scheduler.mark_dirty('admission')

    def _considered(self):
        probes=getattr(self,'probes',None)
        if probes is not None:probes.count_work('flow_lots_considered')

    def _schedule_next(self):
        self.next_intent=self.source.next_intent()
        if self.next_intent is None:self.source_exhausted=True
        else:self.scheduler.schedule(self.next_intent['scheduled_at_us'],3,'flow:release',{})

    def _release(self,payload):
        intent=self.next_intent
        self.pending.append(intent)
        self.emit('release_intent_due',dict(intent))
        self._schedule_next();self.dirty()

    def dispatch(self):
        selected=self._dirty_lots.take()
        if self.manufacturing.campaigns:
            if self.manufacturing.campaigns.dispatch():selected=None
        # Drop-off already includes stocker storage/hoisting. Removal at this
        # milestone adds neither an invented delay nor another carrier move.
        for lot in self._selected_lots(selected):
            lid=lot['lot_id']
            self._considered()
            carrier=lot['carrier_id'];sid=self.custody.locations.get(carrier)
            if sid not in self.exit_slots:continue
            if (lot.get('route_complete_at_us') is None or lot.get('current_visit_id') is not None
                    or carrier in self.transfers.active or carrier in self.manufacturing.claims):
                raise RuntimeError('exit carrier has unfinished domain work')
            owner='exit:'+lid
            with self.custody.batch_notifications():
                if not self.custody.reserve_many([],owner,origins={sid:carrier}):continue
                self.custody.remove(carrier,sid,owner)
                lot.update(status='exited',exited_at_us=self.scheduler.now_us)
                del self._active_lots[lid];del self._lot_by_carrier[carrier];self._sorted_active=None
            self.emit('lot_exited',{'lot_id':lid,'carrier_id':carrier,'slot_id':sid,
                                  'exited_at_us':self.scheduler.now_us})
        while self.pending:
            intent=self.pending[0]
            if not admit(intent,{'lots':self.lots,'custody':self.custody,'entry_slots':self.entry_slots,
                                 'now_us':self.scheduler.now_us}):break
            self.pending.popleft();lid=intent['lot_id']
            product=self.lots[lid]['product_id']
            self.admitted_by_product[product]=self.admitted_by_product.get(product,0)+1
            self._active_lots[lid]=self.lots[lid];self._lot_by_carrier[self.lots[lid]['carrier_id']]=lid;self._sorted_active=None
            if selected is not None:selected=selected|{lid}
            self.emit('lot_admitted',dict(self.lots[lid]))
            first=self.manufacturing.rules.routes[intent['route_id']]['steps'][0]['source_step_id']
            self.manufacturing.enter_visit(lid,first)
        for lot in self._selected_lots(selected):
            lid=lot['lot_id']
            self._considered()
            carrier=lot['carrier_id'];vid=lot.get('current_visit_id')
            if lot.get('exited_at_us') is not None or carrier in self.transfers.active or carrier in self.manufacturing.claims:continue
            if vid is None:
                if lot.get('route_complete_at_us') is None:continue
                destinations=self.exit_slots
            else:
                visit=self.manufacturing.rules.visits[vid]
                if visit['status']!='pending' or visit.get('execution_id'):continue
                step=self.manufacturing._step(visit)
                origin=self.custody.locations[carrier];slot=self.custody.slots[origin]
                tool=self.manufacturing.tools.get(slot.get('equipment_id'),{})
                old=self.manufacturing.batches.assemblies.get(tool.get('assembly_id'))
                clearing=bool(slot['kind']=='rack' and old and old['status']=='finished' and any(
                    member['carrier_id']==carrier and not member['departed'] for member in old['members']))
                # A batch-bound LOT waits where it is until an assembly claims it,
                # except (Stage 0 storage, next_group_bays) on a tool rack: tools
                # hold only their ports, so it moves to an overflow buffer.
                if (step.get('batch_min') and not visit.get('assembly_id') and not clearing
                        and not (getattr(self.transfers,'buffer_selection',None)=='next_group_bays'
                                 and slot['kind']=='rack' and slot.get('equipment_id') in self.manufacturing.tools)):continue
                # A finished batch must physically clear even if its next visit
                # uses this same tool; ordinary ready rack work stays in place.
                if (not clearing and self.manufacturing._rack_ready(vid)
                        and (self.manufacturing.campaigns is None
                             or self.manufacturing.campaigns.can_reside(vid,slot['equipment_id']))):continue
                # Only input docks of the step's tool group can be valid targets.
                index=getattr(self.manufacturing,'resource_index',None);docks=self.transfers.docks
                pool=(index.input_docks_by_group.get(step['tool_group_id'],()) if index is not None else docks)
                destinations=tuple(sorted(sid for sid in pool if sid in docks
                    for dock in (docks[sid],)
                    if dock.get('role') in ('IN','BOTH') and dock.get('equipment_id') in self.manufacturing.tools
                    and self.transfers._target_valid({'visit_id':vid,'lot_id':lid},sid)))
                if not destinations:raise ValueError('route visit has no eligible physical input dock')
            self.transfers.request(carrier,vid,destinations)
