"""Reference service sites and explicitly derived, axis-aligned machine bodies.

Body coordinates are a project placement policy, not recovered SMAT semantics.
Rails and service ports remain fixed. Packing failure is a search failure, not
a proof that every possible arrangement is infeasible.
"""
from collections import defaultdict, Counter
from copy import deepcopy
from .topology import index

SITE_VERSION = 'reference-columns-xy-v2'


def extract_sites(model, clearance_um):
    rails = index(model['rails'], 'rails')
    result = []
    for eq in sorted(model['equipment'], key=lambda e: e['id']):
        if eq['kind'] != 'ordinary':
            continue
        docks = [deepcopy(d) for d in model['docks'] if d.get('equipment_id') == eq['id']]
        roles = {d['role'] for d in docks}
        if not ('BOTH' in roles or {'IN', 'OUT'} <= roles):
            raise ValueError(f"{eq['id']}: missing service roles")
        for dock in docks:
            rail = rails.get(dock['rail_id'])
            if rail is None or not 0 <= dock['offset_um'] <= rail['length_um']:
                raise ValueError(f"{eq['id']}: invalid service dock {dock['id']}")
        if min(eq['width_um'], eq['height_um']) <= 0:
            raise ValueError(f"{eq['id']}: invalid body dimensions")
        result.append({k: eq[k] for k in ('bay_id', 'x_um', 'y_um', 'width_um', 'height_um')} |
                      {'id': eq['id'], 'source_equipment_id': eq['id'],
                       'source_group_id': eq['tool_group_id'], 'orientation': 'source_xy',
                       'source_x_um': eq['x_um'], 'source_y_um': eq['y_um'], 'source_docks': docks})
    index(result, 'sites')
    return result


def assign_sites(instances, sites, clearance_um, *, neutral=False):
    """Complete bipartite matching for independent, fixed-envelope compatibility.

    neutral: no preference for a site's source group; a site labelled with an
    area only takes machines of that area."""
    by_machine = index(instances, 'instances')
    by_site = index(sites, 'sites')
    choices = {}
    for eq in sorted(instances, key=lambda e: e['id']):
        choices[eq['id']] = sorted((s['id'] for s in sites
            if eq['width_um'] <= s['width_um'] and eq['height_um'] <= s['height_um']
            and (not neutral or s.get('area') is None or s['area'] == eq.get('area'))),
            key=lambda sid: ((not neutral and by_site[sid]['source_group_id'] != eq['tool_group_id']), sid))
    occupied = {}
    def visit(machine, seen):
        for sid in choices[machine]:
            if sid in seen:
                continue
            seen.add(sid)
            if sid not in occupied or visit(occupied[sid], seen):
                occupied[sid] = machine
                return True
        return False
    # Constrained machines first also preserves source-group matches where possible.
    order = sorted(by_machine, key=lambda mid: (len(choices[mid]),
        not neutral and not any(by_site[s]['source_group_id'] == by_machine[mid]['tool_group_id'] for s in choices[mid]), mid))
    for mid in order:
        if not visit(mid, set()):
            raise ValueError(f"{by_machine[mid]['tool_group_id']} instance {mid}: incompatible sites {sorted(by_site)}")
    return {mid: sid for sid, mid in sorted(occupied.items())}


def _conflict(a, b, clearance):
    return (2*abs(a['x_um']-b['x_um']) < a['width_um']+b['width_um']+2*clearance and
            2*abs(a['y_um']-b['y_um']) < a['height_um']+b['height_um']+2*clearance)


def _service_centers(model, bay_id, height):
    """Integer center intervals that permit a legal port within the body span."""
    from fractions import Fraction
    from math import ceil, floor
    nodes=index(model['nodes'],'nodes');half=model['traffic']['common_half_um']
    blocked={rid for p in model['traffic']['passages'] for rid in p['rail_ids']}
    ranges=[]
    for rail in model['rails']:
        if rail['bay_id']!=bay_id or rail['id'] in blocked or rail.get('curve',False):continue
        a,b=nodes[rail['from_node']],nodes[rail['to_node']]
        if a['x_um']!=b['x_um'] or a['y_um']==b['y_um'] or rail['length_um']<2*half:continue
        ends=[a['y_um']+Fraction((b['y_um']-a['y_um'])*offset,rail['length_um'])
              for offset in (half,rail['length_um']-half)]
        ranges.append((ceil(min(ends)-Fraction(height,2)),floor(max(ends)+Fraction(height,2))))
    return ranges


