"""Deterministic reduced topology; placement is a proxy, not capacity planning."""
from collections import defaultdict
from copy import deepcopy
from fractions import Fraction
from heapq import heappop, heappush
from pathlib import Path
from .geometry import spatial_um, chord_length, overlaps
from .inputs import bind_validation, read_json, validate_keys, verify_binding
from .sources.smat import validate_topology, traffic_requirements, VALIDATOR
from .sources.smt import decimal, integer, validate_manufacturing
from .timebase import to_ticks
from .topology import compile_traffic, index

ADAPTER = 'generated-minimum-required-v1'
DEFAULT_GEOMETRY = {'length_mm':60000, 'width_mm':20000, 'pitch_mm':30000,
    'slots_per_side':8, 'first_slot_mm':6000, 'slot_pitch_mm':6000,
    'inset_mm':4500, 'envelope_long_mm':6000, 'envelope_trans_mm':9000,
    'clearance_mm':500, 'spine_front_mm':-10000, 'spine_back_mm':-20000,
    'end_margin_mm':10000, 'dock_offsets_mm':[-1500,-500,500,1500],
    'buffer_positions_mm':[54000,57000]}


def transition_graph(manufacturing, weights):
    graph = defaultdict(Fraction)
    for route in manufacturing['model']['routes']:
        weight = Fraction(weights.get(route['product_id'],0))
        steps = [s for s in route['steps'] if s['kind']=='processing']
        for a,b in zip(steps,steps[1:]):
            graph[(a['tool_group_id'],b['tool_group_id'])] += weight
        by = {s['source_step_id']:i for i,s in enumerate(route['steps'])}
        for s in steps:
            if s['rework'] is None: continue
            target = s['rework']['target_step_id']
            target_step = next((t for t in route['steps'][by[target]:] if t['kind']=='processing'),None)
            if target_step is None: raise ValueError('rework target has no executable continuation')
            graph[(s['tool_group_id'],target_step['tool_group_id'])] += (
                weight * Fraction(s['sampling_probability']) * Fraction(s['rework']['probability']))
    return {edge:weight for edge,weight in sorted(graph.items()) if weight}


def _weights(manufacturing, settings):
    streams = manufacturing['model']['releases']
    if 'selected_streams' in settings:
        names=settings['selected_streams']
        if not isinstance(names,list) or len(set(names))!=len(names) or not names:
            raise ValueError('selected_streams must be a unique nonempty ID list')
        if set(names)-{s['id'] for s in streams}: raise ValueError('unknown selected_streams')
        streams=[s for s in streams if s['id'] in names]
    rates=defaultdict(Fraction)
    for s in streams: rates[s['product_id']] += Fraction(s['lots_per_release'])/Fraction(s['interval_seconds'])
    raw=settings.get('product_weights',rates)
    if not isinstance(raw,dict) or set(raw)-{r['product_id'] for r in manufacturing['model']['routes']}:
        raise ValueError('unknown product_weights')
    weights={p:Fraction(decimal(v,'product_weights')) for p,v in raw.items()} if 'product_weights' in settings else dict(rates)
    total=sum(weights.values())
    if total<=0: raise ValueError('product_weights total must be positive')
    return {p:v/total for p,v in sorted(weights.items())}


