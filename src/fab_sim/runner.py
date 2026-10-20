"""Load a run configuration, prepare the fab, and run the simulation (fresh or from a checkpoint)."""
from copy import deepcopy
from pathlib import Path
import re
import traceback
import time
from .inputs import read_json,validate_keys
from .sources.smt import validate_manufacturing
from .generation import load_prepared,generate_topology
from .scheduler import Scheduler
from .rng import Streams
from .custody import Custody
from .manufacturing import Manufacturing,BATCH_POLICY
from .transfers import Transfers,ASSIGNMENT_POLICY
from .traffic import Traffic
from .maintenance import Maintenance
from .amhs_faults import AMHSFaults
from .workload import ReleaseSource
from .flow import Flow
from .output import Output,OutputFailure
from .publisher import Pacer,Publisher
from .probes import Probes,domain_digest,state_projection
from .outcomes import classify_stop
from .streams import StreamProjection,POLICY as STREAMS_POLICY


# Periodic history retirement publishes only full archive segments; every this many
# settlements (and at every checkpoint and stop) it publishes everything eligible.
RETIRE_FLUSH_SETTLEMENTS=200_000


def _integer(value,name,minimum=0):
    if type(value) is not int or value<minimum:raise ValueError('invalid '+name)
    return value


def _path(base,value,name):
    if not isinstance(value,str) or not value:raise ValueError('missing '+name)
    return (base/value).resolve()


from .geometry import spatial_um