def validate_placement(model, instances, sites, assignment, clearance_um):
    by_site = index(sites, 'sites')
    if set(assignment) != {e['id'] for e in instances} or len(set(assignment.values())) != len(instances):
        raise ValueError('invalid machine assignment')
    bodies = []
    for eq in instances:
        site = by_site.get(assignment[eq['id']])
        if site is None or eq['width_um'] > site['width_um'] or eq['height_um'] > site['height_um']:
            raise ValueError(f"invalid assignment envelope for {eq['id']}")
        bodies.append(eq | {'id': site['id'], 'x_um': site['x_um'], 'y_um': site['y_um']})
    fixed = [e for e in model['equipment'] if e['kind'] != 'ordinary' and
             all(k in e for k in ('x_um', 'y_um', 'width_um', 'height_um'))]
    for i, body in enumerate(bodies):
        for other in bodies[i+1:] + fixed:
            if _conflict(body, other, clearance_um):
                raise ValueError(f"clearance conflict: {body['id']} and {other['id']}")


def column_spans(model, sites, max_width, clearance_um):
    """Free packing segment lengths per source column (bay rail Y bounds minus fixed bodies)."""
    nodes = index(model['nodes'], 'nodes')
    fixed = [e for e in model['equipment'] if e['kind'] != 'ordinary' and
             all(k in e for k in ('x_um','y_um','width_um','height_um'))]
    spans = {}
    for bid, x in sorted({(s['bay_id'], s['x_um']) for s in sites}):
        points = [nodes[r[k]] for r in model['rails'] if r['bay_id'] == bid for k in ('from_node','to_node')]
        intervals = [(min(n['y_um'] for n in points), max(n['y_um'] for n in points))]
        for obstacle in fixed:
            if 2*abs(x-obstacle['x_um']) >= max_width+obstacle['width_um']+2*clearance_um:
                continue
            low = obstacle['y_um'] - (obstacle['height_um']+1)//2 - clearance_um
            high = obstacle['y_um'] + (obstacle['height_um']+1)//2 + clearance_um
            intervals = [(a,b) for start,end in intervals for a,b in
                         ((start,min(end,low)),(max(start,high),end)) if b > a]
        spans[(bid, x)] = tuple(b - a for a, b in intervals)
    return spans


