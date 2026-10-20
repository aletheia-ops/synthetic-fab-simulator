"""Directed rational free-flow routing, independent of traffic and handling."""
from fractions import Fraction
from functools import lru_cache
from heapq import heappop, heappush


def effective_speed(rail, vehicle_type, policy):
    # Key by current values, not mutable rail/type identities. This is pure
    # rational arithmetic; traffic permission and fault state stay elsewhere.
    curve=rail['curve']
    return _speed_for_values(vehicle_type['speed_um_s'],curve,
        policy['curve_cap_um_s'] if curve else None,
        rail.get('absolute_cap_um_s'),rail['speed_multiplier'])


@lru_cache(maxsize=1024,typed=True)
def _speed_for_values(vehicle_speed,curve,curve_cap,absolute_cap,multiplier_value):
    speed = Fraction(vehicle_speed)
    if curve: speed = min(speed, Fraction(curve_cap))
    if absolute_cap is not None:
        speed = min(speed, Fraction(absolute_cap))
    multiplier = Fraction(multiplier_value)
    if not 0 < multiplier <= 1 or speed <= 0: raise ValueError('invalid rail speed')
    return speed * multiplier


def speed_parts(rail, low, high, vehicle_type, policy):
    sections = rail.get('speed_sections')
    if sections is None:
        sections = [{'start_um':0,'end_um':rail['length_um'],'curve':rail['curve']}]
    boundary=0; parts=[]
    for section in sections:
        if (type(section['start_um']) is not int or type(section['end_um']) is not int
                or section['start_um']!=boundary or section['end_um']<=boundary
                or type(section['curve']) is not bool): raise ValueError('invalid speed section partition')
        boundary=section['end_um']
        start,end=max(low,section['start_um']),min(high,section['end_um'])
        if start<end:
            parts.append((start,end,effective_speed({**rail,'curve':section['curve']},vehicle_type,policy)))
    if boundary!=rail['length_um']: raise ValueError('speed sections must cover original rail')
    return parts


def travel_seconds(rail, low, high, vehicle_type, policy):
    if not 0<=low<=high<=rail['length_um']: raise ValueError('invalid travel offsets')
    sections=rail.get('speed_sections')
    if sections is not None:
        boundary=0;signature=[]
        for section in sections:
            if (type(section['start_um']) is not int or type(section['end_um']) is not int
                    or section['start_um']!=boundary or section['end_um']<=boundary
                    or type(section['curve']) is not bool):raise ValueError('invalid speed section partition')
            boundary=section['end_um']
            signature.append((section['start_um'],section['end_um'],section['curve']))
        if boundary!=rail['length_um']:raise ValueError('speed sections must cover original rail')
        signature=tuple(signature)
    else:signature=None
    # Validate types before key lookup: False equals 0 but is not a valid
    # boundary. Value keys observe edits and never retain live model objects.
    return _travel_for_values(low,high,rail['length_um'],rail['curve'],
        rail['speed_multiplier'],rail.get('absolute_cap_um_s'),
        vehicle_type['speed_um_s'],policy.get('curve_cap_um_s',_MISSING),signature)


_MISSING=object()


class DirectedReachability:
    """Offset-aware directed connectivity without shortest-time path searches."""
    def __init__(self, model):
        from .topology import index, strongly_connected
        self.rails=index(model['rails'],'rails');self.nodes=index(model['nodes'],'nodes')
        self.outgoing={};self.rows={};self.connected=False
        for rail in self.rails.values():
            # assess() also diagnoses deliberately disconnected raw fixtures;
            # prepared-model endpoint integrity is checked by compile_traffic.
            for endpoint in (rail['from_node'],rail['to_node']):self.nodes.setdefault(endpoint,{'id':endpoint})
            self.outgoing.setdefault(rail['from_node'],[]).append(rail['to_node'])
        try:strongly_connected(self.nodes,self.rails);self.connected=True
        except ValueError as error:
            if str(error)!='topology lacks directed return/service reachability':raise

    def reachable(self, start, end):
        for pose in (start,end):
            if pose['rail_id'] not in self.rails:raise ValueError('unknown pose rail')
            rail=self.rails[pose['rail_id']];offset=pose['offset_um']
            if type(offset) is not int or not 0<=offset<=rail['length_um']:raise ValueError('invalid rail offset')
        if self.connected:return True
        a,b=self.rails[start['rail_id']],self.rails[end['rail_id']]
        if a['id']==b['id'] and end['offset_um']>=start['offset_um']:return True
        node=a['to_node']
        if node not in self.rows:
            reached=set();pending=[node]
            while pending:
                current=pending.pop()
                if current in reached:continue
                reached.add(current);pending.extend(self.outgoing.get(current,()))
            self.rows[node]=reached
        return b['from_node'] in self.rows[node]


@lru_cache(maxsize=16384,typed=True)
def _travel_for_values(low,high,length,curve,multiplier,absolute_cap,vehicle_speed,curve_cap,sections):
    rail={'length_um':length,'curve':curve,'speed_multiplier':multiplier}
    if absolute_cap is not None:rail['absolute_cap_um_s']=absolute_cap
    if sections is not None:
        rail['speed_sections']=[{'start_um':s[0],'end_um':s[1],'curve':s[2]} for s in sections]
    policy={} if curve_cap is _MISSING else {'curve_cap_um_s':curve_cap}
    return sum((Fraction(end-start)/speed for start,end,speed in
        speed_parts(rail,low,high,{'speed_um_s':vehicle_speed},policy)),Fraction(0))


