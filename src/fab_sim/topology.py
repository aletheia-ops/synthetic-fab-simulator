"""Compile static traffic resources and check initial full-body poses."""
from bisect import bisect_left
from collections import defaultdict
from .geometry import overlaps


def index(records, name):
    result = {}
    for record in records:
        key = record.get('id')
        if not isinstance(key, str) or not key or key in result:
            raise ValueError(f'{name}: missing/duplicate identity {key!r}')
        result[key] = record
    return result


def strongly_connected(nodes, rails):
    if not nodes: raise ValueError('empty topology')
    forward, reverse = defaultdict(list), defaultdict(list)
    for r in rails.values():
        forward[r['from_node']].append(r['to_node'])
        reverse[r['to_node']].append(r['from_node'])
    for edges in (forward, reverse):
        reached, pending = set(), [min(nodes)]
        while pending:
            node = pending.pop()
            if node in reached: continue
            reached.add(node); pending.extend(edges[node])
        if reached != set(nodes): raise ValueError('topology lacks directed return/service reachability')


def compile_traffic(model):
    nodes = index(model['nodes'], 'nodes'); rails = index(model['rails'], 'rails')
    types = index(model['vehicle_types'], 'vehicle types')
    docks = index(model['docks'], 'docks'); vehicles = index(model['vehicles'], 'vehicles')
    zones = index(model['zcus'], 'ZCUs')
    if not types: raise ValueError('empty vehicle types')
    if any(type(t['gap_um']) is not int or t['gap_um']<0 for t in types.values()):
        raise ValueError('invalid vehicle gap')
    gap = max(t['gap_um'] for t in types.values())
    if type(gap) is not int or gap < 0: raise ValueError('invalid vehicle gap')
    halves = {}
    for t in types.values():
        if type(t['length_um']) is not int or t['length_um'] <= 0:
            raise ValueError('invalid vehicle length')
        halves[t['id']] = (t['length_um']+gap+1)//2
    common = max(halves.values())
    pitch = model['policy']['holding_pitch_um']
    if type(pitch) is not int or pitch < 2*common: raise ValueError('holding pitch below envelope diameter')
    outgoing, incoming = defaultdict(list), defaultdict(list)
    for r in rails.values():
        if r['from_node'] not in nodes or r['to_node'] not in nodes:
            raise ValueError(f"rail {r['id']}: unknown node")
        if type(r['length_um']) is not int or r['length_um'] <= 0:
            raise ValueError(f"rail {r['id']}: invalid length")
        outgoing[r['from_node']].append(r)
        incoming[r['to_node']].append(r)
    for n in nodes.values():
        if n['zcu_type'] not in ('NONE', 'STOP', 'RESET'): raise ValueError('unknown ZCU node type')
        if n['zcu_id'] is not None and n['zcu_id'] not in zones: raise ValueError('unknown ZCU identity')
        if n['zcu_type'] != 'NONE' and n['zcu_id'] is None: raise ValueError('ZCU boundary without identity')
    passages = []
    passage_rails = set()
    for zone_id in sorted(zones):
        starts = [n['id'] for n in nodes.values() if n['zcu_id']==zone_id and n['zcu_type']=='STOP']
        if not starts: raise ValueError(f'ZCU {zone_id}: missing STOP')
        region_nodes, region_rails, done, active = set(), set(), set(), set()

        def visit(node_id):
            n = nodes[node_id]
            if n['zcu_id'] != zone_id: raise ValueError(f'branch leaves ZCU {zone_id} before RESET at {node_id}')
            region_nodes.add(node_id)
            if n['zcu_type']=='RESET': return
            if node_id in active or not outgoing[node_id]:
                raise ValueError(f'ZCU {zone_id}: branch has no first RESET boundary')
            if node_id in done: return
            active.add(node_id)
            for r in outgoing[node_id]:
                region_rails.add(r['id']); visit(r['to_node'])
            active.remove(node_id); done.add(node_id)

        for start in starts: visit(start)
        passages.append({'id': zone_id, 'rail_ids': sorted(region_rails), 'node_ids': sorted(region_nodes)})
        passage_rails.update(region_rails)
    junctions = [{'id':'junction:'+n, 'node_id':n,
                  'incoming_rail_ids':sorted(r['id'] for r in incoming[n]),
                  'outgoing_rail_ids':sorted(r['id'] for r in outgoing[n])}
                 for n in sorted(nodes) if len(incoming[n])!=1 or len(outgoing[n])!=1]
    strongly_connected(nodes, rails)
    dock_offsets = defaultdict(list); coincidence = defaultdict(list)
    for d in docks.values():
        if d['rail_id'] not in rails: raise ValueError(f"dock {d['id']}: unknown rail")
        r = rails[d['rail_id']]; original = d['offset_um']
        if type(original) is not int or not 0 <= original <= r['length_um']:
            raise ValueError(f"dock {d['id']}: offset outside rail")
        if r['length_um'] < 2*common: raise ValueError(f"dock {d['id']}: rail too short for envelope")
        adjusted = max(common, min(original, r['length_um']-common))
        if adjusted != original and not model['policy']['raw_dock_clamp']:
            raise ValueError(f"dock {d['id']}: incompatible endpoint envelope")
        if r['id'] in passage_rails: raise ValueError(f"dock {d['id']}: stopping in passage interior")
        d['source_offset_um'] = original; d['offset_um'] = adjusted
        dock_offsets[r['id']].append(adjusted)
        coincidence[(r['id'],adjusted)].append(d['id'])
    for positions in dock_offsets.values(): positions.sort()

    def service_overlap(rail_id, offset, half):
        positions = dock_offsets[rail_id]
        i = bisect_left(positions, offset)
        return any(abs(x-offset)<common+half for x in positions[max(0,i-1):i+1])

    holdings = []
    for r in sorted(rails.values(), key=lambda r:r['id']):
        if r['id'] in passage_rails: continue
        for k, offset in enumerate(range(common, r['length_um']-common+1, pitch)):
            if service_overlap(r['id'],offset,common): continue
            holdings.append({'id':f"holding:{r['id']}:{k}", 'rail_id':r['id'],
                             'offset_um':offset, 'bay_id':r['bay_id']})
    occupied = defaultdict(list)
    for v in vehicles.values():
        if v['type_id'] not in types or v['rail_id'] not in rails: raise ValueError(f"vehicle {v['id']}: unknown type/rail")
        r = rails[v['rail_id']]; half = halves[v['type_id']]; offset = v['offset_um']
        if type(offset) is not int or offset-half<0 or offset+half>r['length_um']:
            raise ValueError(f"vehicle {v['id']}: endpoint envelope outside rail")
        if r['id'] in passage_rails: raise ValueError(f"vehicle {v['id']}: initial passage occupancy")
        occupied[r['id']].append((offset-half,offset+half,v['id']))
        v['initial_pose_kind'] = 'transient_service' if service_overlap(r['id'],offset,half) else 'holding'
        if v['initial_pose_kind']=='holding':
            # Source poses remain exact even when not on the common holding grid.
            if common <= offset <= r['length_um']-common and not service_overlap(r['id'],offset,common):
                holdings.append({'id':'source-holding:'+v['id'], 'rail_id':r['id'],
                                 'offset_um':offset, 'bay_id':r['bay_id']})
            else: raise ValueError(f"vehicle {v['id']}: invalid common holding envelope")
    for rail_id, intervals in occupied.items():
        ordered = sorted(intervals)
        for a, b in zip(ordered,ordered[1:]):
            if overlaps(a[:2],b[:2]): raise ValueError(f'initial vehicle envelopes overlap on {rail_id}: {a[2]}, {b[2]}')
    # Equal common envelopes: earliest-end greedy packing gives the maximum
    # count of nonoverlapping candidate holding spaces on each original rail.
    by_rail = defaultdict(list)
    for h in holdings: by_rail[h['rail_id']].append(h['offset_um'])
    holding_capacity = 0
    for positions in by_rail.values():
        end = -1
        for offset in sorted(set(positions)):
            if offset-common >= end:
                holding_capacity += 1; end = offset+common
    if holding_capacity<len(vehicles): raise ValueError('insufficient holding capacity for supplied initial fleet')
    model['holding_points'] = sorted(holdings, key=lambda h:h['id'])
    model['traffic'] = {'common_half_um':common, 'global_gap_um':gap,
                        'holding_capacity':holding_capacity,
                        'type_half_um':halves, 'passages':passages, 'junctions':junctions,
                        'coincident_docks':sorted(sorted(ids) for ids in coincidence.values() if len(ids)>1)}
    return model
