"""Observation counters measure CPU and wall time separately and never draw from domain streams."""
from collections import defaultdict,deque
from contextlib import contextmanager
import hashlib
from time import perf_counter_ns, process_time_ns
from .json_stream import json_bytes


def domain_digest(state: dict) -> str:
    """Hash an explicit domain-only projection, including RNG states if desired.

    Callers exclude run namespaces, output/probe state and callbacks. No live
    objects are introspected, mutated or sampled to build this digest.
    """
    digest=hashlib.sha256()
    for part in _digest_parts(state,2):digest.update(part)
    return digest.hexdigest()


def _digest_parts(value,depth):
    # The json_chunks byte stream: the top levels are streamed to bound memory
    # and everything below is encoded in one pass. Hashing is chunk-agnostic.
    if depth and type(value) is dict and all(type(k) is str for k in value):
        yield b'{'
        for i,key in enumerate(sorted(value)):
            yield (b',' if i else b'')+json_bytes(key)+b':'
            yield from _digest_parts(value[key],depth-1)
        yield b'}'
    elif depth and type(value) in (list,tuple):
        yield b'['
        for i,item in enumerate(value):
            if i:yield b','
            yield from _digest_parts(item,depth-1)
        yield b']'
    else:yield json_bytes(value)


class Probes:
    def __init__(self,*,enabled=True,sample_every=1,recent_limit=32,clock=perf_counter_ns,cpu_clock=process_time_ns):
        if (type(enabled) is not bool or any(type(n) is not int or n<=0 for n in (sample_every,recent_limit)) or
                not callable(clock) or not callable(cpu_clock)):raise ValueError('invalid probe policy')
        self.enabled=enabled;self.sample_every=sample_every;self.clock=clock;self.sequence=0;self.unavailable=0
        self.counts=defaultdict(int);self.wall_ns=defaultdict(int);self.traces=deque(maxlen=recent_limit)
        self.work_counts=defaultdict(int)
        self.cpu_clock=cpu_clock;self.cpu_ns=defaultdict(int);self.cpu_unavailable=0

    def count_work(self,kind,amount=1):
        if not isinstance(kind,str) or not kind or type(amount) is not int or amount<=0:
            raise ValueError('work counter requires a stable kind and positive integer amount')
        if self.enabled:self.work_counts[kind]+=amount

    def _time(self,clock=None):
        try:
            value=(self.clock if clock is None else clock)()
            return value if type(value) is int and value>=0 else None
        except Exception:return None

    @contextmanager
    def handler(self,kind):
        if not isinstance(kind,str) or not kind:raise ValueError('probe requires a stable handler family')
        if not self.enabled:yield;return
        self.sequence+=1;sequence=self.sequence;self.counts[kind]+=1;start=self._time();cpu_start=self._time(self.cpu_clock)
        try:yield
        finally:
            end=self._time();elapsed=end-start if start is not None and end is not None and end>=start else None
            cpu_end=self._time(self.cpu_clock)
            cpu_elapsed=cpu_end-cpu_start if cpu_start is not None and cpu_end is not None and cpu_end>=cpu_start else None
            if cpu_elapsed is None:self.cpu_unavailable+=1
            else:self.cpu_ns[kind]+=cpu_elapsed
            if elapsed is None:self.unavailable+=1
            else:self.wall_ns[kind]+=elapsed
            if sequence%self.sample_every==0:self.traces.append({'handler_sequence':sequence,'kind':kind[:200],'wall_ns':elapsed,'cpu_ns':cpu_elapsed})