def _template(count, profile):
    g = deepcopy(DEFAULT_GEOMETRY)
    if profile is not None:
        validate_keys(profile,set(DEFAULT_GEOMETRY),'geometry')
        if set(profile)!=set(DEFAULT_GEOMETRY): raise ValueError('geometry override must supply the complete profile')
        g=deepcopy(profile)
    slots_per_side=integer(g['slots_per_side'],'slots_per_side',True)
    values={key:spatial_um(value) for key,value in g.items()
            if key not in ('slots_per_side','dock_offsets_mm','buffer_positions_mm')}
    for key,value in values.items():
        if key not in ('spine_front_mm','spine_back_mm') and value<=0: raise ValueError('geometry dimensions must be positive')
    length,width,pitch=values['length_mm'],values['width_mm'],values['pitch_mm']
    if pitch<=width or not values['spine_back_mm']<values['spine_front_mm']<0:
        raise ValueError('invalid spine/bay geometry')
    if values['envelope_long_mm']>values['slot_pitch_mm'] or 2*values['envelope_trans_mm']>width:
        raise ValueError('overlapping slot envelopes')
    if 2*values['inset_mm']!=values['envelope_trans_mm']:
        raise ValueError('slot inset must center the transverse service envelope')
    offsets=[spatial_um(x) for x in g['dock_offsets_mm']]
    buffers=[spatial_um(x) for x in g['buffer_positions_mm']]
    if len(offsets)!=4 or len(set(offsets))!=4 or not buffers or len(set(buffers))!=len(buffers):
        raise ValueError('geometry requires four distinct dock offsets and nonempty distinct buffer positions')
    for value in offsets:
        if abs(value)>values['envelope_long_mm']//2-values['clearance_mm']:
            raise ValueError('dock offset outside slot service envelope')
    nodes=[]; rails=[]; bays=[]; slots=[]; buffer_docks=[]
    def node(name,x,y): nodes.append({'id':name,'x_um':x,'y_um':y,'zcu_id':None,'zcu_type':'NONE'})
    def rail(name,a,b,bay):
        by={n['id']:n for n in nodes}; pa,pb=by[a],by[b]
        size=chord_length((pa['x_um'],pa['y_um']),(pb['x_um'],pb['y_um']))
        if size<=2000000: raise ValueError('template rail must exceed 2000 mm')
        rails.append({'id':name,'from_node':a,'to_node':b,'bay_id':bay,'length_um':size,
                      'curve':False,'speed_multiplier':'1','geometry_mode':'chord',
                      'speed_sections':[{'start_um':0,'end_um':1000000,'curve':True},
                                        {'start_um':1000000,'end_um':size-1000000,'curve':False},
                                        {'start_um':size-1000000,'end_um':size,'curve':True}]})
    bay_count=(count+2*slots_per_side-1)//(2*slots_per_side)
    for i in range(bay_count):
        bid=f'Bay{i+1:03}'; x=pitch*i
        bays.append({'id':bid,'type':'INTRABAY'})
        corners=[(x,0),(x,length),(x+width,length),(x+width,0)]
        for k,(cx,cy) in enumerate(corners): node(f'{bid}:n{k}',cx,cy)
        for k in range(4): rail(f'{bid}:r{k}',f'{bid}:n{k}',f'{bid}:n{(k+1)%4}',bid)
        for side in ('L','R'):
            for k in range(slots_per_side):
                y=values['first_slot_mm']+k*values['slot_pitch_mm']
                if y-values['envelope_long_mm']//2<0 or y+values['envelope_long_mm']//2>length:
                    raise ValueError('slot envelope outside bay')
                port_offsets=[y+o if side=='L' else length-(y+o) for o in offsets]
                slots.append({'id':f'{bid}:{side}:{k:03}', 'bay_id':bid,
                              'x_um':x+values['inset_mm'] if side=='L' else x+width-values['inset_mm'],
                              'y_um':y, 'rail_id':f'{bid}:r{0 if side=="L" else 2}',
                              'port_offsets_um':sorted(port_offsets),
                              'envelope_long_um':values['envelope_long_mm'],
                              'envelope_trans_um':values['envelope_trans_mm'], 'clearance_um':values['clearance_mm']})
        for k,y in enumerate(buffers):
            if not 0<y<length: raise ValueError('buffer position outside bay')
            if any(y in s['port_offsets_um'] for s in slots if s['bay_id']==bid and s['rail_id']==f'{bid}:r0'):
                raise ValueError('buffer position coincides with equipment service access')
            buffer_docks.append({'id':f'{bid}:buffer:{k}', 'equipment_id':None,'kind':'track_buffer',
                                 'rail_id':f'{bid}:r0','offset_um':y,'role':'BOTH','carrier_capacity':1,'type_id':'buffer'})
    inter='InterBay'; bays.append({'id':inter,'type':'INTERBAY'})
    xs=sorted({-values['end_margin_mm'],pitch*(bay_count-1)+width+values['end_margin_mm']}
              |{pitch*i+d for i in range(bay_count) for d in (0,width)})
    front=values['spine_front_mm']; back=values['spine_back_mm']
    for k,x in enumerate(xs): node(f'inter:e{k:03}',x,front)
    node('inter:west-right',xs[-1],back); node('inter:west-left',xs[0],back)
    for k in range(len(xs)-1): rail(f'inter:east:{k:03}',f'inter:e{k:03}',f'inter:e{k+1:03}',inter)
    rail('inter:turn-right',f'inter:e{len(xs)-1:03}','inter:west-right',inter)
    rail('inter:west','inter:west-right','inter:west-left',inter)
    rail('inter:turn-left','inter:west-left','inter:e000',inter)
    for i in range(bay_count):
        bid=f'Bay{i+1:03}'
        rail(f'{bid}:entrance',f'inter:e{xs.index(pitch*i):03}',f'{bid}:n0',bid)
        rail(f'{bid}:exit',f'{bid}:n3',f'inter:e{xs.index(pitch*i+width):03}',bid)
    return {'id':'generated-fab','nodes':nodes,'rails':rails,'bays':bays,'slots':slots,
            'zcus':[],'docks':buffer_docks,'equipment':[],'vehicle_types':[],'vehicles':[]},g


