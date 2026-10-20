"""Immutable prepared topologies and validated generation-request lookup."""
from fractions import Fraction
from copy import deepcopy
from hashlib import sha256
import os
from pathlib import Path
import tempfile
from .generation import (ADAPTER, DEFAULT_GEOMETRY, _template, _weights, downstream_batch_tails,
                         generate_topology, load_prepared)
from .geometry import spatial_um
from .inputs import canonical_digest, canonical_text, read_json, validate_keys, verify_binding
from .json_stream import json_chunks
from .sources.smat import VALIDATOR, traffic_requirements, validate_topology
from .sources.smt import decimal, integer, validate_manufacturing
from .timebase import to_ticks


def default_directory(start=None):
    start=Path(start or Path.cwd()).resolve()
    for origin in (start,Path.cwd().resolve(),Path(__file__).resolve().parent):
        for root in (origin,*origin.parents):
            if (root/'pyproject.toml').is_file():return root/'topologies'
    return start/'topologies'


def _effective(manufacturing,reference,settings):
    policy=settings.get('layout_policy','single_spine_slots_v1')
    if policy not in ('single_spine_slots_v1','smat_reference_slots_v1','smat_area_flow_v1'):raise ValueError('unknown layout policy')
    validate_keys(settings,{'seed','profile','counts','geometry','footprints','fleet_count',
        'improvement_limit','product_weights','selected_streams','holding_pitch_mm','pickup_seconds',
        'drop_seconds','curve_cap_mm_s','entry_capacity','exit_capacity','rack_capacities',
        'layout_policy','clearance_mm','balance_weight','fit_tolerance','section_window_mm','section_max_swaps',
        'vehicle_speed_mm_s'},'generation')
    is_reference=policy in ('smat_reference_slots_v1','smat_area_flow_v1')
    if is_reference and 'geometry' in settings:raise ValueError('reference layout rejects legacy geometry')
    if not is_reference and 'clearance_mm' in settings:raise ValueError('clearance_mm requires reference layout policy')
    if is_reference and 'fleet_count' not in settings:raise ValueError('reference layout requires explicit fleet_count')
    if settings.get('profile','minimum_required')!='minimum_required':raise ValueError('unsupported generation profile')
    seed=settings.get('seed',0)
    if type(seed) is not int or seed<0:raise ValueError('seed must be a nonnegative integer')
    groups=traffic_requirements(manufacturing)['tool_groups'];tails=downstream_batch_tails(manufacturing)
    for name in ('counts','footprints','rack_capacities'):
        if not isinstance(settings.get(name,{}),dict) or set(settings.get(name,{}))-set(groups):
            raise ValueError('unknown '+name)
    counts={g:integer(settings.get('counts',{}).get(g,1),'counts',True) for g in groups}
    racks={};footprints={}
    for group,requirement in groups.items():
        setup_ids={(step.get('setup') or {}).get('id') for route in manufacturing['model']['routes']
            for step in route['steps'] if step.get('tool_group_id')==group}
        campaign=max((t['minimum_runs'] for t in manufacturing['model'].get('setup_transitions',[])
            if t['new'] in setup_ids),default=0)
        default=max(requirement['rack_min']+1,campaign+1,tails.get(group,{}).get('capacity_with_completion_and_spare',0))
        racks[group]=integer(settings.get('rack_capacities',{}).get(group,default),'rack capacity',True)
        if racks[group]<requirement['rack_min']:raise ValueError('rack capacity below source requirement')
        examples=[e for e in reference['model']['equipment'] if e['kind']=='ordinary' and e['tool_group_id']==group]
        if not examples:raise ValueError('missing physical footprint reference')
        fp=settings.get('footprints',{}).get(group)
        if group not in settings.get('footprints',{}):footprints[group]=[max(e['width_um'] for e in examples),max(e['height_um'] for e in examples)]
        else:
            validate_keys(fp,{'width_mm','height_mm'},'footprint')
            footprints[group]=[spatial_um(decimal(fp[k],'footprint',True)) for k in ('width_mm','height_mm')]
        if min(footprints[group])<=0:raise ValueError('footprint dimension quantizes to zero')
    geometry=deepcopy(settings.get('geometry') or DEFAULT_GEOMETRY)
    validate_keys(geometry,set(DEFAULT_GEOMETRY),'geometry')
    if set(geometry)!=set(DEFAULT_GEOMETRY):raise ValueError('geometry override must supply the complete profile')
    slots=integer(geometry['slots_per_side'],'slots_per_side',True)*2
    bays=(sum(counts.values())+slots-1)//slots
    # Use the generator's own geometry validation before accepting a cache hit.
    if not is_reference:_template(sum(counts.values()),settings.get('geometry'))
    geometry={k:(integer(v,'slots_per_side',True) if k=='slots_per_side' else
        [spatial_um(x) for x in v] if k in ('dock_offsets_mm','buffer_positions_mm') else spatial_um(v))
        for k,v in geometry.items()}
    weights=_weights(manufacturing,settings)
    effective={'seed':seed,'profile':'minimum_required','counts':counts,'rack_capacities':racks,
        'footprints_um':footprints,'geometry':geometry,
        'fleet_count':integer(settings.get('fleet_count',bays),'fleet_count',True),
        'improvement_limit':integer(settings.get('improvement_limit',10),'improvement_limit',True),
        'entry_capacity':integer(settings.get('entry_capacity',8),'entry_capacity',True),
        'exit_capacity':integer(settings.get('exit_capacity',8),'exit_capacity',True),
        'weights':{p:str(weights.get(p,0)) for p in sorted({r['product_id'] for r in manufacturing['model']['routes']})},
        'selected_streams':sorted(settings.get('selected_streams',[s.get('id') for s in manufacturing['model']['releases']]),key=str),
        'holding_pitch_um':spatial_um(decimal(settings.get('holding_pitch_mm',2000),'holding pitch',True)),
        'curve_cap_um_s':decimal(settings.get('curve_cap_mm_s',1000),'curve cap',True)*1000,
        'pickup_us':to_ticks(decimal(settings.get('pickup_seconds',10),'pickup',True)),
        'drop_us':to_ticks(decimal(settings.get('drop_seconds',10),'drop',True)),
        'extensions':settings.get('extensions',{})}
    if policy=='smat_area_flow_v1':
        effective['fit_tolerance']=str(decimal(settings.get('fit_tolerance','0.02'),'fit_tolerance'))
        if not 0<=Fraction(effective['fit_tolerance'])<=Fraction(1,10):raise ValueError('fit_tolerance must be within [0, 0.1]')
        effective.setdefault('balance_weight',str(decimal(settings.get('balance_weight','0'),'balance_weight')))
    if 'balance_weight' in settings:
        effective['balance_weight']=str(decimal(settings['balance_weight'],'balance_weight'))
        if Fraction(effective['balance_weight'])<0:raise ValueError('balance_weight must be nonnegative')
    # Optional, absent by default (topology identities without them are unchanged).
    if 'section_window_mm' in settings:
        if policy!='smat_area_flow_v1':raise ValueError('section_window_mm requires smat_area_flow_v1')
        effective['section_window_mm']=integer(settings['section_window_mm'],'section_window_mm',True)
        effective['section_max_swaps']=integer(settings.get('section_max_swaps',40),'section_max_swaps',True)
    if 'vehicle_speed_mm_s' in settings:
        effective['vehicle_speed_um_s']=decimal(settings['vehicle_speed_mm_s'],'vehicle_speed_mm_s',True)*1000
    if not effective['pickup_us'] or not effective['drop_us']:raise ValueError('handling must remain positive after quantization')
    if is_reference:
        effective.pop('geometry')
        effective.update(layout_policy=policy,site_version='reference-columns-xy-v2',
            clearance_um=spatial_um(decimal(settings.get('clearance_mm',500),'clearance_mm',True)))
        if effective['clearance_um']<=0:raise ValueError('clearance quantizes to zero')
    return effective


