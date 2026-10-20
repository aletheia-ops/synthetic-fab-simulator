"""Declared native owner state, shared data references and allocation-only restore."""
from collections import deque, defaultdict, OrderedDict
from dataclasses import dataclass
from fractions import Fraction
from .actions import ActionRef, BoundAction, export_action, restore_action
from .checkpoint_format import CheckpointError
from .manufacturing import SetupState
from .service import Timer, SerializedHandler, Cascade
from .resource_changes import DirtyResources


@dataclass
class RuntimeOwners:
    scheduler: object
    streams: object
    custody: object
    manufacturing: object
    transfers: object
    traffic: object
    source: object
    flow: object
    campaigns: object
    handoffs: object
    clearance: object
    maintenance: object
    amhs_faults: object
    actions: object
    manufacturing_input: dict
    topology_input: dict
    topology_record: dict
    findings: dict
    entry: tuple
    exit: tuple
    scheduler_callbacks: object = None


# Canonical fields are explicit; other named fields are immutable input, wiring,
# or derived caches rebuilt by the current validated native implementation.
SCHEMAS = {
    'custody': ('slots locations', 'version _notification_depth _changed _listeners _resource_listeners _resource_pending_slots _resource_pending_carriers on_change'),
    'manufacturing': ('lots tools setups handlers claims executions executions_archive causes cascade_executions pipelines _active_execution_count _dirty_groups', 'scheduler streams custody emit rules groups batches campaigns dispatch_guards processed_changed _resource_listeners _wait_generation resource_index probes carrier_lot_lookup carrier_lot_id current_lot_items buffer_owner _lot_history_owner super_hot_holds extra_processing actions'),
    'rules': ('visits clocks clock_archive retired_violation_ids _sequence violation_count', 'routes steps scheduler streams lots emit resource_changed _active_visits _active_by_group _live_order _next_live_order _possible_next _predecessors _batch_reaches _deadline_kind _lot_history_owner'),
    'batches': ('assemblies _sequence history_archive', 'rules custody tools setup_states delivery_cost emit eligible_tool exact_population held_out super_hot_expedite buffer_owner _group_tools _group_tools_key'),
    'transfers': ('vehicles demands active movements overflow_visits sequence history_archive movements_archive', 'manufacturing scheduler custody model docks emit exchange handling vehicle_type internal_docks slot_docks clearance handoffs traffic resource_index probes _dirty_handling _handling_frontiers _handling_waiters _admission_retries _event_sink _load_dirty _resident_lots_by_tool _incoming_lots_by_tool _tool_loads _incoming_by_tool _rack_slots_by_tool _cost_router _tool_docks _tool_docks_key _destination_layout _sorted_racks _vehicle_slots _vehicle_starved buffer_selection _buffer_tiers _super_hot_lots congestion_routing_s dispatch_metering _metered _meter_waiting _open_version _open_memo _meter_counts _neg busy_vehicle_cap _cap_engaged super_hot_expedite congestion_destination_s _dock_zones down_tool_avoid_us long_downtime _history_lookup actions'),
    'exchange': ('requests handlers port_causes history_archive', 'manufacturing emit scheduler custody durations _history_lookup actions _pending_tools _dispatch_frontier down_tool_output'),
    'traffic': ('requests active hops targets resources occupancy idle_wait idle_sequence causes base_available history_archive', 'model scheduler vehicles emit rails types holdings docks poses halves passages junctions holding_by_rail holding_offsets transfers manage_idle _dock_offsets_by_rail _denied_hops _recovery_signature _recovery_cycle_cache _recovery_waypoint_rejections _idle_rankings _idle_plans _idle_routers _geometry_cache _idle_target_key _history_lookup _idle_requests _passage_index _claims_version _claim_cache _service_set _rail_versions _claim_epoch _idle_memo hop_lookahead_um bay_entry_divert queue_divert _queue_divert_graph _rail_bay_kind probes actions'),
    'source': ('pending_streams sequence realized_classes realized_products', 'mode count streams source_intervals source_weights weights quotas scale effective_intervals'),
    'flow': ('pending source_exhausted next_intent admitted_by_product _dirty_lots history_archive retired_facts', 'source manufacturing transfers scheduler custody lots emit entry_slots exit_slots _active_lots _sorted_active _lot_by_carrier _delivery_ports _delivery_router _delivery_costs _geometry_cache probes _history_lookup retirement_metrics actions'),
    'campaigns': ('records active_by_tool visit_owners sequence history_archive', 'manufacturing transfers delivery_cost emit policy managed_tools _dirty_tools _proposal_cache _switch_cache'),
    'handoffs': ('records waits history_archive', 'transfers emit _active_records _history_lookup actions'),
    'clearance': ('records waits sequence history_archive', 'manufacturing transfers emit protected_slot_ids _plain_docks_key _plain_docks_value _setup_groups _active_records _active_by_tool _active_by_visit _active_by_carrier_slot'),
    'maintenance': ('definitions causes history_archive initialized', 'manufacturing emit scheduler streams tools exchange_handlers pause_exchange _pending_by_tool _active_by_tool _bindings_by_tool _counter_bindings _counter_ceilings'),
    'amhs_faults': ('records', 'traffic scheduler'),
}
OBJECT_FIELDS = {
    'SetupState': (SetupState, 'current transitions minimum_runs completed_lots'),
    'Timer': (Timer, 'id remaining_us causes completed cancelled _kind _event _end _on_complete'),
    'SerializedHandler': (SerializedHandler, 'id requests active causes history_archive'),
    'Cascade': (Cascade, 'id mode current_setup units executions pending slots admission causes setup_active setup_target active_execution units_archive history_archive'),
    'DirtyResources': (DirtyResources, '_full _ids'),
}