def load_config(path, *, checkpoint_interval_wall_seconds=None, resume_from=None):
    path=Path(path).resolve();c=read_json(path)
    validate_keys(c,{'inputs','generation','workload','execution','replay','output','probes','injected_faults','checkpoint'},'run')
    defaults={'inputs':{},'generation':{},'workload':{},'execution':{},'replay':{},'output':{},'probes':{}}
    for key,value in defaults.items():c.setdefault(key,value)
    allowed={'inputs':{'manufacturing_path','topology_path','physical_reference_path'},
        'execution':{'horizon_us','master_seed','transition_budget','max_settlements','setup_minimum_runs','setup_policy','exit_targets','derived_cache_budget_mib','hop_lookahead_mm','bay_entry_divert','queue_divert','buffer_selection','super_hot_reservation','extra_processing','down_tool_output','congestion_routing_s','congestion_destination_s','dispatch_metering','busy_vehicle_cap','super_hot_expedite','down_tool_avoid_s','replication_salt'},
        'replay':{'rate'},'output':{'directory','topology_directory','run_id','chunk_records','chunk_bytes','max_record_bytes','live','publication'},
        'probes':{'enabled','sample_every','recent_limit'}}
    for key,keys in allowed.items():validate_keys(c[key],keys,key)
    c['execution'].setdefault('master_seed',7);c['execution'].setdefault('transition_budget',100000)
    c['execution'].setdefault('derived_cache_budget_mib',512)
    _integer(c['execution']['derived_cache_budget_mib'],'derived_cache_budget_mib',1)
    if 'exit_targets' in c['execution']:
        from .outcomes import validate_exit_targets
        c['execution']['exit_targets']=validate_exit_targets(c['execution']['exit_targets'])
    _integer(c['execution'].get('horizon_us'),'horizon_us',1)
    _integer(c['execution']['master_seed'],'master_seed');_integer(c['execution']['transition_budget'],'transition_budget',1)
    if 'max_settlements' in c['execution']:_integer(c['execution']['max_settlements'],'max_settlements')
    if 'hop_lookahead_mm' in c['execution']:
        value=c['execution']['hop_lookahead_mm']
        if isinstance(value,bool) or not isinstance(value,(int,str)) or spatial_um(value)<0:
            raise ValueError('hop_lookahead_mm must be a non-negative distance')
    if c['execution'].get('buffer_selection','nearest_origin') not in ('nearest_origin','next_group_bays'):
        raise ValueError('buffer_selection must be nearest_origin or next_group_bays')
    if 'bay_entry_divert' in c['execution'] and type(c['execution']['bay_entry_divert']) is not bool:
        raise ValueError('bay_entry_divert must be boolean')
    if 'queue_divert' in c['execution'] and type(c['execution']['queue_divert']) is not bool:
        raise ValueError('queue_divert must be boolean')
    if 'super_hot_reservation' in c['execution'] and type(c['execution']['super_hot_reservation']) is not bool:
        raise ValueError('super_hot_reservation must be boolean')
    if 'super_hot_expedite' in c['execution'] and type(c['execution']['super_hot_expedite']) is not bool:
        raise ValueError('super_hot_expedite must be boolean')
    if 'busy_vehicle_cap' in c['execution'] and c['execution']['busy_vehicle_cap'] is not None:
        if type(c['execution']['busy_vehicle_cap']) is not int or c['execution']['busy_vehicle_cap']<1:
            raise ValueError('busy_vehicle_cap must be a positive integer or null')
    if 'replication_salt' in c['execution'] and c['execution']['replication_salt'] is not None:
        if type(c['execution']['replication_salt']) is not int or c['execution']['replication_salt']<1:
            raise ValueError('replication_salt must be a positive integer or null')
    if 'down_tool_avoid_s' in c['execution'] and c['execution']['down_tool_avoid_s'] is not None:
        if type(c['execution']['down_tool_avoid_s']) is not int or c['execution']['down_tool_avoid_s']<1:
            raise ValueError('down_tool_avoid_s must be a positive integer or null')
    if 'dispatch_metering' in c['execution'] and c['execution']['dispatch_metering'] is not None:
        if type(c['execution']['dispatch_metering']) is not int or c['execution']['dispatch_metering']<1:
            raise ValueError('dispatch_metering must be a positive integer or null')
    if 'congestion_destination_s' in c['execution']:
        value=c['execution']['congestion_destination_s']
        if type(value) is not int or value<0:raise ValueError('congestion_destination_s must be a non-negative integer')
    if 'congestion_routing_s' in c['execution']:
        value=c['execution']['congestion_routing_s']
        if type(value) is not int or value<0:raise ValueError('congestion_routing_s must be a non-negative integer')
    if 'down_tool_output' in c['execution'] and type(c['execution']['down_tool_output']) is not bool:
        raise ValueError('down_tool_output must be boolean')
    if 'extra_processing' in c['execution']:
        from .perturbations import validate
        validate(c['execution']['extra_processing'])
    if 'setup_policy' in c['execution']:
        from .campaigns import SETUP_POLICIES
        if c['execution']['setup_policy'] not in SETUP_POLICIES:
            raise ValueError('setup_policy must be one of '+', '.join(SETUP_POLICIES))
    overrides=c['execution'].get('setup_minimum_runs',{})
    if not isinstance(overrides,dict) or any(not isinstance(k,str) or not k for k in overrides):
        raise ValueError('setup_minimum_runs must map setup names to positive integers')
    for value in overrides.values():_integer(value,'setup_minimum_runs',1)
    c['replay'].setdefault('rate',None)
    for key,value in {'run_id':'run','chunk_records':64,'chunk_bytes':65536,'max_record_bytes':65536,'live':False}.items():c['output'].setdefault(key,value)
    if type(c['output']['live']) is not bool:raise ValueError('live must be boolean')
    c['output'].setdefault('publication',{})
    validate_keys(c['output']['publication'],{'max_records','max_bytes','batch_records','batch_bytes','max_record_bytes',
        'flush_interval_s','initial_delay_s','cap_delay_s','max_attempts','attempt_timeout_s','retry_budget_s','drain_timeout_s'},'publication')
    if not c['output']['live'] and c['output']['publication']:raise ValueError('publication limits require live output')
    for key,value in {'enabled':True,'sample_every':1,'recent_limit':32}.items():c['probes'].setdefault(key,value)
    c.setdefault('injected_faults',[])
    if not isinstance(c['injected_faults'],list):raise ValueError('injected_faults must be a list')
    ids=[]
    for f in c['injected_faults']:
        if not isinstance(f,dict) or not isinstance(f.get('id'),str):raise ValueError('invalid injected fault')
        ids.append(f['id'])
    if len(ids)!=len(set(ids)):raise ValueError('duplicate injected fault ID')
    c['inputs']['manufacturing_path']=str(_path(path.parent,c['inputs'].get('manufacturing_path'),'manufacturing_path'))
    for key in ('topology_path','physical_reference_path'):
        if key in c['inputs'] and c['inputs'][key] is not None:c['inputs'][key]=str(_path(path.parent,c['inputs'][key],key))
    c['output']['directory']=str(_path(path.parent,c['output'].get('directory'),'output directory'))
    from .topology_store import default_directory
    c['output']['topology_directory']=str(_path(path.parent,
        c['output'].get('topology_directory',str(default_directory(path.parent))),'topology directory'))
    checkpoint=c.setdefault('checkpoint',{})
    validate_keys(checkpoint,{'enabled','interval_wall_seconds','directory','resume_from','allow_compatible_build','allow_policy_change','retain_completed','retain_daily','background','verify_history'},'checkpoint')
    if checkpoint_interval_wall_seconds is not None:
        checkpoint.update(enabled=True,interval_wall_seconds=checkpoint_interval_wall_seconds)
    if resume_from is not None:checkpoint['resume_from']=str(resume_from)
    checkpoint.setdefault('enabled',False);checkpoint.setdefault('interval_wall_seconds',300)
    checkpoint.setdefault('retain_completed',3)
    _integer(checkpoint['retain_completed'],'checkpoint retain_completed',1)
    checkpoint.setdefault('resume_from',None);checkpoint.setdefault('allow_compatible_build',False)
    checkpoint.setdefault('background',False);checkpoint.setdefault('retain_daily',False)
    checkpoint.setdefault('allow_policy_change',False);checkpoint.setdefault('verify_history',False)
    _integer(checkpoint['interval_wall_seconds'],'checkpoint interval_wall_seconds',1)
    for flag in ('enabled','allow_compatible_build','allow_policy_change','background','retain_daily','verify_history'):
        if type(checkpoint[flag]) is not bool:raise ValueError('checkpoint '+flag+' must be boolean')
    checkpoint['directory']=str(_path(path.parent,checkpoint['directory'],'checkpoint directory')) if 'directory' in checkpoint else str(Path(c['output']['directory'])/'checkpoints')
    if checkpoint['resume_from'] is not None:
        checkpoint['resume_from']=str(_path(path.parent,checkpoint['resume_from'],'checkpoint resume_from'))
    if c['output']['live'] and (checkpoint['enabled'] or checkpoint['resume_from'] is not None):
        raise ValueError('checkpoint/resume with live publication is unsupported')
    return c


