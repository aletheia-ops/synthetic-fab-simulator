"""Deterministic scenario checks; static feasibility is not a progress guarantee."""
from fractions import Fraction
from .campaigns import POLICY
from .workload import ReleaseSource
from .manufacturing import BATCH_POLICY
from .transfers import ASSIGNMENT_POLICY


def setup_scenario(config, manufacturing):
    """Create a runtime policy overlay after original input/topology validation.

    This is not a prepared artifact: discard its source validation binding.
    Physical topology remains the one prepared against the original source.
    """
    from copy import deepcopy
    overrides=config['execution'].get('setup_minimum_runs',{})
    if (not isinstance(overrides,dict) or any(not isinstance(k,str) or not k
            or type(v) is not int or v<1 for k,v in overrides.items())):
        raise ValueError('setup_minimum_runs must map setup names to positive integers')
    transitions=manufacturing['model'].get('setup_transitions',[])
    unknown=set(overrides)-{t['new'] for t in transitions}
    if unknown:raise ValueError('unknown setup_minimum_runs targets: '+str(sorted(unknown)))
    changes=[{'transition_id':t['id'],'new_setup':t['new'],
        'source_minimum_runs':t['minimum_runs'],'effective_minimum_runs':overrides[t['new']]}
        for t in transitions if t['new'] in overrides]
    record={'kind':'setup_minimum_runs_sensitivity_v1' if overrides else 'source_baseline',
        'base_manufacturing_sha256':manufacturing.get('validation',{}).get('content_sha256'),
        'changes':sorted(changes,key=lambda c:c['transition_id'])}
    if not overrides:return manufacturing,record
    effective=deepcopy(manufacturing)
    effective.pop('validation',None)
    for transition in effective['model']['setup_transitions']:
        if transition['new'] in overrides:transition['minimum_runs']=overrides[transition['new']]
    return effective,record


def prepare(config, *, with_storage=False):
    from pathlib import Path
    from .inputs import read_json
    from .sources.smt import validate_manufacturing
    from .generation import load_prepared
    manufacturing = validate_manufacturing(read_json(config['inputs']['manufacturing_path']))
    from .topology_store import default_directory, generated, persist
    directory=config.get('output',{}).get('topology_directory',default_directory())
    supplied = config['inputs'].get('topology_path')
    if supplied is not None:
        if config['generation']:
            raise ValueError('generation settings require topology generation, not a supplied topology')
        topology = load_prepared(Path(supplied), manufacturing)
        topology,record=persist(topology,manufacturing,directory)
    else:
        reference = config['inputs'].get('physical_reference_path')
        if reference is None:
            raise ValueError('generation requires physical_reference_path')
        topology,record=generated(manufacturing,load_prepared(Path(reference),manufacturing),config['generation'],directory)
    return (manufacturing,topology,record) if with_storage else (manufacturing,topology)