def _roots(owners):
    return {**{name:getattr(owners,name) for name in SCHEMAS if name not in ('rules','batches','exchange')},
        'rules':owners.manufacturing.rules,'batches':owners.manufacturing.batches,'exchange':owners.transfers.exchange}


def runtime_digest(owners):
    from .probes import domain_digest, state_projection
    return domain_digest(state_projection({name:getattr(owners,name) for name in
        ('scheduler','streams','custody','manufacturing','transfers','traffic','flow','maintenance','amhs_faults')}))


def capture_runtime(owners: RuntimeOwners) -> dict[str, object]:
    scheduler = owners.scheduler.export_state()
    if owners.custody._notification_depth or owners.custody._changed:
        raise CheckpointError('custody transition is in progress')
    nodes=[]; references={}; native_objects=[]; action_refs=[]
    def encode(value):
        if isinstance(value, BoundAction):
            action_refs.append(value.ref)
            return ['action',export_action(value)]
        if isinstance(value,Fraction):return ['fraction',(value.numerator,value.denominator)]
        t=type(value)
        if t in (type(None),bool,int,float,str,bytes):return ['value',value]
        from decimal import Decimal
        if t is Decimal:return ['value',value]
        if id(value) in references:return ['ref',references[id(value)]]
        identity=len(nodes);references[id(value)]=identity;nodes.append(None)
        if t in (dict,OrderedDict,defaultdict):
            if t is defaultdict and value.default_factory is not int:raise CheckpointError('unsupported default factory')
            node=[t.__name__,[[encode(k),encode(v)] for k,v in value.items()]]
        elif t in (list,tuple,set,frozenset,deque):
            values=list(value)
            if t in (set,frozenset):
                from .checkpoint_format import encode_value
                values.sort(key=encode_value)
            node=[t.__name__,[encode(v) for v in values]]
            if t is deque:node.append(value.maxlen)
        elif t.__name__ in OBJECT_FIELDS and t is OBJECT_FIELDS[t.__name__][0]:
            native_objects.append(value)
            fields=OBJECT_FIELDS[t.__name__][1].split()
            allowed=set(fields)|{'scheduler','claims','dirty','emit','actions'}
            if isinstance(value,SerializedHandler):allowed.update(('_pending','_pending_order','_pending_signature','_history_lookup','_pending_changed'))
            if isinstance(value,Cascade):allowed.add('_history_lookup')
            if set(vars(value))-allowed:raise CheckpointError('unsupported native object fields')
            node=[t.__name__,{k:encode(getattr(value,k)) for k in fields}]
        else:raise CheckpointError('unsupported native owner: '+t.__name__)
        nodes[identity]=node
        return ['ref',identity]
    root_states={}
    for name,owner in _roots(owners).items():
        fields,ignored=SCHEMAS[name];fields=fields.split()
        if set(vars(owner))-set(fields)-set(ignored.split()):
            raise CheckpointError('unsupported '+name+' state fields: '+str(set(vars(owner))-set(fields)-set(ignored.split())))
        root_states[name]={k:encode(getattr(owner,k)) for k in fields}
    from .native_actions import validate_native_action
    for ref in action_refs:validate_native_action(owners,ref)
    _validate_scheduler_callbacks(owners,native_objects)
    _check_ownership(owners,native_objects)
    return {'version':1,'owners':root_states,'nodes':nodes,'scheduler':scheduler,
        'streams':owners.streams.export_state(),'physical_digest':runtime_digest(owners)}