def allocate(model):
    """Allocate finite live custody; prepared artifacts remain immutable."""
    tools={};slots=[];entry=[];exit=[]
    for e in model['equipment']:
        if e['kind']=='ordinary':
            racks=[e['id']+':rack:'+str(i) for i in range(_integer(e['rack_capacity'],'rack capacity',1))]
            tools[e['id']]={**deepcopy(e),'rack_slots':racks}
            slots.extend({'id':sid,'kind':'rack','equipment_id':e['id']} for sid in racks)
        elif e['kind'] in ('entry','exit','stocker'):
            docks=sorted((d for d in model['docks'] if d.get('equipment_id')==e['id']),key=lambda d:d['id'])
            if not docks or e['kind']!='stocker' and len(docks)!=1:
                raise ValueError('entry/exit requires one explicit aggregate service dock; stocker requires service docks')
            if len(docks)>1 and any(d.get('role','BOTH')!='BOTH' for d in docks):
                raise ValueError('multiport stocker requires bidirectional service docks')
            internal=[e['id']+':storage:'+str(i) for i in range(_integer(e['rack_capacity'],'stocker capacity',1))]
            slots.extend({'id':sid,'kind':e['kind'] if e['kind'] in ('entry','exit') else 'storage',
                          'equipment_id':e['id'],'dock_id':docks[k%len(docks)]['id']} for k,sid in enumerate(internal))
            if e['kind']=='entry':entry.extend(internal)
            if e['kind']=='exit':exit.extend(internal)
        else:raise ValueError('unsupported physical equipment kind')
    for d in model['docks']:
        slots.append({'id':d['id'],'kind':'port' if d['kind']=='ordinary' else d['kind'],
                      'equipment_id':d.get('equipment_id'),'role':d.get('role','BOTH')})
    vehicles={v['id']:{**deepcopy(v),'carrier_slot_id':'vehicle:'+v['id']} for v in model['vehicles']}
    slots.extend({'id':v['carrier_slot_id'],'kind':'vehicle'} for v in vehicles.values())
    return tools,Custody(slots),vehicles,tuple(entry),tuple(exit)


_TOOL_IN_ID=re.compile(r'^(?:[a-z]+:)?([^:]+:tool:\d+)(?::|$)')