def derive_sites(model, instances, clearance_um, *, columns_for=None, site_area=None):
    """Pack source-width-X bodies along source columns within bay rail Y bounds.

    Each column retains its number of fixed source service sites. The decreasing
    height heuristic allocates machines to columns/obstacle-free intervals, then
    maps them to source ports and translates a packed row toward those positions.
    No source orientation is inferred; global WIDTH-X is an explicit new policy.
    """
    sites = extract_sites(model, clearance_um)
    if site_area is not None:
        for s in sites:s['area'] = site_area[s['id']]
    if not instances:
        raise ValueError('empty requested equipment')
    max_width = max(e['width_um'] for e in instances)
    nodes = index(model['nodes'], 'nodes')
    columns = defaultdict(list)
    for site in sites:
        columns[(site['bay_id'], site['x_um'])].append(site)
    bins = []
    fixed = [e for e in model['equipment'] if e['kind'] != 'ordinary' and
             all(k in e for k in ('x_um','y_um','width_um','height_um'))]
    for key, members in sorted(columns.items()):
        bid, x = key
        points = [nodes[r[k]] for r in model['rails'] if r['bay_id'] == bid for k in ('from_node','to_node')]
        if not points:
            raise ValueError(f'placement search: no bay rail bounds for {bid}')
        intervals = [(min(n['y_um'] for n in points), max(n['y_um'] for n in points))]
        for obstacle in fixed:
            if 2*abs(x-obstacle['x_um']) >= max_width+obstacle['width_um']+2*clearance_um:
                continue
            low = obstacle['y_um'] - (obstacle['height_um']+1)//2 - clearance_um
            high = obstacle['y_um'] + (obstacle['height_um']+1)//2 + clearance_um
            intervals = [(a,b) for start,end in intervals for a,b in
                         ((start,min(end,low)),(max(start,high),end)) if b > a]
        bins.append({'key':key, 'sites':members, 'segments':[{'low':a,'high':b,'used':0,'jobs':[]} for a,b in intervals],
                     'jobs':[], 'groups':{s['source_group_id'] for s in members}})
    if columns_for is not None:
        # Columns chosen by the caller; fill each column's segments in order.
        # A column may hold more tools than its source sites when its length
        # allows: extra positions copy the column's port template (ports are
        # re-projected beside each body afterwards).
        by_key = {column['key']: column for column in bins}
        wanted = Counter(columns_for.values())
        for key, column in by_key.items():
            template = sorted(column['sites'], key=lambda s: s['id'])
            for n in range(max(0, wanted[key] - len(template))):
                extra = deepcopy(template[n % len(template)])
                extra['id'] = f"{template[n % len(template)]['id']}+{n + 1}"
                column['sites'].append(extra); sites.append(extra)
        for eq in sorted(instances, key=lambda e:(-e['height_um'],e['tool_group_id'],e['id'])):
            column = by_key[columns_for[eq['id']]]
            for segment in column['segments']:
                extra = eq['height_um'] + eq['height_um'] % 2 + (clearance_um if segment['jobs'] else 0)
                if segment['used'] + extra <= segment['high'] - segment['low']:
                    column['jobs'].append(eq); segment['jobs'].append(eq); segment['used'] += extra
                    break
            else:
                raise ValueError(f"placement search: column {column['key']} has no length for {eq['id']}")
    for eq in ([] if columns_for is not None else sorted(instances, key=lambda e:(-e['height_um'],e['tool_group_id'],e['id']))):
        candidates = []
        for n, column in enumerate(bins):
            if len(column['jobs']) >= len(column['sites']):
                continue
            # Existing column spacing is checked exactly by validate_placement.
            for k, segment in enumerate(column['segments']):
                extra = eq['height_um'] + eq['height_um'] % 2 + (clearance_um if segment['jobs'] else 0)
                if segment['used'] + extra <= segment['high'] - segment['low']:
                    # Tall bodies go to columns with the most remaining length
                    # per fixed service site, preserving sites for short bodies.
                    available = sum(s['high']-s['low']-s['used'] for s in column['segments'])
                    remaining = len(column['sites'])-len(column['jobs'])
                    candidates.append(((-available/remaining,
                        eq['tool_group_id'] not in column['groups'], n, k), column, segment, extra))
        if not candidates:
            raise ValueError(f"placement search could not allocate {eq['tool_group_id']} instance {eq['id']}; "
                             f"assigned={sum(len(c['jobs']) for c in bins)}/{len(instances)}, "
                             f"body_plus_clearance_um={sum(e['height_um']+clearance_um for e in instances)}, "
                             f"segment_capacity_um={sum(s['high']-s['low']+clearance_um for c in bins for s in c['segments'])}; "
                             'fixed rails/ports unchanged; this is not a proof of global infeasibility')
        _, column, segment, extra = min(candidates, key=lambda item:item[0])
        column['jobs'].append(eq); segment['jobs'].append(eq); segment['used'] += extra
    assignment = {}
    by_site = index(sites, 'sites')
    for column in bins:
        # Generous temporary envelopes permit port reassignment before body packing.
        envelopes = [s | {'width_um':max_width, 'height_um':max(e['height_um'] for e in instances)} for s in column['sites']]
        mapping = assign_sites(column['jobs'], envelopes, clearance_um, neutral=columns_for is not None)
        assignment.update(mapping)
        for segment in column['segments']:
            jobs = sorted(segment['jobs'], key=lambda e:(by_site[mapping[e['id']]]['source_y_um'], e['id']))
            if not jobs:
                continue
            offsets = []; cursor = 0
            for eq in jobs:
                offsets.append(cursor + (eq['height_um']+1)//2)
                cursor += eq['height_um'] + eq['height_um'] % 2 + clearance_um
            shifts = sorted(by_site[mapping[e['id']]]['source_y_um'] - offset for e,offset in zip(jobs,offsets))
            allowed=[(segment['low'],segment['high']-segment['used'])]
            if 'traffic' in model:
                for eq,offset in zip(jobs,offsets):
                    centers=_service_centers(model,column['key'][0],eq['height_um'])
                    allowed=[(max(a,low-offset),min(b,high-offset)) for a,b in allowed
                             for low,high in centers if max(a,low-offset)<=min(b,high-offset)]
                    # Merge duplicates/overlap from opposite bay service rails.
                    merged=[]
                    for a,b in sorted(set(allowed)):
                        if merged and a<=merged[-1][1]+1:merged[-1]=(merged[-1][0],max(b,merged[-1][1]))
                        else:merged.append((a,b))
                    allowed=merged
            if not allowed:raise ValueError(f"placement search: no serviceable packed translation for {column['key']}")
            target=shifts[len(shifts)//2]
            start=min((max(a,min(target,b)) for a,b in allowed),key=lambda value:(abs(value-target),value))
            for eq, offset in zip(jobs, offsets):
                site = by_site[mapping[eq['id']]]
                site.update(y_um=start+offset, width_um=eq['width_um'], height_um=eq['height_um'],
                            orientation='derived_width_x_height_y')
    validate_placement(model, instances, sites, assignment, clearance_um)
    return sites, assignment


def relocate_ports(model, sites, assignment):
    """Project ordinary ports beside derived bodies on legal existing bay rails."""
    from fractions import Fraction
    nodes = index(model['nodes'], 'nodes'); by_site = index(sites, 'sites')
    blocked = {rid for p in model['traffic']['passages'] for rid in p['rail_ids']}
    half = model['traffic']['common_half_um']
    result = []; mapping = []
    for mid, sid in sorted(assignment.items()):
        site = by_site[sid]; choices = []
        for rail in model['rails']:
            if rail['bay_id'] != site['bay_id'] or rail['id'] in blocked or rail.get('curve',False):
                continue
            a,b = nodes[rail['from_node']], nodes[rail['to_node']]
            dx,dy = b['x_um']-a['x_um'], b['y_um']-a['y_um']
            if dx != 0 or dy == 0 or rail['length_um'] < 2*half:
                continue
            offset = round(Fraction(site['y_um']-a['y_um'],dy)*rail['length_um'])
            offset = max(half,min(offset,rail['length_um']-half))
            y = a['y_um']+Fraction(dy*offset,rail['length_um'])
            if 2*abs(y-site['y_um']) > site['height_um']:
                continue
            lateral = max(0,2*abs(a['x_um']-site['x_um'])-site['width_um'])
            choices.append((lateral*lateral+4*(y-site['y_um'])**2,rail['id'],offset,rail))
        if not choices:
            nearby=[(r['id'],r.get('curve'),r['length_um'],r['id'] in blocked,
                     nodes[r['from_node']]['y_um'],nodes[r['to_node']]['y_um'])
                    for r in model['rails'] if r['bay_id']==site['bay_id']]
            raise ValueError(f'{mid}: no legal existing rail beside derived body at site {sid}; '
                             f"bay={site['bay_id']} center_y={site['y_um']} height={site['height_um']} rails={nearby}")
        _,_,center,rail = min(choices)
        source = sorted(site['source_docks'],key=lambda d:d['id'])
        span = min(site['height_um']//2, max(0,rail['length_um']-2*half))
        for k,dock in enumerate(source):
            delta = round(Fraction((2*k-len(source)+1)*span,2*max(1,len(source))))
            offset = max(half,min(center+delta,rail['length_um']-half))
            a,b = nodes[rail['from_node']],nodes[rail['to_node']]
            y = a['y_um']+Fraction((b['y_um']-a['y_um'])*offset,rail['length_um'])
            if 2*abs(y-site['y_um']) > site['height_um']:
                offset = center
            new_id = f'{mid}:port:{k}'
            result.append(deepcopy(dock) | {'id':new_id, 'equipment_id':mid,
                          'rail_id':rail['id'], 'offset_um':offset, 'source_offset_um':offset})
            mapping.append({'source_dock_id':dock['id'],'dock_id':new_id,
                            'source_rail_id':dock['rail_id'],'source_offset_um':dock['offset_um'],
                            'rail_id':rail['id'],'offset_um':offset})
    return result,mapping


def resolve_reference_settings(manufacturing, reference, settings):
    # The cache and generator use the same normalization/validation contract.
    from .topology_store import _effective
    return _effective(manufacturing,reference,settings)


class DirectedSiteCosts:
    """Lazy exact loaded travel costs, with shortest-path rows shared by sites."""
    def __init__(self, model, ports, sites, assignment, vehicle_type):
        from .routing import travel_seconds
        self.travel=travel_seconds;self.model=model;self.vehicle_type=vehicle_type
        self.rails=index(model['rails'],'rails');self.outgoing=defaultdict(list)
        self.edge={r['id']:travel_seconds(r,0,r['length_um'],vehicle_type,model['policy']) for r in model['rails']}
        for r in model['rails']:self.outgoing[r['from_node']].append(r)
        self.rows={};self.cache={};self.service={}
        for mid,sid in assignment.items():
            docks=[d for d in ports if d['equipment_id']==mid]
            rail=self.rails[docks[0]['rail_id']]
            outgoing=[d['offset_um'] for d in docks if d['role'] in ('OUT','BOTH')]
            incoming=[d['offset_um'] for d in docks if d['role'] in ('IN','BOTH')]
            self.service[sid]=(rail,outgoing,incoming,
                min(travel_seconds(rail,x,rail['length_um'],vehicle_type,model['policy']) for x in outgoing),
                min(travel_seconds(rail,0,x,vehicle_type,model['policy']) for x in incoming))

    def __getitem__(self, pair):
        from fractions import Fraction
        from heapq import heappop,heappush
        from .timebase import to_ticks
        if pair in self.cache:return self.cache[pair]
        ra,outgoing,_,tail,_=self.service[pair[0]]
        rb,_,incoming,_,head=self.service[pair[1]]
        start=ra['to_node']
        if start not in self.rows:
            best={};pending=[(Fraction(0),start)]
            while pending:
                cost,node=heappop(pending)
                if node in best:continue
                best[node]=cost
                for rail in self.outgoing[node]:
                    if rail['to_node'] not in best:heappush(pending,(cost+self.edge[rail['id']],rail['to_node']))
            self.rows[start]=best
        if rb['from_node'] not in self.rows[start]:raise ValueError('unreachable reference service pair')
        value=tail+self.rows[start][rb['from_node']]+head
        if ra['id']==rb['id']:
            direct=[self.travel(ra,a,b,self.vehicle_type,self.model['policy']) for a in outgoing for b in incoming if b>=a]
            if direct:value=min(value,min(direct))
        result=to_ticks(value)+self.model['policy']['pickup_us']+self.model['policy']['drop_us']
        self.cache[pair]=result
        return result


def generate_reference_topology(manufacturing, reference, settings):
    from .geometry import overlaps
    from .inputs import bind_validation, canonical_digest
    from .sources.smat import traffic_requirements, VALIDATOR
    from .topology import compile_traffic
    from .generation import ADAPTER, downstream_batch_tails, _place, _weights, transition_graph
    resolved = resolve_reference_settings(manufacturing,reference,settings)
    instances = []
    for group,count in sorted(resolved['counts'].items()):
        width,height = resolved['footprints_um'][group]
        for k in range(count):
            instances.append({'id':f'{group}:tool:{k+1:03}', 'kind':'ordinary','tool_group_id':group,
                'width_um':width,'height_um':height,'rack_capacity':resolved['rack_capacities'][group],
                'coordinate_reference':'center','orientation':'derived_width_x_height_y'})
    source = reference['model']
    area_history = None
    if resolved['layout_policy'] == 'smat_area_flow_v1':
        from collections import Counter
        from .area_placement import assign_columns
        from .generation import _weights as _w, transition_graph as _tg
        area_of_group = {g['id']: g['area'] for g in manufacturing['model']['tool_groups']}
        for eq in instances:eq['area'] = area_of_group[eq['tool_group_id']]
        source_sites = extract_sites(source, resolved['clearance_um'])
        spans = column_spans(source, source_sites, max(e['width_um'] for e in instances), resolved['clearance_um'])
        vt = next((t for t in source['vehicle_types'] if t['id']=='OHTV0'),source['vehicle_types'][0])
        source_ports = [d for d in source['docks'] if d.get('equipment_id') in {s['id'] for s in source_sites}]
        site_costs = DirectedSiteCosts(source, source_ports, source_sites, {s['id']: s['id'] for s in source_sites}, vt)
        from fractions import Fraction as _F
        rates = {}
        for rel in manufacturing['model']['releases']:
            if resolved['selected_streams'] and rel['id'] not in resolved['selected_streams']:continue
            rates[rel['route_id']] = rates.get(rel['route_id'],0)+_F(86400)/_F(rel['interval_seconds'])*rel['lots_per_release']
        visits = {}
        for route in manufacturing['model']['routes']:
            for step in route['steps']:
                if step['kind']=='processing':
                    visits[step['tool_group_id']] = visits.get(step['tool_group_id'],0)+rates.get(route['id'],0)*_F(step['sampling_probability'])
        counts = resolved['counts']
        tool_load = {g: round(2000*visits.get(g,0)/counts[g]) for g in counts if counts[g]}
        # Feasible start: the source-column packing of the reference policy.
        start_sites, start = derive_sites(source, instances, resolved['clearance_um'])
        start_by = {x['id']: (x['bay_id'], x['source_x_um']) for x in start_sites}
        initial = {mid: start_by[sid] for mid, sid in start.items()}
        columns_for, area_history = assign_columns(source_sites, instances,
            _tg(manufacturing,_w(manufacturing,settings)), site_costs, clearance_um=resolved['clearance_um'],
            tolerance=resolved['fit_tolerance'], tool_load=tool_load, balance_weight=resolved['balance_weight'], spans=spans,
            initial=initial)
        if resolved.get('section_window_mm'):
            from .area_placement import balance_sections
            columns_for, section_history = balance_sections(source_sites, instances,
                _tg(manufacturing,_w(manufacturing,settings)), site_costs, clearance_um=resolved['clearance_um'],
                tolerance=resolved['fit_tolerance'], tool_load=tool_load, spans=spans, columns_for=columns_for,
                window_um=int(resolved['section_window_mm'])*1000, max_swaps=int(resolved.get('section_max_swaps',40)))
            area_history = {**area_history, 'section_balance': section_history}
        sites,assignment = derive_sites(source,instances,resolved['clearance_um'],columns_for=columns_for)
    else:
        sites,assignment = derive_sites(source,instances,resolved['clearance_um'])
    model = deepcopy(source)
    model['id'] = 'generated-smat-reference-fab'
    model['equipment'] = [e for e in model['equipment'] if e['kind'] != 'ordinary']
    model['docks'] = [d for d in model['docks'] if d['kind'] != 'ordinary']
    model['vehicles'] = []
    allocations = {}
    for kind in ('entry','exit'):
        facilities = sorted((e for e in model['equipment'] if e['kind']==kind),key=lambda e:e['id'])
        total = resolved[kind+'_capacity']
        if not facilities or total < len(facilities):
            raise ValueError(f'{kind} capacity below preserved facility count')
        q,r = divmod(total,len(facilities))
        for k,eq in enumerate(facilities):eq['rack_capacity']=q+(k<r)
        allocations[kind] = {e['id']:e['rack_capacity'] for e in facilities}
    model['policy'].update({k:resolved[k] for k in ('holding_pitch_um','pickup_us','drop_us')})
    model['policy']['curve_cap_um_s'] = str(resolved['curve_cap_um_s'])
    model['policy']['raw_dock_clamp'] = False
    ports,port_mapping = relocate_ports(source,sites,assignment)
    by_site = index(sites,'sites')
    vehicle_type = next((t for t in model['vehicle_types'] if t['id']=='OHTV0'),model['vehicle_types'][0])
    if 'vehicle_speed_um_s' in resolved:
        # Straight-track speed override (SMAT2022 paper Table 1: 5.0 m/s); placement
        # above keeps the source vehicle's travel costs so the layout is speed-independent.
        from .sources.smat import text as _text
        vehicle_type['speed_um_s']=_text(resolved['vehicle_speed_um_s'])
    weights=_weights(manufacturing,settings);graph=transition_graph(manufacturing,weights)
    slots=[s | {'envelope_width_um':s['width_um']+2*resolved['clearance_um'],
                'envelope_height_um':s['height_um']+2*resolved['clearance_um'],
                'clearance_um':resolved['clearance_um']} for s in sites if s['id'] in assignment.values()]
    # Costs use the effective motion settings and the relocated service points.
    costs=DirectedSiteCosts(model,ports,sites,assignment,vehicle_type)
    initial=assignment
    balance=None
    if 'balance_weight' in resolved and resolved['layout_policy']!='smat_area_flow_v1':
        # Expected pickups + drops per day per tool: 2 * group visits / tool count.
        from fractions import Fraction as _F
        rates={}
        for rel in manufacturing['model']['releases']:
            if resolved['selected_streams'] and rel['id'] not in resolved['selected_streams']:continue
            rates[rel['route_id']]=rates.get(rel['route_id'],0)+_F(86400)/_F(rel['interval_seconds'])*rel['lots_per_release']
        visits={}
        for route in manufacturing['model']['routes']:
            for step in route['steps']:
                if step['kind']=='processing':
                    visits[step['tool_group_id']]=visits.get(step['tool_group_id'],0)+rates.get(route['id'],0)*_F(step['sampling_probability'])
        counts=resolved['counts']
        balance={'weight':resolved['balance_weight'],
                 'tool_load':{g:round(2000*visits.get(g,0)/counts[g]) for g in counts if counts[g]}}
    assignment,history=_place(instances,slots,graph,costs,resolved['improvement_limit'],initial=assignment,balance=balance)
    validate_placement(source,instances,sites,assignment,resolved['clearance_um'])
    # Physical port poses follow the occupied site when group identities swap.
    old_site_to_mid={sid:mid for mid,sid in initial.items()}
    new_mid_to_old={mid:old_site_to_mid[sid] for mid,sid in assignment.items()}
    previous=ports;ports=[];by_old={mid:[d for d in previous if d['equipment_id']==mid] for mid in initial}
    old_mapping={d['dock_id']:d for d in port_mapping};port_mapping=[]
    for mid,old in sorted(new_mid_to_old.items()):
        for k,dock in enumerate(by_old[old]):
            new_id=f'{mid}:port:{k}'
            ports.append(dock | {'id':new_id,'equipment_id':mid})
            port_mapping.append(old_mapping[dock['id']] | {'dock_id':new_id})
    for eq in instances:
        site = by_site[assignment[eq['id']]]
        eq.update(bay_id=site['bay_id'],slot_id=site['id'],x_um=site['x_um'],y_um=site['y_um'])
        model['equipment'].append(eq)
    model['docks'].extend(ports)
    model['slots'] = sites
    compile_traffic(model)
    half = model['traffic']['type_half_um'][vehicle_type['id']]
    queues = defaultdict(list)
    for h in model['holding_points']:queues[h['bay_id']].append(h)
    occupied = defaultdict(list)
    while len(model['vehicles']) < resolved['fleet_count']:
        added = False
        for bid in sorted(queues):
            queue = queues[bid]
            while queue:
                h = queue.pop(0); envelope=(h['offset_um']-half,h['offset_um']+half)
                if any(overlaps(envelope,other) for other in occupied[h['rail_id']]):continue
                occupied[h['rail_id']].append(envelope)
                model['vehicles'].append({'id':f'oht:{len(model["vehicles"])+1:04}', 'type_id':vehicle_type['id'],
                    'rail_id':h['rail_id'],'offset_um':h['offset_um'],'carrier_id':None})
                added=True;break
            if len(model['vehicles']) == resolved['fleet_count']:break
        if not added:raise ValueError('requested fleet cannot fit nonoverlapping holding space')
    compile_traffic(model)
    provenance = {'adapter_version':ADAPTER,'layout_policy':resolved['layout_policy'],
        **({'area_placement':area_history} if area_history is not None else {}),
        'geometry_version':SITE_VERSION,'generator_settings':deepcopy(settings),
        'manufacturing_sha256':manufacturing['validation']['content_sha256'],
        'physical_reference_sha256':reference['validation']['content_sha256'],
        'network_sha256':canonical_digest({k:source[k] for k in ('nodes','rails','bays','zcus')}),
        'rail_adaptations':[], 'site_mapping':assignment, 'dock_mapping':port_mapping,
        'unused_sites':sorted(set(by_site)-set(assignment.values())),
        'body_orientation_interpretation':'explicit project WIDTH-X/HEIGHT-Y; not recovered source semantics',
        'entry_exit_allocations':allocations,
        'storage_totals':{'stocker':sum(e['rack_capacity'] for e in model['equipment'] if e['kind']=='stocker'),
                          'track_buffer':sum(d['carrier_capacity'] for d in model['docks'] if d['kind']=='track_buffer')},
        'downstream_batch_tails':downstream_batch_tails(manufacturing),
        'topology_product_weights':{p:str(w) for p,w in weights.items()},
        'transition_graph':[{'from':a,'to':b,'weight':str(w)} for (a,b),w in graph.items()],
        'placement':history | {'site_algorithm':SITE_VERSION,'maximum_body_displacement_um':max(abs(s['y_um']-s['source_y_um']) for s in sites if s['id'] in assignment.values())}}
    return bind_validation({'schema_version':1,'model':model,'provenance':provenance},traffic_requirements(manufacturing),VALIDATOR)
