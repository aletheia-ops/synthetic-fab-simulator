"""Source-derived finite buffer capacity and all-or-none equipment clearance."""
from .manufacturing import Manufacturing, RouteRules
from .transfers import Transfers
from .routing_index import FreeFlowCosts
from .derived_cache import DerivedCache


# Stable identities preserve fallback after class-level extensions are installed.
_NATIVE_TARGET_VALID = Transfers._target_valid
_NATIVE_ELIGIBLE_TOOLS = RouteRules.eligible_tools
_NATIVE_COMPONENTS = RouteRules._components
_NATIVE_STEP = Manufacturing._step
_NATIVE_POSE = Transfers._pose
_NATIVE_REACHABLE = Transfers._reachable
_NATIVE_ROUTER = Transfers._router
_NATIVE_CACHE_GET = DerivedCache.get
_NATIVE_CACHE_PUT = DerivedCache.put
_NATIVE_ROUTER_METHODS = {name:getattr(FreeFlowCosts,name)
    for name in ('path','reachable','_pose','_row')}


def clearance_requirements(manufacturing, topology):
    from .routing import DirectedReachability
    source=manufacturing['model'];model=topology.get('model',topology)
    connectivity=DirectedReachability(model)
    tools=[e for e in model['equipment'] if e['kind']=='ordinary']
    docks={d['id']:d for d in model['docks']}
    buffers=sorted(d['id'] for d in docks.values() if d['kind']=='track_buffer'
                   and d.get('role','BOTH')=='BOTH')
    vehicle_type=model['vehicles'][0]['type_id'] if model['vehicles'] else None
    tool_ids={tool['id'] for tool in tools}
    input_destinations={sid:d for sid,d in docks.items()
        if d.get('equipment_id') in tool_ids and d.get('role','BOTH') in ('IN','BOTH')}
    buffer_destinations={sid:docks[sid] for sid in buffers}
    rows={}
    # Strongly connected: every origin reaches every destination, so all origins
    # share one row per destination kind instead of a private copy each.
    shared={id(input_destinations):frozenset(input_destinations),id(buffer_destinations):frozenset(buffer_destinations)}
    def reachable(a,b):
        if a not in rows:
            if vehicle_type is None:rows[a]=frozenset()
            elif connectivity.connected:
                rows[a]=shared[id(input_destinations if a in buffer_destinations else buffer_destinations)]
            else:
                # A buffer needs paths to equipment inputs; an equipment
                # output needs paths to buffers. Reuse one directed search
                # per origin, retaining only reachable IDs rather than costs.
                destinations=input_destinations if a in buffer_destinations else buffer_destinations
                rows[a]=frozenset(sid for sid,pose in destinations.items() if connectivity.reachable(docks[a],pose))
        return b in rows[a]
    constraints=[];batch_bound=setup_bound=0
    for tool in tools:
        inputs=[d['id'] for d in docks.values() if d.get('equipment_id')==tool['id'] and d.get('role','BOTH') in ('IN','BOTH')]
        outputs=[d['id'] for d in docks.values() if d.get('equipment_id')==tool['id'] and d.get('role','BOTH') in ('OUT','BOTH')]
        targets={(step.get('setup') or {}).get('id') for route in source['routes'] for step in route['steps']
                 if step.get('tool_group_id')==tool['tool_group_id'] and step.get('setup')}
        initial=next((g.get('initial_setup') for g in source.get('tool_groups',[]) if g['id']==tool['tool_group_id']),None)
        if initial is not None:targets.add(initial)
        else:
            for route in source['routes']:
                installed=False
                for step in route['steps']:
                    if step.get('tool_group_id')!=tool['tool_group_id'] or step.get('kind')!='processing':continue
                    if step.get('setup'):installed=True
                    elif not installed:targets.add(None)
        for route in source['routes']:
            bound=0
            for step in route['steps']:
                if step.get('tool_group_id')!=tool['tool_group_id']:continue
                batch=min((step.get('batch_max') or 0)//25,tool['rack_capacity']) if step.get('batch_min') else 0
                target=(step.get('setup') or {}).get('id')
                setup=max((t['minimum_runs'] for t in source.get('setup_transitions',[]) if len(targets)>1 and target is not None and t['new']==target),default=0)
                batch_bound=max(batch_bound,batch);setup_bound=max(setup_bound,setup)
                bound=max(bound,batch,setup)
            if not bound:continue
            compatible={sid for sid in buffers if (docks[sid].get('compatible_products') is None
                or route['product_id'] in docks[sid]['compatible_products'])
                and any(reachable(a,sid) for a in outputs) and any(reachable(sid,b) for b in inputs)}
            constraints.append((tool['id'],route['id'],bound,compatible))
    required=max(batch_bound,setup_bound)+1 if constraints else 0
    findings=[];selected=[]
    # Search deterministically, pruning when even all remaining compatible slots
    # cannot satisfy a constraint. Usually the first sorted subset is valid.
    def select(chosen,remaining):
        if any(len(set(chosen)&valid)+min(required-len(chosen),len(set(remaining)&valid))<bound+1
               for _,_,bound,valid in constraints):return None
        if len(chosen)==required:return chosen
        if len(remaining)<required-len(chosen):return None
        for i,sid in enumerate(remaining):
            answer=select(chosen+[sid],remaining[i+1:])
            if answer is not None:return answer
        return None
    if required:
        selected=select([],buffers) or []
        if not selected:
            findings=[{'severity':'error','code':'clearance_capacity_unavailable','entity_ids':sorted({tid for tid,_,_,_ in constraints}),
                       'evidence':{'required_capacity':required,'buffer_count':len(buffers),
                        'constraints':[{'tool_id':tid,'route_id':rid,'required_members':bound,'compatible_reachable_slots':sorted(valid)}
                            for tid,rid,bound,valid in constraints]}}]
    return {'required_capacity':required,'batch_bound':batch_bound,'setup_bound':setup_bound,
            'protected_slot_ids':selected,'ordinary_capacity':len(buffers)-len(selected),'findings':findings}


class ClearancePool:
    def __init__(self,manufacturing,transfers,protected_slot_ids,emit):
        if getattr(transfers,'clearance',None) is not None:raise ValueError('clearance already installed')
        ids=tuple(sorted(protected_slot_ids))
        if len(ids)!=len(set(ids)) or any(sid not in transfers.docks or transfers.docks[sid]['kind']!='track_buffer'
                or transfers.docks[sid].get('role','BOTH')!='BOTH' for sid in ids):
            raise ValueError('invalid protected buffer slots')
        self.manufacturing,self.transfers,self.emit=manufacturing,transfers,emit
        self.protected_slot_ids=ids;self.records={};self.waits={};self.sequence=0;self.history_archive=None
        self._plain_docks_key=None;self._plain_docks_value=False;self._setup_groups={}
        self.rebuild_derived()
        transfers.clearance=self
        manufacturing.buffer_owner=self.buffer_owner
        manufacturing.batches.buffer_owner=self.buffer_owner

    def rebuild_derived(self):
        self._active_records={};self._active_by_tool={};self._active_by_visit={};self._active_by_carrier_slot={}
        for record in self.records.values():
            if record['status']=='active':self._index_record(record)

    def _index_record(self,record):
        rid=record['id'];self._active_records[rid]=record
        self._active_by_tool.setdefault(record['tool_id'],{})[rid]=record
        for member in record['members']:
            self._active_by_visit.setdefault(member['visit_id'],{})[rid]=record
            self._active_by_carrier_slot.setdefault((member['carrier_id'],member['slot_id']),{})[rid]=record

    def _retire_record(self,record):
        rid=record['id'];self._active_records.pop(rid,None)
        keys=[(self._active_by_tool,record['tool_id'])]
        keys.extend((self._active_by_visit,m['visit_id']) for m in record['members'])
        keys.extend((self._active_by_carrier_slot,(m['carrier_id'],m['slot_id'])) for m in record['members'])
        for mapping,key in keys:
            values=mapping.get(key)
            if values is not None:
                values.pop(rid,None)
                if not values:del mapping[key]

    def _plain_docks(self,docks):
        # Prepared dock records are immutable at runtime: check the mapping
        # once (per mapping object and size) instead of on every assignment.
        key=(id(docks),len(docks))
        if self._plain_docks_key!=key:
            self._plain_docks_value=all(type(dock) is dict and (
                dock.get('compatible_products') is None
                or type(dock['compatible_products']) in (list,tuple)
                and all(type(product) is str for product in dock['compatible_products']))
                for dock in docks.values())
            self._plain_docks_key=key
        return self._plain_docks_value

    def _first_assignment(self,members,slots):
        # Feasibility pruning preserves the first complete DFS assignment.
        native=(bool(members) and (len(slots)>1 or len(slots)<len(members))
                and getattr(self._compatible,'__func__',None) is _NATIVE_COMPATIBLE
                and getattr(self._compatibility_context,'__func__',None) is _NATIVE_CONTEXT)
        if native:
            t=self.transfers;m=self.manufacturing
            router=getattr(t,'_cost_router',None)
            native=(getattr(t._target_valid,'__func__',None) is _NATIVE_TARGET_VALID
                and getattr(m.rules.eligible_tools,'__func__',None) is _NATIVE_ELIGIBLE_TOOLS
                and getattr(getattr(m.rules,'_components',None),'__func__',None) is _NATIVE_COMPONENTS
                and getattr(m._step,'__func__',None) is _NATIVE_STEP
                and getattr(t._pose,'__func__',None) is _NATIVE_POSE
                and getattr(getattr(t,'_reachable',None),'__func__',None) is _NATIVE_REACHABLE
                and getattr(getattr(t,'_path',None),'__func__',None) is Transfers._native_path
                and getattr(getattr(t,'_router',None),'__func__',None) is _NATIVE_ROUTER
                and type(router) is FreeFlowCosts
                and all(getattr(getattr(router,name),'__func__',None) is method
                    for name,method in _NATIVE_ROUTER_METHODS.items())
                and type(router.cache) is DerivedCache
                and getattr(router.cache.get,'__func__',None) is _NATIVE_CACHE_GET
                and getattr(router.cache.put,'__func__',None) is _NATIVE_CACHE_PUT)
        if native:
            # Callback-bearing containers must retain the ordinary per-pair
            # evaluation order, just like overridden methods above.
            native=(type(t.docks) is dict and type(m.tools) is dict
                and type(m.lots) is dict and type(m.rules.visits) is dict
                and type(m.rules.routes) is dict
                and all(type(tool) is dict and type(tool['tool_group_id']) is str for tool in m.tools.values())
                and self._plain_docks(t.docks))
        if native:
            for member in members:
                if not native:break
                lot=m.lots[m.rules.visits[member['visit_id']]['lot_id']]
                components=m.rules.routes[lot['route_id']]['dedication_components']
                native=(native and type(lot) is dict and type(lot['dedication']) is dict
                    and all(type(key) is str and type(value) is str for key,value in lot['dedication'].items())
                    and type(components) in (list,tuple)
                    and all(type(component) is dict and type(component['id']) is str
                        and type(component['source_step_ids']) in (list,tuple)
                        and all(type(step) is str for step in component['source_step_ids'])
                        for component in components))
        if native:
            # Distinct holding slots cannot house a larger cohort. Recheck
            # capacity on each dispatch; resource releases still wake contenders.
            if len(slots)<len(members):return None
            allowed=[]
            for member in members:
                context=None;compatible=[]
                lot=m.lots[m.rules.visits[member['visit_id']]['lot_id']]
                for sid in slots:
                    products=t.docks[sid].get('compatible_products')
                    if products is not None and lot.get('product_id') not in products:continue
                    if context is None:context=self._compatibility_context(member,lot)
                    if self._compatible(member,sid,_context=context):compatible.append(sid)
                allowed.append(tuple(compatible))
        else:
            allowed=[tuple(s for s in slots if self._compatible(m,s)) for m in members]
        def feasible(first,remaining):
            matched={}
            def augment(member,seen):
                for sid in allowed[member]:
                    if sid not in remaining or sid in seen:continue
                    seen.add(sid)
                    if sid not in matched or augment(matched[sid],seen):
                        matched[sid]=member;return True
                return False
            return all(augment(i,set()) for i in range(first,len(members)))
        remaining=set(slots);chosen=[]
        for index in range(len(members)):
            for sid in allowed[index]:
                if sid in remaining and feasible(index+1,remaining-{sid}):
                    chosen.append(sid);remaining.remove(sid);break
            else:return None
        return tuple(chosen)

    def describe_active(self):
        return [self._event(r) for r in self._active_records.values()]+[dict(w) for _,w in sorted(self.waits.items())]

    def output_unowned(self,visit_id):
        # A downstream batch/campaign owns delivery to its reserved rack. Such
        # visits cannot simultaneously evacuate through leased overflow.
        m=self.manufacturing
        return (not m.rules.visits[visit_id].get('assembly_id')
                and not (m.campaigns and visit_id in m.campaigns.visit_owners))

    def _resident(self,tid,carrier):
        m=self.manufacturing;t=self.transfers;lookup=getattr(m,'carrier_lot_lookup',None)
        if lookup is None:lot=next((l for l in m.lots.values() if l['carrier_id']==carrier),None)
        else:
            try:lot=lookup(carrier)[1]
            except ValueError:return None
        if not lot or lot.get('admitted_at_us') is None:return None
        vid=lot.get('current_visit_id');origin=m.custody.locations.get(carrier)
        if (vid is None or origin not in m.tools[tid]['rack_slots'] or carrier in m.claims
                or not self.output_unowned(vid)):return None
        visit=m.rules.visits[vid];request=t.demands.get(t.active.get(carrier))
        if (visit['status']!='pending' or visit.get('execution_id') or m.custody.slots[origin]['reservation'] is not None
                or request and (request['status']!='pending' or request['output_exchange_id'] is not None)):
            return None
        if self.admission(vid):return None
        eligible=m.rules.eligible_tools(vid,list(m.tools))
        if not eligible:return None
        return {'visit_id':vid,'carrier_id':carrier,'origin_id':origin,'status':'planned',
                'age':request['original_request_time_us'] if request else visit['entered_at_us']}

    def _cohort(self,tid):
        m=self.manufacturing;tool=m.tools[tid]
        assembly=m.batches.assemblies.get(tool.get('assembly_id'))
        if assembly and assembly['status']=='finished':
            carriers=[x['carrier_id'] for x in assembly['members'] if not x['departed']
                      and m.custody.locations.get(x['carrier_id']) in tool['rack_slots']]
            members=[self._resident(tid,c) for c in carriers]
            if members and all(members):return 'finished_batch',members
            return None
        # A setup-change cohort needs a resident step of this tool's group
        # whose setup target has positive minimum runs; routes are static.
        # Without staged campaigns, residents never wait for a campaign.
        if getattr(m.campaigns,'policy','staged_campaign')!='staged_campaign':return None
        setup=m.setups.get(tid)
        if setup is not None and not self._setup_cohort_possible(tool['tool_group_id'],setup.transitions):return None
        residents=[self._resident(tid,m.custody.slots[sid]['carrier_id']) for sid in tool['rack_slots']
                   if m.custody.slots[sid]['carrier_id']]
        residents=[x for x in residents if x]
        targets=[]
        for member in residents:
            step=m._step(m.rules.visits[member['visit_id']]);target=(step.get('setup') or {}).get('id')
            if step['tool_group_id']!=tool['tool_group_id'] or target is None or target==m.setups[tid].current:continue
            minimum=max((tr['minimum_runs'] for tr in m.setups[tid].transitions if tr['new']==target),default=0)
            if minimum and m.setups[tid].can_use(step):targets.append((member['age'],target,minimum))
        if not targets:return None
        _,target,minimum=min(targets)
        free=sum(m.custody.available(sid) for sid in tool['rack_slots'])
        needed=min(len(residents),max(0,minimum-free))
        # Resident incompatible successors can also need external staging when
        # there is nominal space but no complete resident campaign can commit.
        if not needed:return None
        members=sorted(residents,key=lambda x:(x['age'],x['visit_id']))[:needed]
        return 'setup_change',members

    def _setup_cohort_possible(self,group,transitions):
        key=(group,id(transitions))
        cached=self._setup_groups.get(key)
        if cached is None:
            targets={tr['new'] for tr in transitions if tr['minimum_runs']}
            cached=bool(targets) and any(step['tool_group_id']==group and (step.get('setup') or {}).get('id') in targets
                for steps in self.manufacturing.rules.steps.values() for step in steps.values())
            self._setup_groups[key]=(cached,transitions)
            return cached
        return cached[0]

    def _compatibility_context(self,member,lot):
        t=self.transfers;m=self.manufacturing
        tid=m.custody.slots[member['origin_id']]['equipment_id']
        outputs=[s for s,d in t.docks.items() if d.get('equipment_id')==tid and d.get('role') in ('OUT','BOTH')]
        eligible=set(m.rules.eligible_tools(member['visit_id'],list(m.tools)))
        native=(getattr(t._target_valid,'__func__',None) is _NATIVE_TARGET_VALID
                and getattr(m.rules.eligible_tools,'__func__',None) is _NATIVE_ELIGIBLE_TOOLS
                and getattr(getattr(m.rules,'_components',None),'__func__',None) is _NATIVE_COMPONENTS
                and getattr(m._step,'__func__',None) is _NATIVE_STEP)
        if native and eligible:
            # The full-fleet result already applies dedication. Keep group
            # membership live, without repeating singleton dedication scans.
            group=m._step(m.rules.visits[member['visit_id']])['tool_group_id']
            inputs=[s for s,d in t.docks.items() if d.get('equipment_id') in eligible
                    and m.tools[d['equipment_id']]['tool_group_id']==group
                    and d.get('role') in ('IN','BOTH')]
        else:
            inputs=[s for s,d in t.docks.items() if d.get('equipment_id') in eligible
                    and t._target_valid({'visit_id':member['visit_id'],'lot_id':lot.get('id',m.rules.visits[member['visit_id']]['lot_id'])},s)
                    and d.get('role') in ('IN','BOTH')]
        return outputs,inputs

    def _compatible(self,member,sid,*,_context=None):
        t=self.transfers;m=self.manufacturing;dock=t.docks[sid]
        lot=m.lots[m.rules.visits[member['visit_id']]['lot_id']]
        if dock.get('compatible_products') is not None and lot.get('product_id') not in dock['compatible_products']:return False
        outputs,inputs=(_context if _context is not None else self._compatibility_context(member,lot))
        reachable=getattr(t,'_reachable',None)
        if reachable is None:reachable=lambda a,b:t._path(a,b) is not None
        return any(reachable(t._pose(a),t._pose(sid)) for a in outputs) and any(
            reachable(t._pose(sid),t._pose(b)) for b in inputs)

    def dispatch(self):
        m=self.manufacturing;proposals=[];self.waits={};changed=False
        for record in tuple(self._active_records.values()):
            stale=[x for x in record['members'] if x['status']=='planned' and (
                m.lots[m.rules.visits[x['visit_id']]['lot_id']].get('current_visit_id')!=x['visit_id']
                or m.custody.locations.get(x['carrier_id'])!=x['origin_id']
                or not self.output_unowned(x['visit_id']))]
            if stale:
                if any(m.custody.slots[x['slot_id']]['reservation']!=record['id']
                        or m.custody.slots[x['slot_id']]['carrier_id'] is not None for x in stale):
                    raise RuntimeError('clearance cancellation ownership changed')
                wholly_unstarted=len(stale)==len(record['members'])
                with m.custody.batch_notifications():
                    for member in stale:
                        m.custody.release(member['slot_id'],record['id']);member['status']='departed'
                    if all(x['status']=='departed' for x in record['members']):
                        record['status']='cancelled' if wholly_unstarted else 'completed'
                        self._retire_record(record)
                self.emit('clearance_lease_cancelled' if wholly_unstarted else 'clearance_unused_members_released',
                    self._event(record));changed=True
        # Only rack residents can form a cohort. Lease cleanup above still runs
        # for every active lease, including tools that have become empty.
        index=getattr(m,'resource_index',None)
        tool_ids=(sorted({index.tool_by_slot[sid] for sid in m.custody.locations.values()
            if sid in index.tool_by_slot}) if index is not None else sorted(m.tools))
        for tid in tool_ids:
            if self._active_by_tool.get(tid):continue
            cohort=self._cohort(tid)
            if cohort:
                kind,members=cohort;proposals.append((min(x['age'] for x in members),tid,kind,members))
        for _,tid,kind,members in sorted(proposals):
            free=[sid for sid in self.protected_slot_ids if m.custody.available(sid)]
            assignment=self._first_assignment(tuple(members),tuple(free))
            slots=list(assignment) if assignment is not None else None
            if slots is None:
                self.waits[tid]={'tool_id':tid,'kind':kind,'required_capacity':len(members),
                    'available_capacity':len(free),'conflicting_leases':sorted(self._active_records)}
                continue
            owner=f'clearance:{self.sequence+1:08}'
            if not m.custody.reserve_many(slots,owner):continue
            self.sequence+=1
            members=[{**member,'slot_id':sid} for member,sid in zip(members,slots)]
            record={'id':owner,'tool_id':tid,'kind':kind,'members':members,'slots':slots,'status':'active'}
            self.records[owner]=record;self._index_record(record)
            self.emit('clearance_lease_committed',self._event(record));changed=True
        return changed

    def _event(self,record):return {**record,'members':[dict(x) for x in record['members']],'slots':list(record['slots'])}

    def admission(self,visit_id):
        for record in self._active_by_visit.get(visit_id,{}).values():
            member=next((x for x in record['members'] if x['visit_id']==visit_id and x['status']!='departed'),None)
            if member:return member['slot_id'],record['id']
        return None

    def buffer_owner(self,carrier_id,slot_id):
        for record in self._active_by_carrier_slot.get((carrier_id,slot_id),{}).values():
            if any(x['carrier_id']==carrier_id and x['slot_id']==slot_id
                    and x['status']=='buffered' for x in record['members']):return record['id']
        return None

    def valid_output(self,visit_id,owner,origin_id):
        record=self.records.get(owner);m=self.manufacturing
        if not record or record['status']!='active':return False
        member=next((x for x in record['members'] if x['visit_id']==visit_id),None)
        if not member or member['status']!='in_transit' or member['origin_id']!=origin_id:return False
        return (self.output_unowned(visit_id)
            and m.custody.locations.get(member['carrier_id'])==origin_id
            and self._compatible(member,member['slot_id']))

    def buffered(self,carrier_id,slot_id):
        self.manufacturing.dirty()
        for record in tuple(self._active_by_carrier_slot.get((carrier_id,slot_id),{}).values()):
            member=next((x for x in record['members'] if x['carrier_id']==carrier_id and x['slot_id']==slot_id
                         and x['status']=='in_transit'),None)
            if member:
                c=self.manufacturing.custody
                if c.locations.get(carrier_id)!=slot_id:raise RuntimeError('clearance arrival precedes delivery')
                if not c.reserve_many([],record['id'],origins={slot_id:carrier_id}):raise RuntimeError('clearance arrival ownership changed')
                member['status']='buffered';self.emit('clearance_member_buffered',self._event(record))

    def departed(self,carrier_id,slot_id):
        for record in tuple(self._active_by_carrier_slot.get((carrier_id,slot_id),{}).values()):
            member=next((x for x in record['members'] if x['carrier_id']==carrier_id and x['slot_id']==slot_id
                         and x['status']=='buffered'),None)
            if member:
                if self.manufacturing.custody.locations.get(carrier_id)==slot_id:raise RuntimeError('clearance departure precedes physical pickup')
                member['status']='departed'
                if all(x['status']=='departed' for x in record['members']):
                    record['status']='completed';self._retire_record(record)
                self.emit('clearance_member_departed',self._event(record))

    def describe(self):return [self._event(r) for r in self.records.values()]+[dict(w) for _,w in sorted(self.waits.items())]


_NATIVE_COMPATIBLE = ClearancePool._compatible
_NATIVE_CONTEXT = ClearancePool._compatibility_context