def restore_runtime(owners: RuntimeOwners, state: dict[str, object], actions, *, verify_history=False) -> None:
    try:
        if set(state)!={'version','owners','nodes','scheduler','streams','physical_digest'} or state['version']!=1:
            raise ValueError('unsupported runtime state')
        roots=_roots(owners)
        if set(state['owners'])!=set(roots):raise ValueError('native owner inventory mismatch')
        nodes=state['nodes'];objects={};filling=set();filled=set();action_refs=[]
        def decode(value):
            if type(value) is not list or len(value)!=2:raise ValueError('invalid state reference')
            tag,data=value
            if tag=='value':return data
            if tag=='fraction':return Fraction(*data)
            if tag=='action':
                action=restore_action(data,actions);action_refs.append(action.ref)
                return action
            if tag!='ref' or type(data) is not int or not 0<=data<len(nodes):raise ValueError('invalid owner reference')
            if data in filled:return objects[data]
            node=nodes[data];kind=node[0];content=node[1]
            if data in filling:
                if data not in objects:raise ValueError('cyclic immutable reference')
                return objects[data]
            filling.add(data)
            if kind in ('dict','OrderedDict','defaultdict'):
                obj={} if kind=='dict' else OrderedDict() if kind=='OrderedDict' else defaultdict(int)
                objects[data]=obj
                for key,item in content:
                    key=decode(key)
                    if key in obj:raise ValueError('duplicate canonical key')
                    obj[key]=decode(item)
            elif kind in ('list','deque'):
                obj=[] if kind=='list' else deque(maxlen=node[2]);objects[data]=obj
                obj.extend(decode(v) for v in content)
            elif kind in ('tuple','set','frozenset'):
                obj={'tuple':tuple,'set':set,'frozenset':frozenset}[kind](decode(v) for v in content)
                objects[data]=obj
            elif kind in OBJECT_FIELDS:
                cls,fields=OBJECT_FIELDS[kind]
                if set(content)!=set(fields.split()):raise ValueError('native state field mismatch')
                obj=cls.__new__(cls);objects[data]=obj
                for k,v in content.items():setattr(obj,k,decode(v))
            else:raise ValueError('unsupported native object kind')
            filling.remove(data);filled.add(data)
            return obj
        for name,owner in roots.items():
            fields=SCHEMAS[name][0].split()
            if set(state['owners'][name])!=set(fields):raise ValueError('owner field mismatch')
            for key,value in state['owners'][name].items():setattr(owner,key,decode(value))
        if len(filled)!=len(nodes):raise ValueError('unreferenced native state objects')
        _rewire(owners,objects.values(),actions,state['scheduler'],verify_history=verify_history)
        owners.streams.restore_state(state['streams'])
        owners.scheduler.restore_state(state['scheduler'])
        from .native_actions import validate_native_action
        for ref in action_refs:validate_native_action(owners,ref)
        _validate_scheduler_callbacks(owners,objects.values())
        _check_ownership(owners,objects.values())
        if runtime_digest(owners)!=state['physical_digest']:raise ValueError('restored physical digest mismatch')
    except (ValueError,TypeError,KeyError,AttributeError,IndexError,RecursionError) as error:
        raise CheckpointError('invalid native runtime checkpoint: '+str(error)) from error