def _slot_costs(model, vehicle_type, pickup_us, drop_us):
    from .routing import travel_seconds
    rails=index(model['rails'],'rails'); outgoing=defaultdict(list)
    for r in rails.values(): outgoing[r['from_node']].append(r)
    edge_cost={r['id']:travel_seconds(r,0,r['length_um'],vehicle_type,model['policy']) for r in rails.values()}
    distances={}
    for source in sorted(n['id'] for n in model['nodes']):
        best={}; pending=[(Fraction(0),source)]
        while pending:
            cost,node=heappop(pending)
            if node in best: continue
            best[node]=cost
            for r in outgoing[node]: heappush(pending,(cost+edge_cost[r['id']],r['to_node']))
        distances[source]=best
    costs={}
    for a in model['slots']:
        ra=rails[a['rail_id']]
        for b in model['slots']:
            rb=rails[b['rail_id']]
            candidates=[]
            for lo in a['port_offsets_um']:
                for hi in b['port_offsets_um']:
                    via=travel_seconds(ra,lo,ra['length_um'],vehicle_type,model['policy'])
                    via+=distances[ra['to_node']][rb['from_node']]
                    via+=travel_seconds(rb,0,hi,vehicle_type,model['policy'])
                    if ra['id']==rb['id'] and hi>=lo:
                        via=min(via,travel_seconds(ra,lo,hi,vehicle_type,model['policy']))
                    candidates.append(via)
            costs[(a['id'],b['id'])]=to_ticks(min(candidates))+pickup_us+drop_us
    return costs