def state_projection(state):
    """Capture physical owners without callback identity or observation clocks.

    Cross-owner object links are represented by separately named roots. Timers,
    claims, setup credits, route clocks and future event payloads remain visible.
    This does not inspect closures or restore a checkpoint.
    """
    from decimal import Decimal
    from fractions import Fraction
    from random import Random
    from collections import deque
    from .scheduler import Scheduler
    from .manufacturing import RouteRules,Manufacturing
    from .manufacturing import BatchAssembly
    from .service import SerializedHandler,Cascade
    from .clearance import ClearancePool
    from .handoffs import HandoffPlans
    from .custody import Custody
    from .flow import Flow
    from .rng import Streams
    from .transfers import Transfers,EquipmentExchange
    from .traffic import Traffic
    from .maintenance import Maintenance
    from .campaigns import SetupCampaigns
    lot_digests={}
    links={'_lot_history_owner','actions','scheduler','streams','custody','manufacturing','transfers','traffic','emit','on_change'}
    def project(value):
        if value is None or isinstance(value,(str,int,float,bool,Decimal,Fraction)):return value
        if isinstance(value,Random):return {'rng_state':value.getstate()}
        if isinstance(value,dict):
            if all(isinstance(key,str) for key in value):
                return {key:project(item) for key,item in value.items() if not callable(item)}
            entries=[{'key':project(key),'value':project(item)} for key,item in value.items() if not callable(item)]
            return sorted(entries,key=lambda entry:json_bytes(entry['key']))
        if isinstance(value,(list,tuple,deque)):return [project(item) for item in value if not callable(item)]
        if isinstance(value,(set,frozenset)):
            return sorted((project(item) for item in value),key=json_bytes)
        if type(value).__module__.startswith('fab_sim.') and hasattr(value,'__dict__'):
            fields={}
            for key,item in vars(value).items():
                if key in links or callable(item):continue

                if isinstance(value,Flow) and key in ('history_archive','retired_facts','_history_lookup','retirement_metrics'):continue
                lot_owner=value if isinstance(value,Flow) and getattr(value,'actions',None) else getattr(value,'_lot_history_owner',None)
                if lot_owner is not None and key=='lots':
                    from .history_archive import lot_ledger_digest
                    if id(lot_owner) not in lot_digests:lot_digests[id(lot_owner)]=lot_ledger_digest(lot_owner)
                    item=lot_digests[id(lot_owner)]
                if lot_owner is not None and isinstance(value,RouteRules) and key=='visits':
                    item={'representation':'included-in-lot-history-v1'}
                # The salt is a declared setting (in the physical identity); its effect is in the streams.
                if isinstance(value,Streams) and key=='replication_salt':continue
                if isinstance(value,Streams) and lot_owner is not None and key=='_streams':
                    item={k:v for k,v in item.items() if not(k[0]=='processing' and k[1] in lot_owner.lots)}
                if isinstance(value,(ClearancePool,SetupCampaigns)) and key=='history_archive':continue
                if isinstance(value,ClearancePool) and key in ('_plain_docks_key','_plain_docks_value','_setup_groups'):continue
                if isinstance(value,(ClearancePool,SetupCampaigns)) and key=='records':
                    from .history_archive import record_ledger_digest
                    item=record_ledger_digest(value,'clearance-lease' if isinstance(value,ClearancePool) else 'setup-campaign')
                if isinstance(value,RouteRules) and key in ('clock_archive','retired_violation_ids'):continue
                if isinstance(value,Maintenance) and key=='history_archive':continue
                if isinstance(value,Maintenance) and key=='causes':
                    from .history_archive import maintenance_ledger_digest
                    item=maintenance_ledger_digest(value)
                if isinstance(value,RouteRules) and key=='clocks':
                    from .history_archive import clock_ledger_digest
                    item=clock_ledger_digest(value)
                if isinstance(value,Custody) and key in ('_resource_listeners','_resource_pending_slots','_resource_pending_carriers','version'):continue
                if isinstance(value,RouteRules) and key in ('_possible_next','_predecessors','_batch_reaches','_active_visits','_active_by_group','_live_order','_next_live_order'):continue
                if isinstance(value,Manufacturing) and key=='_active_execution_count':continue
                if isinstance(value,Manufacturing) and key in ('_resource_listeners','_wait_generation','resource_index','probes','_dirty_groups','processed_changed','super_hot_holds','extra_processing'):continue
                if isinstance(value,Flow) and key in ('_active_lots','_sorted_active','_lot_by_carrier','_dirty_lots','probes','admitted_by_product','_delivery_ports','_delivery_router','_delivery_costs','_geometry_cache'):continue
                if isinstance(value,Transfers) and key in ('resource_index','probes','_dirty_handling','_handling_frontiers','_handling_waiters','_admission_retries','_event_sink','_load_dirty','_resident_lots_by_tool','_incoming_lots_by_tool','_tool_loads','_incoming_by_tool','_rack_slots_by_tool','_cost_router','_tool_docks','_tool_docks_key','_destination_layout','_sorted_racks','_vehicle_slots','_vehicle_starved','buffer_selection','_buffer_tiers','_super_hot_lots','congestion_routing_s','dispatch_metering','_metered','_meter_waiting','_open_version','_open_memo','_meter_counts','_neg','busy_vehicle_cap','_cap_engaged','super_hot_expedite','congestion_destination_s','_dock_zones','down_tool_avoid_us','long_downtime'):continue
                if isinstance(value,EquipmentExchange) and key in ('_pending_tools','_dispatch_frontier','down_tool_output'):continue
                if isinstance(value,BatchAssembly) and key=='super_hot_expedite':continue
                if isinstance(value,(Transfers,EquipmentExchange,HandoffPlans)) and key in ('history_archive','movements_archive','_history_lookup'):continue
                ledger=None
                if isinstance(value,Transfers) and key=='overflow_visits':
                    owner=getattr(value.manufacturing,'_lot_history_owner',None)
                    if owner is not None:
                        carriers={lot['carrier_id'] for lot in owner.lots.values()}
                        item={pair for pair in item if pair[0] not in carriers}
                if isinstance(value,Transfers) and key=='demands':ledger=('history_archive','transfer-demand')
                elif isinstance(value,Transfers) and key=='movements':ledger=('movements_archive','transfer-movement')
                elif isinstance(value,EquipmentExchange) and key=='requests':ledger=('history_archive','equipment-exchange')
                elif isinstance(value,HandoffPlans) and key=='records':ledger=('history_archive','handoff-plan')
                if ledger and (getattr(value,'actions',None) is not None or getattr(value,ledger[0]) is not None):
                    from .history_archive import native_ledger_digest
                    item=native_ledger_digest(item,getattr(value,ledger[0]),ledger[1])
                if isinstance(value,(Manufacturing,BatchAssembly,Cascade)) and key in ('executions_archive','history_archive','units_archive','_history_lookup'):continue
                ledger=None
                if isinstance(value,Manufacturing) and key=='executions':ledger=('executions_archive','manufacturing-execution')
                elif isinstance(value,BatchAssembly) and key=='assemblies':ledger=('history_archive','batch-assembly')
                elif isinstance(value,Cascade) and key=='units':ledger=('units_archive','cascade-unit:'+value.id)
                elif isinstance(value,Cascade) and key=='executions':ledger=('history_archive','cascade-execution:'+value.id)
                if ledger and (isinstance(value,BatchAssembly) or getattr(value,'actions',None) is not None or getattr(value,ledger[0]) is not None):
                    from .history_archive import native_ledger_digest
                    item=native_ledger_digest(item,getattr(value,ledger[0]),ledger[1])
                if isinstance(value,Traffic) and key in ('probes','_dock_offsets_by_rail','_denied_hops','_recovery_signature','_recovery_cycle_cache','_recovery_waypoint_rejections','_idle_rankings','_idle_plans','_idle_routers','_geometry_cache','_idle_target_key','_history_lookup','_idle_requests','_passage_index','_claims_version','_claim_cache','_service_set','_rail_versions','_claim_epoch','_idle_memo','hop_lookahead_um','bay_entry_divert','queue_divert','_queue_divert_graph','_rail_bay_kind','history_archive'):continue
                if isinstance(value,HandoffPlans) and key=='_active_records':continue
                if isinstance(value,BatchAssembly) and key in ('_group_tools','_group_tools_key'):continue
                if isinstance(value,Maintenance) and key in ('pause_exchange','_pending_by_tool','_active_by_tool','_bindings_by_tool','_counter_bindings','_counter_ceilings'):continue
                if (isinstance(value,Traffic) and key=='requests'
                        and (getattr(value,'actions',None) is not None or value.history_archive is not None)):
                    from .history_archive import traffic_ledger_digest
                    item=traffic_ledger_digest(value)
                if isinstance(value,SerializedHandler) and key in ('_pending','_pending_order','_pending_signature','_history_lookup','history_archive','_pending_changed'):continue
                if (isinstance(value,SerializedHandler) and key=='requests'
                        and (getattr(value,'actions',None) is not None or value.history_archive is not None)):
                    from .history_archive import service_ledger_digest
                    item=service_ledger_digest(value)
                if isinstance(value,SetupCampaigns) and key in ('policy','_dirty_tools','_proposal_cache','_switch_cache'):continue
                if isinstance(value,ClearancePool) and key in ('_active_records','_active_by_tool','_active_by_visit','_active_by_carrier_slot'):continue
                if isinstance(value,Scheduler) and key in ('handled_events','pass_calls','recent','probes'):continue
                if isinstance(value,Scheduler) and key in ('_handlers','_passes'):
                    fields[key]=sorted(item);continue
                fields[key]=project(item)
            return {'owner_type':type(value).__name__,'state':fields}
        raise ValueError('unsupported domain projection value: '+type(value).__name__)
    return project(state)