def _rewire(o,objects,actions,scheduler_state,*,verify_history=False):
    # History archives hold retired records only; simulation never reads them.
    # verify_history re-reads every chain (integrity, kinds, retired-LOT facts)
    # as an audit; a normal restore needs only the checkpoint.
    from .resource_index import ResourceIndex
    m,t,traffic,flow=o.manufacturing,o.transfers,o.traffic,o.flow
    m._wait_generation=getattr(m,'_wait_generation',0)+1
    m.rules.lots=flow.lots=m.lots;m.batches.tools=o.maintenance.tools=m.tools
    m.batches.setup_states=m.setups;traffic.vehicles=t.vehicles
    o.maintenance.exchange_handlers=t.exchange.handlers
    if verify_history and m.rules.clock_archive is not None:
        from .history_archive import HistoryArchive
        head=m.rules.clock_archive
        HistoryArchive(head['directory']).validate(head)
    if verify_history and o.maintenance.history_archive is not None:
        from .history_archive import HistoryArchive
        head=o.maintenance.history_archive
        HistoryArchive(head['directory']).validate(head)
    for owner,kind in ((o.clearance,'clearance-lease'),(o.campaigns,'setup-campaign')):
        if verify_history and owner.history_archive is not None:
            from .history_archive import HistoryArchive
            head=owner.history_archive
            for item_kind,_ in HistoryArchive(head['directory']).identities(head):
                if item_kind!=kind:raise ValueError('invalid archived owner kind')
    m.resource_index=t.resource_index=ResourceIndex(o.custody.slots,m.tools,t.model,protected_slot_ids=o.clearance.protected_slot_ids)
    # A caller may restore repeatedly into the same allocation. Retire only
    # callbacks owned by prior native timers; unknown handlers remain visible
    # to callback validation instead of being silently accepted.
    for kind,callback in tuple(o.scheduler._handlers.items()):
        timer=getattr(callback,'__self__',None)
        if isinstance(timer,Timer) and callback==timer._complete:
            o.scheduler.unregister(kind)
    previous_lookup=getattr(actions,'history_lookup',None)
    if previous_lookup is not None:previous_lookup.close()
    # Restore reads every archive chain anyway: index the identities owners
    # look up now, instead of re-reading each chain on its first lookup.
    from .history_lookup import HistoryLookup
    lookup=actions.history_lookup=HistoryLookup()
    def primed(head,kind,check):
        if not verify_history:return
        from .history_archive import HistoryArchive
        ids=[]
        # Identities only, read from checksum-verified segments without parsing records.
        for item_kind,identity in HistoryArchive(head['directory']).identities(head):
            check({'kind':item_kind,'id':identity});ids.append(identity)
        lookup.prime(head,kind,ids)
    flow._history_lookup=None;flow.actions=actions
    flow.retirement_metrics={'largest_record_bytes':0,'oversize_attempts':0}
    from .history_archive import wire_lot_history,validate_lot_history
    wire_lot_history(flow)
    if verify_history:validate_lot_history(flow)
    for owner,attribute,kind in ((m,'executions_archive','manufacturing-execution'),(m.batches,'history_archive','batch-assembly')):
        head=getattr(owner,attribute)
        if verify_history and head is not None:
            from .history_archive import HistoryArchive
            for item_kind,_ in HistoryArchive(head['directory']).identities(head):
                if item_kind!=kind:raise ValueError('invalid archived manufacturing ledger kind')
    for owner,attribute,kind in ((t,'history_archive','transfer-demand'),(t,'movements_archive','transfer-movement'),
            (t.exchange,'history_archive','equipment-exchange'),(o.handoffs,'history_archive','handoff-plan')):
        owner._history_lookup=None;head=getattr(owner,attribute)
        if head is not None:
            def check(item,kind=kind):
                if item['kind']!=kind:raise ValueError('invalid archived transfer ledger kind')
            primed(head,kind,check)
    traffic._history_lookup=None
    traffic._denied_hops={}
    traffic._claim_cache=None;traffic._claim_epoch+=1;traffic._idle_memo={}
    traffic._recovery_cycle_cache=None
    traffic._recovery_waypoint_rejections.clear()
    traffic._idle_requests={identity:record for identity,record in traffic.requests.items() if record['reposition']}
    if traffic.history_archive is not None:
        from .history_archive import HistoryArchive
        head=traffic.history_archive
        def check(item):
            if item['kind']!='traffic-request':raise ValueError('invalid archived traffic kind')
        primed(head,'traffic-request',check)
    actions.services.clear();actions.pipelines.clear()
    for obj in objects:
        if isinstance(obj,(Timer,SerializedHandler,Cascade)):obj.scheduler=o.scheduler
        if isinstance(obj,Timer):
            if not obj.completed and not obj.cancelled:o.scheduler.register(obj._kind,obj._complete)
        elif isinstance(obj,SerializedHandler):
            obj.actions=actions;obj.claims=m.claims;obj.emit=m.emit;actions.services[obj.id]=obj
            obj._history_lookup=None;obj._pending_changed=None
            if obj.history_archive is not None:
                from .history_archive import HistoryArchive
                head=obj.history_archive
                def check(item,kind='service:'+obj.id):
                    if item['kind']!=kind:raise ValueError('invalid archived service kind')
                primed(head,'service:'+obj.id,check)
            obj.rebuild_derived()
            if obj.id.startswith('manufacturing:'):
                tid=obj.id[len('manufacturing:'):];obj.dirty=lambda tid=tid:m.tool_changed(tid)
            elif obj.id.startswith('exchange:'):obj.dirty=t.exchange.dirty
            else:raise ValueError('unknown native service owner')
        elif isinstance(obj,Cascade):
            obj.actions=actions;obj.emit=m.emit;actions.pipelines[obj.id]=obj
            obj._history_lookup=None
            for attribute,kind in (('units_archive','cascade-unit:'+obj.id),('history_archive','cascade-execution:'+obj.id)):
                head=getattr(obj,attribute)
                if head is not None:
                    def check(item,kind=kind):
                        if item['kind']!=kind:raise ValueError('invalid archived cascade ledger kind')
                    primed(head,kind,check)
            tid=obj.id[len('pipeline:'):];obj.dirty=lambda tid=tid:m.tool_changed(tid)
    m.rules._active_visits={v['id']:v for v in m.rules.visits.values()
        if m.lots[v['lot_id']].get('current_visit_id')==v['id']}
    m.rules._live_order={vid:i for i,vid in enumerate(m.rules._active_visits)}
    m.rules._next_live_order=len(m.rules._live_order)
    m.rules._active_by_group={}
    for vid,v in m.rules._active_visits.items():m.rules._active_by_group.setdefault(m._step(v)['tool_group_id'],{})[vid]=v
    flow._active_lots={lid:lot for lid,lot in m.lots.items() if lot.get('exited_at_us') is None}
    flow._lot_by_carrier={lot['carrier_id']:lid for lid,lot in flow._active_lots.items()};flow._sorted_active=None
    o.handoffs._active_records={did:r for did,r in o.handoffs.records.items() if r['phase'] not in ('completed','cancelled')}
    t.exchange.rebuild_derived()
    o.clearance.rebuild_derived()
    o.campaigns.rebuild_derived()
    o.maintenance.rebuild_derived()
    # Caches carry no permissions or event timing. Force their next query to
    # derive a complete frontier from canonical records.
    t._handling_frontiers={};t._handling_waiters={};t._dirty_handling=DirtyResources()
    t._admission_retries={}
    t._resident_lots_by_tool={};t._incoming_lots_by_tool={};t._load_dirty=True;t._cost_router=None
    t._tool_loads={};t._incoming_by_tool=None;t._rack_slots_by_tool=None
    flow._delivery_ports=None;flow._delivery_router=None;flow._delivery_costs={}
    traffic._idle_rankings.clear();traffic._idle_plans.clear();traffic._idle_routers.clear()
    traffic._geometry_cache.clear();flow._geometry_cache=traffic._geometry_cache
    # Fault handler lifetimes are part of the scheduler identity.
    for kind in list(o.scheduler._handlers):
        if kind.startswith('amhs-fault:'):o.scheduler.unregister(kind)
    for rid,record in o.amhs_faults.records.items():
        start='amhs-fault:start:'+rid;recover='amhs-fault:recover:'+rid
        if start in scheduler_state['handlers']:
            o.scheduler.register(start,lambda payload,r=record:o.amhs_faults._start(r))
            o.scheduler_callbacks['handlers'][start]=o.scheduler._handlers[start]
        if recover in scheduler_state['handlers']:
            o.scheduler.register(recover,lambda payload,r=record:o.amhs_faults._recover(r))
            o.scheduler_callbacks['handlers'][recover]=o.scheduler._handlers[recover]