def _place(instances, slots, graph, costs, limit, initial=None, balance=None):
    from functools import lru_cache
    from math import lcm
    from heapq import nsmallest

    slot_by=index(slots,'slots'); instance_by=index(instances,'instances')
    degree=defaultdict(Fraction)
    for (a,b),weight in graph.items(): degree[a]+=weight; degree[b]+=weight
    order=sorted(instances,key=lambda e:(-degree[e['tool_group_id']],e['tool_group_id'],e['id']))
    placed={}; by_group=defaultdict(list)
    affected_by_group={g:tuple(edge for edge in graph if g in edge)
                       for g in {e['tool_group_id'] for e in instances}}
    def fits(eq,slot):
        if 'envelope_width_um' in slot:
            return (eq['width_um']+2*slot['clearance_um']<=slot['envelope_width_um']
                    and eq['height_um']+2*slot['clearance_um']<=slot['envelope_height_um'])
        return (eq['width_um']+2*slot['clearance_um']<=slot['envelope_long_um']
                and eq['height_um']+2*slot['clearance_um']<=slot['envelope_trans_um'])

    @lru_cache(maxsize=32768)
    def nearest(origin, destinations):
        # Prefix reuse shares work when greedy placement appends a destination.
        # Chunking bounds recursion even for unusually large replication counts.
        if len(destinations)>64:
            split=1 << ((len(destinations)-1).bit_length()-1)
            return min(nearest(origin,destinations[:split]),nearest(origin,destinations[split:]))
        last=costs[(origin,destinations[-1])]
        return min(nearest(origin,destinations[:-1]),last) if len(destinations)>1 else last

    def coefficients(edges):
        fractions={edge:Fraction(graph[edge])/len(by_group[edge[0]])
                   for edge in edges if by_group[edge[0]]}
        scale=lcm(*(f.denominator for f in fractions.values()))
        return {edge:int(f*scale) for edge,f in fractions.items()},scale

    def term(edge,weights):
        a,b=edge
        # Each member is its own zero-cost destination on a same-group edge.
        if a==b or not by_group[a] or not by_group[b]: return 0
        destinations=tuple(placed[e] for e in by_group[b])
        return weights[edge]*sum(nearest(placed[e],destinations) for e in by_group[a])

    for eq in order:
        if initial is not None:
            if set(initial)!={e['id'] for e in instances} or len(set(initial.values()))!=len(initial):
                raise ValueError('invalid initial placement assignment')
            sid=initial[eq['id']]
            if sid not in slot_by or not fits(eq,slot_by[sid]):raise ValueError('invalid initial placement footprint')
            placed[eq['id']]=sid;by_group[eq['tool_group_id']].append(eq['id'])
            continue
        affected=affected_by_group[eq['tool_group_id']]
        by_group[eq['tool_group_id']].append(eq['id'])
        weights,_=coefficients(affected)
        candidates=[];used=set(placed.values())
        for slot in slots:
            if slot['id'] in used or not fits(eq,slot): continue
            placed[eq['id']]=slot['id']
            # Unaffected terms are constant across these candidates. A common
            # exact denominator allows integer comparison without rounding.
            candidates.append((sum(term(edge,weights) for edge in affected),slot['id']))
        if not candidates: raise ValueError(f"footprint of {eq['id']} cannot fit any available slot")
        _,placed[eq['id']]=min(candidates)

    weights,scale=coefficients(graph)
    total=sum(term(edge,weights) for edge in graph)
    initial=Fraction(total,scale); swaps=[]; termination='local_optimum'
    # Optional bay balance: score = travel + w * travel0 * S / S0, where S is the
    # sum of squared bay loads (integer milli-moves per day per tool). Compared
    # exactly in integers; w = 0 (or None) leaves the travel-only search.
    if balance is not None and balance['weight']:
        w=Fraction(balance['weight']);tool_load=balance['tool_load'];slot_bay={s['id']:s['bay_id'] for s in slots}
        bay_load=defaultdict(int)
        for e,sid in placed.items():bay_load[slot_bay[sid]]+=tool_load[instance_by[e]['tool_group_id']]
        squares=lambda:sum(v*v for v in bay_load.values())
        s0=squares();travel0=total
        if not s0 or not travel0:w=Fraction(0)
    else:w=Fraction(0)
    initial_squares=squares() if w else None
    keys=sorted(placed)
    # Counts are fixed during swaps, so edge coefficients and their common
    # denominator remain valid for the entire improvement pass.
    affected_pairs={}
    for iteration in range(limit):
        terms={edge:term(edge,weights) for edge in graph}; winner=None
        # Removing one destination needs at most the previous two nearest
        # choices. These rows are fixed throughout this swap-search pass.
        best_two={edge:{e:nsmallest(2,((costs[(placed[e],placed[f])],f)
                                     for f in by_group[edge[1]]))
                        for e in by_group[edge[0]]}
                  for edge in graph if edge[0]!=edge[1] and by_group[edge[1]]}
        for i,a in enumerate(keys):
            for b in keys[i+1:]:
                ga,gb=instance_by[a]['tool_group_id'],instance_by[b]['tool_group_id']
                if ga==gb: continue  # Permuting a group's slots cannot improve its objective.
                if not fits(instance_by[a],slot_by[placed[b]]) or not fits(instance_by[b],slot_by[placed[a]]): continue
                pair=tuple(sorted((ga,gb)))
                if pair not in affected_pairs:
                    affected_pairs[pair]=tuple(edge for edge in graph if ga in edge or gb in edge)
                affected=affected_pairs[pair]
                placed[a],placed[b]=placed[b],placed[a]
                def candidate_term(edge):
                    source,destination=edge
                    if source==destination: return 0
                    if not by_group[source] or not by_group[destination]: return 0
                    if destination in pair:
                        removed=a if destination==ga else b
                        replacement=placed[removed]
                        remaining=tuple(placed[e] for e in by_group[destination] if e!=removed)
                        subtotal=0
                        for origin in by_group[source]:
                            if origin in (a,b):
                                previous=nearest(placed[origin],remaining) if remaining else None
                            else:
                                choices=best_two[edge][origin]
                                choice=1 if choices[0][1]==removed else 0
                                previous=choices[choice][0] if choice<len(choices) else None
                            incoming=costs[(placed[origin],replacement)]
                            subtotal+=incoming if previous is None else min(previous,incoming)
                        return weights[edge]*subtotal
                    # Only one origin moved on this edge. All other nearest
                    # distances are unchanged, so update its exact contribution.
                    moved=a if source==ga else b
                    original=placed[b] if moved==a else placed[a]
                    destinations=tuple(placed[e] for e in by_group[destination])
                    if not destinations: return 0
                    delta=nearest(placed[moved],destinations)-nearest(original,destinations)
                    return terms[edge]+weights[edge]*delta
                candidate=total-sum(terms[e] for e in affected)+sum(candidate_term(e) for e in affected)
                placed[a],placed[b]=placed[b],placed[a]
                if w:
                    ba,bb=slot_bay[placed[a]],slot_bay[placed[b]]
                    if ba==bb:delta_s=0
                    else:
                        la,lb=tool_load[ga],tool_load[gb];x,y=bay_load[ba],bay_load[bb]
                        delta_s=(x-la+lb)**2+(y-lb+la)**2-x*x-y*y
                    score=(candidate-total)*s0*w.denominator+w.numerator*travel0*delta_s
                    if score<0 and (winner is None or (score,a,b)<winner[:3]):winner=(score,a,b,candidate)
                elif candidate<total and (winner is None or (candidate,a,b)<winner): winner=(candidate,a,b)
        if winner is None: break
        if w:
            _,a,b,total=winner;ga,gb=instance_by[a]['tool_group_id'],instance_by[b]['tool_group_id']
            ba,bb=slot_bay[placed[a]],slot_bay[placed[b]]
            if ba!=bb:
                bay_load[ba]+=tool_load[gb]-tool_load[ga];bay_load[bb]+=tool_load[ga]-tool_load[gb]
        else:total,a,b=winner
        placed[a],placed[b]=placed[b],placed[a]; swaps.append([a,b])
        if iteration==limit-1: termination='iteration_limit'
    return placed,{'initial_objective':str(initial),'final_objective':str(Fraction(total,scale)),
                   'swaps':swaps,'termination':termination,'improvement_limit':limit,
                   'graph_policy':'nominal_consecutive_rework_v1',
                   'cost_policy':'minimum_port_loaded_ticks_v1','algorithm':'greedy_best_pair_swap_v1',
                   **({'balance_weight':str(w),'balance_policy':'bay_squared_load_v1',
                       'initial_bay_squares':initial_squares,'final_bay_squares':squares()} if w else {})}