def _request(manufacturing,reference,settings):
    # Include the code involved in generation/validation, not installation paths.
    root=Path(__file__).parent
    modules=('generation.py','geometry.py','routing.py','topology.py','timebase.py',
             'inputs.py','sources/smat.py','sources/smt.py','topology_store.py','reference_layout.py')
    return {'schema':'topology-generation-request-v1','adapter_version':ADAPTER,
        'generator_code':{name:sha256((root/name).read_bytes()).hexdigest() for name in modules},
        'manufacturing_sha256':manufacturing['validation']['content_sha256'],
        'reference_sha256':reference['validation']['content_sha256'],
        'effective_settings':_effective(manufacturing,reference,settings)}


def _atomic(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.pending-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            for chunk in json_chunks(value):stream.write(chunk)
            stream.write(b'\n');stream.flush();os.fsync(stream.fileno())
        try:os.link(name,path)
        except FileExistsError:
            if canonical_text(read_json(path))!=canonical_text(value):
                raise ValueError('conflicting topology registration: '+str(path))
    finally:Path(name).unlink(missing_ok=True)


def persist(artifact,manufacturing,directory):
    if artifact.get('provenance',{}).get('adapter_version')==ADAPTER:
        if not verify_binding(artifact,traffic_requirements(manufacturing),VALIDATOR):
            raise ValueError('topology content/validation identity mismatch; reprepare')
    else:artifact=validate_topology(artifact,manufacturing)
    identity=artifact['validation']['content_sha256'];path=Path(directory).resolve()/(identity+'.json')
    _atomic(path,artifact)
    return artifact,{'path':str(path),'content_sha256':identity,'generation_key':None,'cache_hit':False}


def _registered(manufacturing,reference,request,key,root,entry):
    registration=read_json(entry)
    validate_keys(registration,{'request','content_sha256'},'topology registration')
    identity=registration.get('content_sha256')
    if canonical_text(registration.get('request'))!=canonical_text(request) or not isinstance(identity,str) or len(identity)!=64 or any(c not in '0123456789abcdef' for c in identity):
        raise ValueError('invalid topology generation registration')
    path=root/(identity+'.json');artifact=load_prepared(path,manufacturing)
    provenance=artifact['provenance']
    if (artifact['validation']['content_sha256']!=identity or
        provenance.get('manufacturing_sha256')!=request['manufacturing_sha256'] or
        provenance.get('physical_reference_sha256')!=request['reference_sha256'] or
        canonical_text(_effective(manufacturing,reference,provenance['generator_settings']))!=canonical_text(request['effective_settings'])):
        raise ValueError('registered topology does not match generation request')
    return artifact,{'path':str(path),'content_sha256':identity,'generation_key':key,'cache_hit':True}


def generated(manufacturing,reference,settings,directory):
    validate_manufacturing(manufacturing);validate_topology(reference,manufacturing)
    request=_request(manufacturing,reference,settings);key=canonical_digest(request)
    root=Path(directory).resolve();entry=root/'requests'/(key+'.json')
    if entry.exists():return _registered(manufacturing,reference,request,key,root,entry)
    artifact,record=persist(generate_topology(manufacturing,reference,settings),manufacturing,root)
    try:_atomic(entry,{'request':request,'content_sha256':record['content_sha256']})
    except ValueError:
        # Equivalent raw settings can yield distinct provenance/content hashes.
        # A concurrent winner is reusable only after the normal full checks.
        if not entry.exists():raise
        return _registered(manufacturing,reference,request,key,root,entry)
    record['generation_key']=key
    return artifact,record