def _check_ownership(o,objects):
    c=o.custody
    actual={slot['carrier_id']:sid for sid,slot in c.slots.items() if slot['carrier_id'] is not None}
    if actual!=c.locations or len(actual)!=sum(s['carrier_id'] is not None for s in c.slots.values()):
        raise ValueError('custody carrier ownership mismatch')
    m=o.manufacturing
    if m._active_execution_count!=sum(e['status']!='unloaded' for e in m.executions.values()):
        raise ValueError('active execution ownership mismatch')

    for tid,tool in m.tools.items():
        if 'cascade_execution_ids' in tool and tool['cascade_execution_ids'] is not m.cascade_executions[tid]:
            raise ValueError('cascade execution ownership mismatch')
    events={event[2]:event for event in o.scheduler._heap}
    for obj in objects:
        if isinstance(obj,SerializedHandler):
            active=[r for r in obj.requests.values() if r['status']=='active']
            if obj.active is None:
                if active:raise ValueError('service active ownership mismatch')
            elif (len(active)!=1 or obj.active is not active[0]
                    or obj.requests.get(obj.active['id']) is not obj.active
                    or any(obj.claims.get(c)!=obj.active['owner_id'] for c in obj.active['carriers'])):
                raise ValueError('service active ownership mismatch')
        elif isinstance(obj,Timer):
            if obj._kind!='timer:'+obj.id:raise ValueError('timer handler identity mismatch')
            if obj.completed or obj.cancelled:continue
            if obj.causes:
                if obj._event is not None or obj._end is not None:raise ValueError('paused timer owns a deadline')
            else:
                event=events.get(obj._event)
                if (type(obj._event) is not int or obj._event in o.scheduler._cancelled or event is None
                        or event[0]!=obj._end or event[1]!=0 or event[3]!=obj._kind or event[4]!={}):
                    raise ValueError('active timer deadline ownership mismatch')


def _validate_scheduler_callbacks(o,objects):
    """Only callbacks allocated by the native composition may survive a save."""
    s=o.scheduler;known=o.scheduler_callbacks
    if known is None:raise CheckpointError('native scheduler callback inventory missing')
    expected={k:v for k,v in known['handlers'].items() if not k.startswith('amhs-fault:')}
    for rid,record in o.amhs_faults.records.items():
        for phase,statuses in (('start',('scheduled',)),('recover',('scheduled','active'))):
            kind='amhs-fault:'+phase+':'+rid
            if record['status'] in statuses:expected[kind]=known['handlers'][kind]
    for obj in objects:
        if isinstance(obj,Timer) and not obj.completed and not obj.cancelled:
            if obj._kind in expected:raise CheckpointError('duplicate native timer handler')
            expected[obj._kind]=obj._complete
    if (s._handlers!=expected or s._passes!=known['passes']
            or s._deadline_checks!=known['deadline_checks']):
        raise CheckpointError('unsupported scheduler callback registration')