def assess(config, manufacturing, topology):
    manufacturing,scenario=setup_scenario(config,manufacturing)
    source = ReleaseSource(manufacturing, config['workload'])
    if 'exit_targets' in config['execution']:
        from .outcomes import validate_exit_targets
        validate_exit_targets(config['execution']['exit_targets'],{s['product_id'] for s in source.streams.values()
            if source.weights[s['product_id']]>0})
    model = topology['model']
    equipment = {e['id']: e for e in model['equipment']}
    tools = {tid: e for tid, e in equipment.items() if e['kind'] == 'ordinary'}
    docks = {d['id']: d for d in model['docks']}
    findings = []
    def finding(severity, code, entities, **evidence):
        record = {'severity': severity, 'code': code, 'entity_ids': sorted(entities), 'evidence': evidence}
        if record not in findings:
            findings.append(record)
    from .flow import root_batch_staging_requirement,ongoing_root_staging_requirements
    capacities={}
    for tool in tools.values():capacities.setdefault(tool['tool_group_id'],[]).append(tool['rack_capacity'])
    routes={r['id']:r for r in manufacturing['model']['routes']}
    staging=root_batch_staging_requirement(routes,source,capacities) if source.mode=='finite' else 0
    entry_capacity=sum(e['rack_capacity'] for e in equipment.values() if e['kind']=='entry')
    if entry_capacity<staging:
        finding('warning','finite_batch_remainder_staging_risk',[],
                entry_capacity=entry_capacity,required_entry_capacity=staging,
                guard_enabled=False)
    if source.mode=='continuous':
        for root in ongoing_root_staging_requirements(routes,source):
            if entry_capacity<root['minimum_lots']:
                finding('warning','ongoing_root_batch_staging_risk',[root['product_id'],root['route_id']],
                    **root,entry_capacity=entry_capacity,
                    limitations=['A per-root minimum does not cover interleaving release streams.',
                                 'Sufficient entry capacity alone does not establish runtime liveness.'])
    inputs = {tid: sorted(d['id'] for d in docks.values() if d.get('equipment_id') == tid
                         and d.get('role', 'BOTH') in ('IN', 'BOTH')) for tid in tools}
    outputs = {tid: sorted(d['id'] for d in docks.values() if d.get('equipment_id') == tid
                          and d.get('role', 'BOTH') in ('OUT', 'BOTH')) for tid in tools}
    for tid in tools:
        if not inputs[tid]: finding('error', 'missing_input_dock', [tid])
        if not outputs[tid]: finding('error', 'missing_output_dock', [tid])
    from .generation import downstream_batch_tails
    tails = downstream_batch_tails(manufacturing)
    for tid, tool in tools.items():
        envelope = tails.get(tool['tool_group_id'])
        if envelope and tool['rack_capacity'] < envelope['capacity_with_completion_and_spare']:
            finding('warning', 'downstream_batch_tail_capacity_risk', [tid],
                rack_capacity=tool['rack_capacity'], **envelope)
    # Pure routing on the supplied directed model; no clock, scheduler or RNG.
    from .routing import DirectedReachability
    connectivity=DirectedReachability(model)
    path_cache = {}
    vehicle_type = model['vehicles'][0]['type_id'] if model['vehicles'] else None
    def reachable(a, b):
        if (a, b) not in path_cache:
            if vehicle_type is None:
                path_cache[a, b] = False
            else:
                path_cache[a,b]=connectivity.reachable(docks[a],docks[b])
        return path_cache[a, b]
    entries = [d['id'] for d in docks.values() if d['kind'] == 'entry' and d.get('role', 'BOTH') in ('OUT', 'BOTH')]
    exits = [d['id'] for d in docks.values() if d['kind'] == 'exit' and d.get('role', 'BOTH') in ('IN', 'BOTH')]
    if not entries: finding('error', 'missing_entry_dock', [])
    if not exits: finding('error', 'missing_exit_dock', [])
    if not model['vehicles']: finding('error', 'missing_fleet', [])
    for route in manufacturing['model']['routes']:
        quota = source.quotas.get(route['product_id'], 0) if source.mode=='finite' else None
        if source.mode=='finite' and not quota: continue
        if source.weights.get(route['product_id'],0)<=0:continue
        if route['id'] not in {stream['route_id'] for stream in source.streams.values()}: continue
        steps = [step for step in route['steps'] if step['kind'] == 'processing']
        eligible = {}
        for step in steps:
            ids = sorted(tid for tid, e in tools.items() if e['tool_group_id'] == step['tool_group_id'])
            eligible[step['source_step_id']] = ids
            entities = [route['product_id'], route['id'], step['source_step_id'], *ids]
            if not ids: finding('error', 'missing_tool_group', entities, tool_group_id=step['tool_group_id'])
            if step.get('batch_min'):
                minimum = (step['batch_min'] + 24) // 25
                if quota is not None and quota < minimum:
                    mandatory = Fraction(step['sampling_probability']) == 1
                    finding('error' if mandatory else 'warning',
                        'batch_cohort_shortage' if mandatory else 'conditional_batch_shortage', entities,
                        quota=quota, minimum_lots=minimum, sampling_probability=step['sampling_probability'])
            target = (step.get('setup') or {}).get('id')
            minimum = max((t['minimum_runs'] for t in manufacturing['model'].get('setup_transitions', [])
                           if target is not None and t['new'] == target), default=0)
            if minimum:
                if step.get('batch_min'):
                    finding('error', 'unsupported_batch_setup_campaign', entities, minimum_runs=minimum)
                # Only staged campaigns stage minimum-run LOTs at the tool.
                staged = config['execution'].get('setup_policy', 'staged_campaign') == 'staged_campaign'
                for tid in ids if staged else ():
                    if tools[tid]['rack_capacity'] < minimum + 1:
                        finding('error', 'campaign_staging_capacity', [tid, target],
                            rack_capacity=tools[tid]['rack_capacity'], minimum_runs=minimum, required_with_spare=minimum + 1)
                if quota is not None and quota < minimum:
                    # Other products with the same setup can supply a campaign;
                    # per-product quota alone is not an impossibility proof.
                    finding('warning', 'small_setup_supply', entities, quota=quota, minimum_runs=minimum)
        sequence = [entries, *([dock for tid in eligible[step['source_step_id']] for dock in inputs[tid]] for step in steps), exits]
        for index in range(len(sequence) - 1):
            origins = sequence[index] if index == 0 else [dock for tid in eligible[steps[index - 1]['source_step_id']] for dock in outputs[tid]]
            destinations = sequence[index + 1]
            if not any(reachable(a, b) for a in origins for b in destinations):
                finding('error', 'unreachable_route_delivery', [route['id']],
                    from_step='entry' if index == 0 else steps[index - 1]['source_step_id'],
                    to_step=steps[index]['source_step_id'] if index < len(steps) else 'exit')
        for step in steps:
            rework = step.get('rework')
            if not rework or Fraction(rework['probability']) <= 0: continue
            route_ids = [item['source_step_id'] for item in route['steps']]
            target_index = route_ids.index(rework['target_step_id'])
            target = next(item for item in route['steps'][target_index:] if item['kind'] == 'processing')
            origins = [dock for tid in eligible[step['source_step_id']] for dock in outputs[tid]]
            destinations = [dock for tid in eligible[target['source_step_id']] for dock in inputs[tid]]
            if not any(reachable(a, b) for a in origins for b in destinations):
                finding('error', 'unreachable_rework_delivery', [route['id'], step['source_step_id'], target['source_step_id']])
        for left, right in zip(steps, steps[1:]):
            if left['tool_group_id'] != right['tool_group_id']: continue
            left_setup = (left.get('setup') or {}).get('id')
            right_setup = (right.get('setup') or {}).get('id')
            # Zero-minimum ordinary transitions can commit the successor in
            # its existing rack. Positive-minimum recipes require campaign
            # clearance while the remaining members drain or stage.
            campaign_change = right_setup is not None and left_setup != right_setup and any(
                transition['minimum_runs'] > 0
                and transition['new'] in (left_setup, right_setup)
                for transition in manufacturing['model'].get('setup_transitions', []))
            needs_clearance = bool(left.get('batch_min') or right.get('batch_min')
                or campaign_change)
            if not needs_clearance: continue
            same_instance = any(left['source_step_id'] in component['source_step_ids']
                and right['source_step_id'] in component['source_step_ids']
                for component in route.get('dedication_components', []))
            for tid in eligible[left['source_step_id']]:
                alternatives = [d['id'] for d in docks.values() if d['kind'] == 'track_buffer'
                    and d.get('role', 'BOTH') in ('IN', 'BOTH')]
                alternatives += [dock for other in eligible[right['source_step_id']] if other != tid and not same_instance for dock in inputs[other]]
                if not any(reachable(a, b) and any(reachable(b, dest) for dest in inputs[tid])
                           for a in outputs[tid] for b in alternatives):
                    finding('error', 'no_same_tool_clearance', [tid, route['id'], left['source_step_id'], right['source_step_id']])
    from .clearance import clearance_requirements
    from .handoffs import POLICY as TRANSPORT_POLICY
    clearance=clearance_requirements(manufacturing,topology)
    findings.extend(clearance['findings'])
    findings.sort(key=lambda f: (f['severity'], f['code'], f['entity_ids']))
    status = 'rejected' if any(f['severity'] == 'error' for f in findings) else 'warnings' if findings else 'ready'
    return {'schema': 'fab-preflight-v1', 'status': status, 'controller_policy': POLICY,'transport_policy':TRANSPORT_POLICY,'clearance_pool':clearance,'scenario':scenario,
            'batch_policy':BATCH_POLICY,'equipment_assignment_policy':ASSIGNMENT_POLICY,
            'findings': findings, 'limitations': ['Directed reachability does not establish traffic feasibility.',
                'Sampling, rework, finite tails and future congestion require runtime observation.']}


def preflight(config_path):
    from .runner import load_config
    config = load_config(config_path)
    return assess(config, *prepare(config))