def downstream_batch_tails(manufacturing):
    """Conservative producer retention for distinct future batch visit keys.

    A processed LOT cannot depart into an uncommitted batch assembly. Include
    sampled steps until the next mandatory processing step, without forecasting
    draws. Different product/route/step keys cannot assemble together.
    """
    targets = defaultdict(dict)
    for route in manufacturing['model']['routes']:
        steps = [step for step in route['steps'] if step['kind'] == 'processing']
        for index, origin in enumerate(steps):
            if Fraction(origin['sampling_probability']) == 0: continue
            for target in steps[index + 1:]:
                if target.get('batch_min') and Fraction(target['sampling_probability']) > 0:
                    key = (route['product_id'], route['id'], target['source_step_id'])
                    targets[origin['tool_group_id']][key] = (target['batch_min'] + 24) // 25
                if Fraction(target['sampling_probability']) == 1: break
    return {group: {'capacity_with_completion_and_spare': sum(size - 1 for size in batches.values()) + 2,
        'targets': [{'product_id': key[0], 'route_id': key[1], 'source_step_id': key[2],
                     'minimum_lots': size} for key, size in sorted(batches.items())]}
        for group, batches in sorted(targets.items())}


def generate_topology(manufacturing: dict, physical_reference: dict, settings: dict) -> dict:
    validate_manufacturing(manufacturing); validate_topology(physical_reference,manufacturing)
    policy=settings.get('layout_policy','single_spine_slots_v1')
    if policy in ('smat_reference_slots_v1','smat_area_flow_v1'):
        from .reference_layout import generate_reference_topology
        return generate_reference_topology(manufacturing,physical_reference,settings)
    if policy!='single_spine_slots_v1':raise ValueError('unknown layout policy')
    settings={k:v for k,v in settings.items() if k!='layout_policy'}
    validate_keys(settings,{'seed','profile','counts','geometry','footprints','fleet_count',
                            'improvement_limit','product_weights','selected_streams',
                            'holding_pitch_mm','pickup_seconds','drop_seconds','curve_cap_mm_s',
                            'entry_capacity','exit_capacity','rack_capacities'},'generation')
    seed=settings.get('seed',0)
    if type(seed) is not int or seed<0: raise ValueError('seed must be a nonnegative integer')
    if settings.get('profile','minimum_required')!='minimum_required': raise ValueError('unsupported generation profile')
    requirements=traffic_requirements(manufacturing); groups=requirements['tool_groups']
    counts=settings.get('counts',{}); footprints=settings.get('footprints',{});racks=settings.get('rack_capacities',{})
    if not isinstance(racks,dict) or set(racks)-set(groups):raise ValueError('unknown rack capacities')
    for group,value in racks.items():
        if integer(value,'rack capacity',True)<groups[group]['rack_min']:raise ValueError('rack capacity below source requirement')
    for value,name in ((counts,'counts'),(footprints,'footprints')):
        if not isinstance(value,dict) or set(value)-set(groups): raise ValueError(f'unknown {name}')
    defaults = {};batch_tails=downstream_batch_tails(manufacturing)
    for group in groups:
        setup_ids = {(step.get('setup') or {}).get('id') for route in manufacturing['model']['routes']
            for step in route['steps'] if step.get('tool_group_id') == group}
        campaign_min = max((t['minimum_runs'] for t in manufacturing['model'].get('setup_transitions', [])
            if t['new'] in setup_ids), default=0)
        defaults[group] = max(groups[group]['rack_min'] + 1, campaign_min + 1,
            batch_tails.get(group,{}).get('capacity_with_completion_and_spare',0))
    reference=physical_reference['model']; instances=[]
    for group in sorted(groups):
        examples=[e for e in reference['equipment'] if e['kind']=='ordinary' and e['tool_group_id']==group]
        if not examples: raise ValueError('missing physical footprint reference')
        width=max(e['width_um'] for e in examples); height=max(e['height_um'] for e in examples)
        if group in footprints:
            fp=footprints[group]; validate_keys(fp,{'width_mm','height_mm'},'footprint')
            width=spatial_um(decimal(fp['width_mm'],'footprint width',True))
            height=spatial_um(decimal(fp['height_mm'],'footprint height',True))
        if width<=0 or height<=0: raise ValueError('footprint dimension quantizes to zero')
        for i in range(integer(counts.get(group,1),'counts',True)):
            instances.append({'id':f'{group}:tool:{i+1:03}','tool_group_id':group,'kind':'ordinary',
                              'width_um':width,'height_um':height,'rack_capacity':racks.get(group,defaults[group]),
                              'coordinate_reference':'center','orientation':'width_along_rail'})
    model,geometry=_template(len(instances),settings.get('geometry'))
    model['vehicle_types']=[dict(t) for t in reference['vehicle_types']]
    if not model['vehicle_types']: raise ValueError('missing vehicle metadata')
    vehicle_type=next((t for t in model['vehicle_types'] if t['id']=='OHTV0'),model['vehicle_types'][0])
    pickup=to_ticks(decimal(settings.get('pickup_seconds',10),'pickup_seconds',True))
    drop=to_ticks(decimal(settings.get('drop_seconds',10),'drop_seconds',True))
    if not pickup or not drop: raise ValueError('handling must remain positive after quantization')
    model['policy']={'geometry':'chord_integer_sqrt_ties_even_v1','coordinate_reference':'center',
                     'holding_pitch_um':spatial_um(decimal(settings.get('holding_pitch_mm',2000),'holding_pitch_mm',True)),
                     'curve_cap_um_s':str(decimal(settings.get('curve_cap_mm_s',1000),'curve_cap_mm_s',True)*1000),
                     'pickup_us':pickup,'drop_us':drop,'raw_dock_clamp':False}
    weights=_weights(manufacturing,settings); graph=transition_graph(manufacturing,weights)
    costs=_slot_costs(model,vehicle_type,pickup,drop)
    placed,history=_place(instances,model['slots'],graph,costs,integer(settings.get('improvement_limit',10),'improvement_limit',True))
    by_slot=index(model['slots'],'slots')
    for eq in instances:
        slot=by_slot[placed[eq['id']]]
        eq.update(slot_id=slot['id'],bay_id=slot['bay_id'],x_um=slot['x_um'],y_um=slot['y_um'])
        model['equipment'].append(eq)
        for k,offset in enumerate(slot['port_offsets_um']):
            model['docks'].append({'id':eq['id']+f':port:{k}', 'equipment_id':eq['id'],
                                  'type_id':'equipment','kind':'ordinary','rail_id':slot['rail_id'],
                                  'offset_um':offset,'role':'BOTH','carrier_capacity':1})
    east=sorted((r for r in model['rails'] if r['id'].startswith('inter:east:')),key=lambda r:r['id'])
    for kind,rail,offset,role in (('entry',east[0],2000000,'OUT'),('exit',east[-1],east[-1]['length_um']-2000000,'IN')):
        model['equipment'].append({'id':kind,'tool_group_id':kind.upper(),'kind':kind,'bay_id':'InterBay',
                                   'rack_capacity':integer(settings.get(kind+'_capacity',8),kind+'_capacity',True)})
        model['docks'].append({'id':kind+':dock','equipment_id':kind,'kind':kind,'type_id':kind,
                              'rail_id':rail['id'],'offset_um':offset,'role':role,'carrier_capacity':1})
    compile_traffic(model)
    count=integer(settings.get('fleet_count',len([b for b in model['bays'] if b['type']=='INTRABAY'])),'fleet_count',True)
    bay_ids=sorted(b['id'] for b in model['bays'] if b['type']=='INTRABAY')+['InterBay']
    queues={bid:[h for h in model['holding_points'] if h['bay_id']==bid] for bid in bay_ids}
    cursors={bid:0 for bid in bay_ids}; occupied=defaultdict(list); half=model['traffic']['type_half_um'][vehicle_type['id']]
    while len(model['vehicles'])<count:
        added=False
        for bid in bay_ids:
            queue=queues[bid]
            while cursors[bid]<len(queue):
                h=queue[cursors[bid]]; cursors[bid]+=1; envelope=(h['offset_um']-half,h['offset_um']+half)
                if any(overlaps(envelope,x) for x in occupied[h['rail_id']]): continue
                occupied[h['rail_id']].append(envelope)
                model['vehicles'].append({'id':f'oht:{len(model["vehicles"])+1:04}', 'type_id':vehicle_type['id'],
                                          'rail_id':h['rail_id'],'offset_um':h['offset_um'],'carrier_id':None})
                added=True; break
            if len(model['vehicles'])==count: break
        if not added: raise ValueError('requested fleet cannot fit nonoverlapping holding space')
    compile_traffic(model)
    provenance={'adapter_version':ADAPTER,'rack_policy':'source-campaign-and-batch-tail-spare-v3','downstream_batch_tails':batch_tails,'resolved_rack_defaults':defaults,'generator_settings':deepcopy(settings),'resolved_geometry':geometry,'seed':seed,
                'manufacturing_sha256':manufacturing['validation']['content_sha256'],
                'physical_reference_sha256':physical_reference['validation']['content_sha256'],
                'placement':history,'topology_product_weights':{p:str(w) for p,w in weights.items()},
                'transition_graph':[{'from':a,'to':b,'weight':str(w)} for (a,b),w in graph.items()],
                'count_profile':'minimum_required','geometry_version':'single_spine_slots_v1',
                'capacity_version':'finite_source_required_v1'}
    return bind_validation({'schema_version':1,'model':model,'provenance':provenance},requirements,VALIDATOR)


def load_prepared(path: Path, manufacturing: dict) -> dict:
    artifact=read_json(path)
    if artifact.get('provenance',{}).get('adapter_version')==ADAPTER:
        validate_keys(artifact,{'schema_version','model','provenance','validation'},'topology')
        if type(artifact.get('schema_version')) is not int or artifact['schema_version']!=1:
            raise ValueError('unsupported topology schema')
        if not verify_binding(artifact,traffic_requirements(manufacturing),VALIDATOR):
            raise ValueError('topology content/validation identity mismatch; reprepare')
        return artifact
    return validate_topology(artifact,manufacturing)