def free_flow_costs(topology, start, destinations, vehicle_type_id):
    """Rank many poses with one directed shortest-path search; no path mutation."""
    model=topology.get('model',topology)
    rails={r['id']:r for r in model['rails']};types={v['id']:v for v in model['vehicle_types']}
    vehicle_type=types[vehicle_type_id];origin=rails[start['rail_id']];policy=model['policy']
    outgoing={}
    for rail in rails.values():outgoing.setdefault(rail['from_node'],[]).append(rail)
    def cost(rail,low,high):return travel_seconds(rail,low,high,vehicle_type,policy)
    queue=[(cost(origin,start['offset_um'],origin['length_um']),origin['to_node'])];best={}
    while queue:
        seconds,node=heappop(queue)
        if node in best and best[node]<=seconds:continue
        best[node]=seconds
        for rail in outgoing.get(node,()):
            heappush(queue,(seconds+cost(rail,0,rail['length_um']),rail['to_node']))
    result={}
    for pid,pose in destinations.items():
        rail=rails[pose['rail_id']];candidates=[]
        if rail['id']==origin['id'] and pose['offset_um']>=start['offset_um']:
            candidates.append(cost(rail,start['offset_um'],pose['offset_um']))
        if rail['from_node'] in best:candidates.append(best[rail['from_node']]+cost(rail,0,pose['offset_um']))
        if candidates:result[pid]=min(candidates)
    return result


def free_flow_costs_to(topology, origins, end, vehicle_type_id):
    """Rank many origins to one pose using exact reverse directed costs."""
    model=topology.get('model',topology)
    rails={r['id']:r for r in model['rails']};types={v['id']:v for v in model['vehicle_types']}
    vehicle_type=types[vehicle_type_id];policy=model['policy']
    for pose in (end,*origins.values()):
        if pose['rail_id'] not in rails:raise ValueError('unknown pose rail')
        offset=pose['offset_um']
        if type(offset) is not int or not 0<=offset<=rails[pose['rail_id']]['length_um']:
            raise ValueError('invalid rail offset')
    target=rails[end['rail_id']];incoming={}
    for rail in rails.values():incoming.setdefault(rail['to_node'],[]).append(rail)
    def cost(rail,low,high):return travel_seconds(rail,low,high,vehicle_type,policy)
    queue=[(cost(target,0,end['offset_um']),target['from_node'])];best={}
    while queue:
        seconds,node=heappop(queue)
        if node in best and best[node]<=seconds:continue
        best[node]=seconds
        for rail in incoming.get(node,()):
            heappush(queue,(seconds+cost(rail,0,rail['length_um']),rail['from_node']))
    result={}
    for oid,pose in origins.items():
        rail=rails[pose['rail_id']];candidates=[]
        if rail['id']==target['id'] and end['offset_um']>=pose['offset_um']:
            candidates.append(cost(rail,pose['offset_um'],end['offset_um']))
        if rail['to_node'] in best:
            candidates.append(cost(rail,pose['offset_um'],rail['length_um'])+best[rail['to_node']])
        if candidates:result[oid]=min(candidates)
    return result


def free_flow_path(topology, start, end, vehicle_type_id):
    model = topology.get('model', topology)
    rails = {r['id']: r for r in model['rails']}
    types = {v['id']: v for v in model['vehicle_types']}
    vehicle_type = types[vehicle_type_id]
    for pose in (start, end):
        if pose['rail_id'] not in rails: raise ValueError('unknown pose rail')
        offset = pose['offset_um']
        if type(offset) is not int or not 0 <= offset <= rails[pose['rail_id']]['length_um']:
            raise ValueError('invalid rail offset')
    origin, target = rails[start['rail_id']], rails[end['rail_id']]
    outgoing = {}
    for r in rails.values(): outgoing.setdefault(r['from_node'], []).append(r)

    def segment(rail, low, high):
        pieces=speed_parts(rail,low,high,vehicle_type,model['policy'])
        parts=tuple((f"{rail['id']}@{a}:{b}",rail['id'],a,b) for a,b,speed in pieces)
        return parts,sum((Fraction(b-a)/speed for a,b,speed in pieces),Fraction(0))

    candidates = []
    if origin['id'] == target['id'] and end['offset_um'] >= start['offset_um']:
        parts, cost = segment(origin, start['offset_um'], end['offset_um'])
        candidates.append((cost, tuple(p[0] for p in parts), parts))
    parts, cost = segment(origin, start['offset_um'], origin['length_um'])
    queue = [(cost, tuple(p[0] for p in parts), origin['to_node'], parts)]
    best = {}
    while queue:
        cost, ids, node, parts = heappop(queue)
        if node in best and best[node] <= (cost, ids): continue
        best[node] = (cost, ids)
        if candidates and cost > min(candidates)[0]: continue
        if node == target['from_node']:
            tail, extra = segment(target, 0, end['offset_um'])
            candidates.append((cost+extra, ids+tuple(p[0] for p in tail), parts+tail))
        for r in outgoing.get(node, ()):
            tail, extra = segment(r, 0, r['length_um'])
            heappush(queue, (cost+extra, ids+tuple(p[0] for p in tail), r['to_node'], parts+tail))
    if not candidates: raise ValueError('unreachable directed destination')
    seconds, _, parts = min(candidates)
    return {'sublinks': [{'id': p[0], 'rail_id': p[1], 'start_um': p[2], 'end_um': p[3]} for p in parts],
            'seconds': seconds}