def event_references(kind,payload):
    mapping={'lot_id':'lot_ids','lot_ids':'lot_ids','visit_id':'visit_ids','visit_ids':'visit_ids',
        'current_visit_id':'visit_ids','tool_id':'equipment_ids','equipment_id':'equipment_ids',
        'carrier_id':'carrier_ids','carrier_ids':'carrier_ids','vehicle_id':'oht_ids',
        'demand_id':'transfer_ids','assembly_id':'batch_ids','execution_id':'execution_ids',
        'request_id':'movement_ids','movement_id':'movement_ids','transfer_id':'transfer_ids',
        'carriers':'carrier_ids','source_step_id':'step_ids'}
    # Slot ids (racks, ports) and service owners name their tool: TOOL:tool:NNN:...
    tool_slots=('origin_id','destination_id','rack_slot_id','source_rack_id','output_port_id','port_id','slot_id','owner_id')
    found={}
    def add(group,value):
        if isinstance(value,str) and value:found.setdefault(group,set()).add(value)
        elif isinstance(value,(list,tuple)):
            for item in value:add(group,item)
    def inspect(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key in mapping:add(mapping[key],item)
                elif isinstance(item,(dict,list,tuple)):inspect(item)
        elif isinstance(value,(list,tuple)):
            for item in value:inspect(item)
    inspect(payload)
    for key in tool_slots:
        value=payload.get(key)
        if isinstance(value,str):
            match=_TOOL_IN_ID.match(value)
            if match:add('equipment_ids',match.group(1))
    # A carrier belongs to one LOT for its whole life (carrier id = LOT id + ':carrier').
    for carrier in found.get('carrier_ids',()):
        if carrier.endswith(':carrier'):add('lot_ids',carrier[:-len(':carrier')])
    if 'source_step_id' in payload and 'lot_id' in payload:add('visit_ids',payload.get('id'))
    for prefix,group in (('transfer_movement_','movement_ids'),('transfer_','transfer_ids'),
            ('batch_','batch_ids'),('carrier_exchange_','exchange_ids'),('traffic_','movement_ids'),
            ('execution_','execution_ids')):
        if kind.startswith(prefix):
            add(group,payload.get('id'));break
    return {group:sorted(ids) for group,ids in found.items()}




def build_runtime(config: dict, emit, *, initialize: bool, prepared=None):
    from .runtime_state import RuntimeOwners
    from .actions import ActionRegistry
    from .native_actions import bind_native_actions
    if prepared is None:
        from .preflight import prepare, assess, setup_scenario
        manufacturing,topology,topology_record=prepare(config,with_storage=True)
        findings=assess(config,manufacturing,topology)
        manufacturing,_=setup_scenario(config,manufacturing)
        if findings['status']=='rejected':
            raise ValueError('preflight rejected: '+', '.join(f['code'] for f in findings['findings'] if f['severity']=='error'))
    else:
        manufacturing,topology,topology_record,findings=prepared
    settings=config['execution'];s=Scheduler(settings['transition_budget']);streams=Streams(settings['master_seed'],settings.get('replication_salt'))
    tools,custody,vehicles,entry,exit=allocate(topology['model']);lots={}
    m=Manufacturing(manufacturing,s,streams,lots,custody,tools,emit)
    from .derived_cache import DerivedCache
    geometry_cache=DerivedCache(config['execution'].get('derived_cache_budget_mib',512)*1024*1024)
    t=Transfers(m,topology,vehicles,emit,buffer_selection=settings.get('buffer_selection','nearest_origin'));traffic=Traffic(topology,s,vehicles,emit,transfers=t,geometry_cache=geometry_cache,
        hop_lookahead_um=spatial_um(settings.get('hop_lookahead_mm',0)),
        bay_entry_divert=settings.get('bay_entry_divert',False),
        queue_divert=settings.get('queue_divert',False))
    if settings.get('super_hot_reservation',False):m.super_hot_holds=t.super_hot_destinations
    if settings.get('congestion_routing_s'):t.congestion_routing_s=settings['congestion_routing_s']
    if settings.get('congestion_destination_s'):t.congestion_destination_s=settings['congestion_destination_s']
    if settings.get('dispatch_metering'):t.dispatch_metering=settings['dispatch_metering'];t._metered=True
    if settings.get('busy_vehicle_cap'):t.busy_vehicle_cap=settings['busy_vehicle_cap']
    if settings.get('super_hot_expedite'):t.super_hot_expedite=True;m.batches.super_hot_expedite=True
    if settings.get('extra_processing'):
        from .perturbations import ExtraProcessing
        m.extra_processing=ExtraProcessing(settings['extra_processing'],m.rules.routes,m.groups,streams,emit)
    from .resource_index import ResourceIndex
    m.resource_index=t.resource_index=ResourceIndex(custody.slots,tools,t.model,protected_slot_ids=findings['clearance_pool']['protected_slot_ids'])
    source=ReleaseSource(manufacturing,config['workload'])
    from .campaigns import SetupCampaigns,POLICY
    from .clearance import ClearancePool
    from .handoffs import HandoffPlans,POLICY as TRANSPORT_POLICY
    campaigns=SetupCampaigns(m,t,lambda carrier,tid:flow.delivery_cost(carrier,tid),emit,
        policy=config['execution'].get('setup_policy','staged_campaign'))
    clearance=ClearancePool(m,t,findings['clearance_pool']['protected_slot_ids'],emit)
    handoffs=HandoffPlans(t,clearance,emit)
    flow=Flow(source,m,t,entry,exit,emit,initialize=initialize,geometry_cache=geometry_cache)
    maintenance=Maintenance(m,emit,exchange_handlers=t.exchange.handlers)
    if settings.get('down_tool_avoid_s'):
        t.down_tool_avoid_us=settings['down_tool_avoid_s']*1_000_000;t.long_downtime=maintenance.long_downtime
    if settings.get('down_tool_output',False):t.exchange.down_tool_output=True;maintenance.pause_exchange=False
    faults=config['injected_faults']
    maintenance.initialize(topology,manufacturing,injected_faults=[f for f in faults if f.get('target_type')=='equipment'],initialize=initialize)
    amhs_faults=AMHSFaults(traffic,[f for f in faults if f.get('target_type')!='equipment'],initialize=initialize)
    owners=RuntimeOwners(s,streams,custody,m,t,traffic,source,flow,campaigns,handoffs,clearance,maintenance,amhs_faults,
        ActionRegistry(),manufacturing,topology,topology_record,findings,entry,exit)
    bind_native_actions(owners,owners.actions)
    from .history_archive import wire_lot_history
    wire_lot_history(flow)
    owners.scheduler_callbacks={'handlers':dict(s._handlers),'passes':dict(s._passes),
        'deadline_checks':list(s._deadline_checks)}
    if not initialize:s.dirty.clear()
    return owners


def assessed_obligations(clocks,retired=()):
    """Fresh ordered obligation lists from one canonical clock pass.

    retired holds [event_id, clock_id] of archived violated clocks in event_id
    order; live clocks are in creation (event_id) order too, so the merge keeps
    the order of an unarchived ledger."""
    violated=[];unresolved=[]
    for cid,clock in clocks.items():
        if clock['violated']:violated.append(cid)
        elif clock['closed_at_us'] is None:unresolved.append(cid)
    if retired:
        from heapq import merge
        live=[(clocks[cid]['event_id'],cid) for cid in violated]
        violated=[cid for _,cid in merge(((e,c) for e,c in retired),live)]
    return violated,unresolved


def run(config_path:Path,*,publication_adapter=None,clock=time.monotonic,sleep=time.sleep,
        progress=None,progress_interval=15,observation_clock=time.monotonic,
        checkpoint_interval_wall_seconds=None,resume_from=None)->dict:
    from .checkpoint import CheckpointSettings,CheckpointCoordinator,physical_identity,validate_resume,build_identity,runtime_compatibility,STATE_CONTRACT,upgrade_state
    from .checkpoint_format import CheckpointManifest,CheckpointBundle,CheckpointError
    from .checkpoint_store import CheckpointStore,CheckpointWriteError
    from .runtime_state import capture_runtime,restore_runtime
    config=load_config(config_path,checkpoint_interval_wall_seconds=checkpoint_interval_wall_seconds,resume_from=resume_from)
    cp=config['checkpoint']
    checkpoint_settings=CheckpointSettings(cp['enabled'],cp['interval_wall_seconds'],Path(cp['directory']),
        Path(cp['resume_from']) if cp['resume_from'] else None,cp['allow_compatible_build'],cp['allow_policy_change'],
        cp['background'],cp['verify_history'])
    store=CheckpointStore(checkpoint_settings.directory,retain_completed=cp['retain_completed'],retain_daily=cp['retain_daily'])
    restore_start=(time.perf_counter(),time.process_time()) if checkpoint_settings.resume_from else None
    bundle=store.read(checkpoint_settings.resume_from) if checkpoint_settings.resume_from else None
    checkpoint_identity=physical_identity(config,{key:Path(value) for key,value in config['inputs'].items()
        if key.endswith('_path') and value is not None}) if (checkpoint_settings.enabled or bundle) else None
    relaxed={}
    if bundle:
        relaxed=validate_resume(bundle,checkpoint_identity,allow_compatible_build=checkpoint_settings.allow_compatible_build,
            allow_policy_change=checkpoint_settings.allow_policy_change)
        upgrade_state(bundle)
        if config['execution']['horizon_us']<bundle.manifest.saved_tick:raise CheckpointError('resume horizon precedes saved tick')
    if config['output']['live']!=(publication_adapter is not None):raise ValueError('live output requires an explicit publication adapter')
    publisher=Publisher(publication_adapter,clock=clock,sleep=sleep,**{k:v for k,v in config['output']['publication'].items() if k!='extensions'}) if config['output']['live'] else None
    from .preflight import prepare, assess, setup_scenario
    manufacturing,topology,topology_record=prepare(config,with_storage=True)
    findings=assess(config,manufacturing,topology)
    manufacturing,_=setup_scenario(config,manufacturing)
    if findings['status']=='rejected':
        raise ValueError('preflight rejected: '+', '.join(f['code'] for f in findings['findings'] if f['severity']=='error'))
    output=None;observer=None;projection=None
    event_hooks=[]
    def emit(kind,payload):
        if output is None:raise RuntimeError('domain transition during startup')
        for hook in event_hooks:hook(kind,payload)
        if observer is not None:observer.observe(kind,payload,s.now_us)
        # The summary is read only on output failure or at a checkpoint; defer
        # the fleet-wide assessment instead of recomputing it for every event.
        output.defer_summary(_event_summary)
        for stream,event_type,body in projection.project(kind,payload):
            record={'simulation_time_us':s.now_us,'event_type':event_type,'entity_refs':event_references(event_type,body),'payload':body}
            if stream=='diagnostics':
                output.write_diagnostics(({'record_type':event_type,'simulation_time_us':s.now_us,**body},))
                continue
            (output.commit_oht if stream=='oht' else output.commit)((record,))
    def _event_summary():
        observed=assessment();observed['settled']=False
        return {**observed,**classify_stop(observed)}
    owners=build_runtime(config,emit,initialize=bundle is None,prepared=(manufacturing,topology,topology_record,findings))
    event_hooks.append(owners.transfers.note_event)
    if bundle:
        if set(bundle.state)!={'runtime','progress','control','evidence'}:raise CheckpointError('invalid checkpoint state inventory')
        restore_runtime(owners,bundle.state['runtime'],owners.actions,verify_history=checkpoint_settings.verify_history)
    s,streams,custody,m,t,traffic=owners.scheduler,owners.streams,owners.custody,owners.manufacturing,owners.transfers,owners.traffic
    source,flow,campaigns,handoffs,clearance,maintenance,amhs_faults=owners.source,owners.flow,owners.campaigns,owners.handoffs,owners.clearance,owners.maintenance,owners.amhs_faults
    tools,vehicles,entry,exit=m.tools,t.vehicles,owners.entry,owners.exit;lots=m.lots
    settings=config['execution'];ongoing=source.mode=='continuous';targets=settings.get('exit_targets');exit_targets_reached_at_us=None
    from .campaigns import POLICY
    from .handoffs import POLICY as TRANSPORT_POLICY
    pacer=Pacer(publisher=publisher,clock=clock,sleep=sleep,rate=config['replay']['rate'])
    probes=Probes(**{k:v for k,v in config['probes'].items() if k!='extensions'})
    s.probes=flow.probes=m.probes=t.probes=probes
    capacity={'entry':len(entry),'exit':len(exit),'fleet':len(vehicles),'racks':{tid:len(tool['rack_slots']) for tid,tool in tools.items()},
              'ports':{tid:sum(d.get('equipment_id')==tid for d in topology['model']['docks']) for tid in tools}}
    from .progress import Progress
    from .history_archive import history_manifest
    def product_exits():
        return {p:observer.counts.get(p,{}).get('exited',0) for p,w in source.weights.items() if w>0}
    def progress_context():
        context={            'history_retirement':deepcopy(flow.retirement_metrics),'active_campaigns':campaigns.describe(),
            'active_processing_count':m.active_execution_count(),
            'active_transfer_count':t.transfer_counts()['committed_transfer_count'],
            **t.transfer_counts(),'active_handoffs':handoffs.describe(active_only=True),'handoff_waits':deepcopy(handoffs.waits),'clearance_leases':clearance.describe_active(),'clearance_history':history_manifest(clearance,'clearance-lease'),
            'scheduled_recoveries':[{'id':cause['id'],'recovery_at_us':cause['recovery_at_us']}
                for cause in maintenance.causes.values() if cause['status']=='active'],
            'setup_waits':[{'tool_id':tid,'installed_setup':setup.current,
                'completed_runs':setup.completed_lots,'minimum_runs':setup.minimum_runs}
                for tid,setup in sorted(m.setups.items()) if setup.completed_lots<setup.minimum_runs]}
        if ongoing:context.update(source_open=True,**flow.pending_admission_summary(s.now_us))
        if targets:
            counts=product_exits();context.update(exit_targets=dict(targets),product_exit_counts=counts,
                exit_targets_reached_at_us=exit_targets_reached_at_us,
                objective_achieved=all(counts[p]>=n for p,n in targets.items()))
        return context
    def observed_progress(record):
        progress({**record,'work_observation':{'counts':dict(probes.counts),
            'work_counts':dict(probes.work_counts),'cpu_ns':dict(probes.cpu_ns),'wall_ns':dict(probes.wall_ns),
            'timing_scope':'Process CPU/wall timers. Settlement includes scheduler phases; dispatch:traffic includes traffic_dispatch. Nested totals are not additive.'}})
    products=[p for p,w in source.weights.items() if w>0] if ongoing else [p for p,q in source.quotas.items() if q]
    observer=Progress(lots,m.rules.visits,products,
        report=observed_progress if progress is not None else None,interval=progress_interval,clock=observation_clock,context=progress_context,retired=flow.retired_facts)
    settlements=0;horizon=settings['horizon_us'];operator=False
    continuation=None
    if bundle:
        if set(bundle.state)!={'runtime','progress','control','evidence'}:
            raise CheckpointError('invalid checkpoint state inventory')
        observer.restore_state(bundle.state['progress'])
        control=bundle.state['control'];continuation=bundle.state['evidence']
        if set(control)!={'settlements','exit_targets','exit_targets_reached_at_us'}:
            raise CheckpointError('invalid checkpoint control state')
        settlements=control['settlements']
        exit_targets_reached_at_us=control['exit_targets_reached_at_us']
        if (settlements!=bundle.manifest.settlements or s.now_us!=bundle.manifest.saved_tick
                or bundle.state['runtime']['physical_digest']!=bundle.manifest.physical_digest
                or continuation['last_committed_sequence']!=bundle.manifest.last_committed_sequence
                or continuation['parent_run_id']!=bundle.manifest.parent_run_id
                or continuation['last_committed_time_us'] is not None and continuation['last_committed_time_us']>s.now_us):
            raise CheckpointError('checkpoint manifest/state watermark mismatch')
        if control['exit_targets']!=targets:
            exit_targets_reached_at_us=s.now_us if targets and all(product_exits()[p]>=n for p,n in targets.items()) else None
        continuation={**continuation,'parent_checkpoint':str(checkpoint_settings.resume_from.resolve()),
            'saved_tick':s.now_us,'settlements':settlements,
            'parent_exit_targets_reached_at_us':control['exit_targets_reached_at_us'],**relaxed}
    coordinator=CheckpointCoordinator(checkpoint_settings,store,clock=observation_clock) if checkpoint_settings.enabled else None
    def assessment():
        exited=observer.exited_count
        violation_ids,unresolved_obligations=assessed_obligations(m.rules.clocks,m.rules.retired_violation_ids)
        state={'now_us':s.now_us,'horizon_us':horizon,'cohort_size':source.count,'exited_count':exited,
            'pending_release_count':None if ongoing else source.count-len(lots)-flow.retired_facts['total']-len(flow.pending),'pending_admission_count':len(flow.pending),
            'wip_count':len(lots)+flow.retired_facts['total']-exited,'active_execution_count':m.active_execution_count(),
            **t.transfer_counts(),'output_status':'pending','settled':True,'operator_stop':operator,
            'checks_assessed':True,'violation_ids':violation_ids,
            'unresolved_obligations':unresolved_obligations}
        if ongoing:state.update(release_mode='ongoing',source_open=True)
        if targets:state.update(exit_targets=dict(targets),product_exit_counts=product_exits(),
            exit_targets_reached_at_us=exit_targets_reached_at_us)
        return state
    def output_failure():
        # The last on-disk manifest may still be pending if storage itself failed.
        return {**getattr(output,'final_manifest',{}),**output.summary,'outcome':'Failed','output_status':'failed',
            'reason':'required_output_failure','failure':output.failure,
            'last_committed_sequence':output.last_committed_sequence,'last_committed_time_us':output.last_committed_time_us}
    archives={}
    def _archive(directory):
        # Archives are stateless apart from their resolved directory; reuse
        # one per directory instead of resolving it for every owner.
        from .history_archive import HistoryArchive
        key=str(directory);archive=archives.get(key)
        if archive is None:archive=archives[key]=HistoryArchive(directory,deferred_sync=True)
        return archive
    def sync_archives():
        for archive in archives.values():archive.sync()
    def retire_history(sync=True,flush=None):
        from .history_archive import retire_closed_clocks
        head=m.rules.clock_archive
        archive=_archive(output.directory/'history')
        # Between checkpoints only full segments are published (small owners
        # batch up); synced retirements publish everything eligible.
        archive.flush=sync if flush is None else flush
        try:
            while retire_closed_clocks(m.rules,archive):pass
            from .history_archive import retire_recovered_causes
            head=maintenance.history_archive
            cause_archive=_archive(output.directory/'history')
            while retire_recovered_causes(maintenance,cause_archive):pass
            from .history_archive import retire_clearance_records,retire_campaign_records
            for owner,retire in ((clearance,retire_clearance_records),(campaigns,retire_campaign_records)):
                head=owner.history_archive
                ledger_archive=_archive(output.directory/'history')
                while retire(owner,ledger_archive):pass
            from .history_archive import retire_service_requests
            heap_kinds={event[3] for event in s._heap}
            for handler in (*m.handlers.values(),*t.exchange.handlers.values()):
                head=handler.history_archive
                service_archive=_archive(output.directory/'history')
                while retire_service_requests(handler,service_archive,heap_kinds):pass
            from .history_archive import retire_idle_requests
            head=traffic.history_archive
            idle_archive=_archive(output.directory/'history')
            while retire_idle_requests(traffic,idle_archive):pass
            from .history_archive import retire_transfer_groups
            head=t.history_archive
            transfer_archive=_archive(output.directory/'history')
            while retire_transfer_groups(t,transfer_archive):pass
            from .history_archive import retire_manufacturing_records
            head=m.executions_archive
            execution_archive=_archive(output.directory/'history')
            while retire_manufacturing_records(m,execution_archive):pass
            from .history_archive import retire_exited_lots
            head=flow.history_archive
            lot_archive=_archive(output.directory/'history')
            while retire_exited_lots(flow,lot_archive):pass
            # Checkpoints and the final manifest are captured only after a
            # synced retirement (sync=True); periodic retirements skip it.
            if sync:sync_archives()
        except (OSError,CheckpointError) as error:
            raise CheckpointWriteError('history archival failed: '+str(error)) from error

    def capture_checkpoint():
        retire_history()
        runtime=capture_runtime(owners)
        evidence=output.checkpoint_watermark()
        state={'runtime':runtime,'progress':observer.export_state(),
            'control':{'settlements':settlements,'exit_targets':targets,
                'exit_targets_reached_at_us':exit_targets_reached_at_us},
            'evidence':evidence}
        manifest=CheckpointManifest(1,STATE_CONTRACT,build_identity(),runtime_compatibility(),checkpoint_identity,
            s.now_us,settlements,output.run_id,str(checkpoint_settings.resume_from.resolve()) if bundle else None,
            evidence['last_committed_sequence'],runtime['physical_digest'],{})
        return CheckpointBundle(manifest,state)
    # Background saves: the parent retires history and fixes the output
    # watermark at the boundary; a forked child captures and writes the rest.
    def prepare_checkpoint():
        retire_history()
        return output.checkpoint_watermark()
    def build_checkpoint(evidence):
        runtime=capture_runtime(owners)
        state={'runtime':runtime,'progress':observer.export_state(),
            'control':{'settlements':settlements,'exit_targets':targets,
                'exit_targets_reached_at_us':exit_targets_reached_at_us},
            'evidence':evidence}
        manifest=CheckpointManifest(1,STATE_CONTRACT,build_identity(),runtime_compatibility(),checkpoint_identity,
            s.now_us,settlements,output.run_id,str(checkpoint_settings.resume_from.resolve()) if bundle else None,
            evidence['last_committed_sequence'],runtime['physical_digest'],{})
        return CheckpointBundle(manifest,state)
    if bundle:classify_stop(assessment())  # Reject invalid inherited observation before opening child output.
    restore_metrics={'wall_seconds':time.perf_counter()-restore_start[0],'cpu_seconds':time.process_time()-restore_start[1],
        'includes':'bundle read, validation, input preparation, allocation and state restore'} if restore_start else None
    projection=StreamProjection(traffic,t,custody)
    output=Output(config['output']['directory'],config['output']['run_id'],{'runtime_version':'physical-run-v1','event_streams':STREAMS_POLICY,
        'resolved_config':config,'capacity':capacity,'manufacturing_provenance':manufacturing['provenance'],
        'topology_provenance':topology['provenance'],'topology_artifact':topology_record,'release_policy':source.manifest(),
        **({'continuation':continuation} if bundle else {}),
        'controller_policy':POLICY,'transport_policy':TRANSPORT_POLICY,
        **({'setup_policy':config['execution']['setup_policy']} if 'setup_policy' in config['execution'] else {}),
        'batch_policy':BATCH_POLICY,'equipment_assignment_policy':ASSIGNMENT_POLICY,
        'clearance_pool':findings['clearance_pool'],'preflight':findings,'scenario':findings['scenario']},
        base_sequence=bundle.manifest.last_committed_sequence if bundle else 0,
        base_time_us=continuation['last_committed_time_us'] if bundle else None,
        publisher=publisher,**{key:config['output'][key] for key in ('chunk_records','chunk_bytes','max_record_bytes')})
    def loop_summary():
        observed=assessment();return {**observed,**classify_stop(observed)}
    CONTINUE={'stop_domain':False}
    try:
        resumed_check=bool(bundle)
        if bundle and t.exchange.down_tool_output:maintenance.release_exchange_pauses()
        # The decoded checkpoint is not needed once state is restored; release it
        # instead of holding a second copy of the state for the whole run.
        bundle=None
        while True:
            operator=settings.get('max_settlements') is not None and settlements>=settings['max_settlements']
            if operator:break
            # After the first iteration the previous iteration's final stop
            # classification already covered this unchanged state.
            if resumed_check and classify_stop(assessment())['stop_domain']:break
            resumed_check=False
            target=s.now_us if settlements==0 else min(s.next_time_us if s.next_time_us is not None else horizon,horizon)
            pacer.wait_for(target)
            with probes.handler('settlement'):s.settle(target)
            settlements+=1
            # Periodic retirement bounds memory; the archive is made durable
            # only where a checkpoint or the final manifest references it.
            if settlements%1000==0:retire_history(sync=False,flush=settlements%RETIRE_FLUSH_SETTLEMENTS==0)
            if targets and settings.get('max_settlements') is not None and settlements>=settings['max_settlements']:
                operator=True
            if targets and exit_targets_reached_at_us is None and all(product_exits()[p]>=n for p,n in targets.items()):
                exit_targets_reached_at_us=s.now_us
            # The checkpoint/failure summary is built from current state when read.
            if targets:output.defer_summary(loop_summary)
            observer.maybe_report(s.now_us,settlements)
            # With an open source only an operator stop, the exit targets or the
            # horizon can stop the run; the full assessment, which also checks the
            # observation, runs every 1000 settlements and before any stop.
            if (ongoing and settlements%1000 and not operator and s.now_us!=horizon
                    and not (targets and all(product_exits()[p]>=n for p,n in targets.items()))):
                stop=CONTINUE
            else:stop=classify_stop(assessment())
            if coordinator:
                with probes.handler('checkpoint_coordinator'):
                    coordinator.maybe_save(capture_checkpoint,boundary=(s.now_us,settlements),
                        background=(prepare_checkpoint,build_checkpoint))
            if stop['stop_domain']:break
        retire_history()
        if coordinator and not s.dirty:coordinator.maybe_save(capture_checkpoint,boundary=(s.now_us,settlements),final=True)
        state=assessment();state['output_status']='complete';summary={**state,**classify_stop(state)}
        summary.update(settlements=settlements,release_policy=source.manifest(),handoff_plans=handoffs.describe(active_only=True),handoff_history=history_manifest(handoffs,'handoff-plan'),handoff_waits=deepcopy(handoffs.waits),clearance_leases=clearance.describe_active(),clearance_history=history_manifest(clearance,'clearance-lease'),replay_lateness_s=pacer.max_lateness_s,
            probes={'counts':dict(probes.counts),'work_counts':dict(probes.work_counts),'wall_ns':dict(probes.wall_ns),'cpu_ns':dict(probes.cpu_ns),'cpu_unavailable':probes.cpu_unavailable,'traces':list(probes.traces)})
        if coordinator:summary['checkpoint_metrics']=coordinator.metrics
        if restore_metrics:summary['checkpoint_restore_metrics']=restore_metrics
        summary['progress']=observer.snapshot(s.now_us)
        observer.maybe_report(s.now_us,settlements,force=True)
        summary['domain_digest_version']='full-physical-state-archive-v9'
        summary['domain_digest']=domain_digest(state_projection({'scheduler':s,'streams':streams,'custody':custody,
            'manufacturing':m,'transfers':t,'traffic':traffic,'flow':flow,'maintenance':maintenance,'amhs_faults':amhs_faults}))
        if targets:output.summary=deepcopy(summary)
        return output.finalize(summary)
    except OutputFailure:
        return output_failure()
    except Exception as error:
        failure={'category':type(error).__name__,'message':str(error),'simulation_time_us':s.now_us,
                 'state_may_be_partial':True,'recent':list(s.recent),'traceback':traceback.format_exc()}
        snapshot={'lots':lots,'visits':m.rules.visits,'custody':custody.slots,'vehicles':vehicles,
                  'pending_admission':list(flow.pending),'transfers':{did:t._event(r) for did,r in t.demands.items()}}
        observed=assessment();observed['settled']=False
        constraints=classify_stop(observed)
        try:sync_archives()
        except OSError:pass  # The failure record below still reports the run.
        try:
            output.write_diagnostics((failure,))
            return output.finalize({**observed,**constraints,'outcome':'Failed','reason':'checkpoint_write_failure' if isinstance(error,CheckpointWriteError) else 'runtime_failure','failure':failure,'failure_state':snapshot})
        except OutputFailure:return output_failure()
