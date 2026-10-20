"""Conservative, deterministic recovery of persistent assigned and empty-clearing body-wait cycles.

Repeat amendments require completed-hop progress and a new recovery position.
Twelve free waypoint candidates are examined per member.
Every proposed route keeps its destination and passes the ordinary swept-hop gate.
This does not claim complete global deadlock prevention or full-route clearance.
"""
from copy import deepcopy
from .geometry import swept_intervals

POLICY = 'assigned-body-cycle-progress-guarded-detour-v2'

def _movement(t,r,*,allow_amended=False,require_persistent=True):
    if t.transfers is None:return None
    v=t.vehicles[r['vehicle_id']];m=t.transfers.movements.get(r['id'])
    if (r['status']!='pending' or r['reposition'] or r['cancel_requested'] or r.get('route_recovery') and not allow_amended
            or r['vehicle_id'] in t.hops or not r['remaining'] or not v.get('available',True)
            or t.causes['oht'][r['vehicle_id']] or require_persistent and t.scheduler.now_us<=r['waiting_since_us']
            or not m or m['cancelled'] or m['status'] not in ('requested','moving')):return None
    d=t.transfers.demands.get(m['demand_id']);c=t.transfers.custody
    if (not d or v.get('assignment_id')!=d['id'] or v.get('movement_id')!=r['id']
            or d.get('vehicle_id')!=r['vehicle_id'] or m['vehicle_id']!=r['vehicle_id'] or m['phase'] not in ('pickup','drop')):return None
    phase=m['phase'];origin=d['origin_id'] if phase=='pickup' else v['carrier_slot_id']
    target=v['carrier_slot_id'] if phase=='pickup' else d['destination_id']
    if (d['status']!=('assigned' if phase=='pickup' else 'picked_up')
            or c.locations.get(d['carrier_id'])!=origin
            or c.slots[origin]['carrier_id']!=d['carrier_id'] or c.slots[origin]['reservation']!=d['id']
            or c.slots[target]['carrier_id'] is not None or c.slots[target]['reservation']!=d['id']
            or d.get('rack_slot_id') and c.slots[d['rack_slot_id']]['reservation']!=d['rack_owner']):return None
    if m['target_pose']!=r['target_pose']:return None
    return m,d

def _repeat_progress(r, v):
    """Physical movement, not advancing clock or changed maintenance, permits retry."""
    prior=r.get('route_recovery')
    if not prior:return True
    traveled=prior.get('traveled_seconds')
    if traveled is None or r['cumulative_seconds']<=traveled:return False
    pose={k:v[k] for k in ('rail_id','offset_um')}
    return pose not in prior.get('recovery_poses',[])


def _clearing_edges(t, eligible, claims):
    """Exact first-hop dependencies, rooted in eligible assigned body waits.

    Idle wait evidence also includes rejected destination envelopes. It is not
    an adjacency list: recompute the planner's first endpoint-free selection.
    Empty members are graph evidence only, never candidates for route amendment.
    """
    edges={};selected={}
    frontier=[b['owner'] for r in eligible.values() for b in r['blockers']]
    visited=set(eligible)
    while frontier:
        vid=frontier.pop()
        if vid in visited:continue
        visited.add(vid)
        if not t.manage_idle or vid not in t.idle_wait or vid not in t.vehicles:continue
        v=t.vehicles[vid]
        if (vid in t.active or vid in t.hops or v.get('movement_id') or not t._empty(vid)
                or not v.get('available',True) or t.causes['oht'][vid]):continue
        for hid,seconds in t._idle_destinations(v):
            if not seconds:continue
            if t._blockers(vid,t._envelope(t.holdings[hid],v['type_id']),[],claim_index=claims):continue
            path,sweep,resources=t._idle_plan(v,hid)
            # A fault is not a stationary body cycle and cannot be bypassed.
            parts,_,_=t._next_hop({'remaining':path['sublinks'],'target_pose':t.holdings[hid]})
            if any(t.causes['rail'][part['rail_id']] for part in parts):break
            blocked=t._blockers(vid,sweep,resources,claim_index=claims)
            if blocked and all(b['kind']=='rail' for b in blocked):
                edges[vid]=tuple(b['owner'] for b in blocked)
                selected[vid]=hid
                frontier.extend(edges[vid])
            break
    return edges,selected

def _rejection_permission(t, vid, record, claims):
    if record['kind']=='fault':
        return tuple((rid,tuple(sorted(t.causes['rail'][rid]))) for rid in record['rails'])
    return t._hop_permission_key(vid,record['sweep'],record['resources'],record['rails'],claims)


def _remember_rejection(t, key, record, claims):
    # Derived negative observations only; never cache a permission to move.
    record['permission']=_rejection_permission(t,key[0],record,claims)
    cache=t._recovery_waypoint_rejections
    cache[key]=record;cache.move_to_end(key)
    if len(cache)>256:cache.popitem(last=False)


def strongly_connected_cycles(graph):
    """Sorted SCCs with a cycle (size > 1 or a self edge) of an adjacency map."""
    # Iterative Tarjan visits each edge once, without a reverse graph or
    # adjacency sorting. Only the externally visible SCCs are canonicalized.
    indices={};low={};active=set();members=[];cycles=[]
    for root in graph:
        if root in indices:continue
        indices[root]=low[root]=len(indices);active.add(root);members.append(root)
        frames=[(root,iter(graph[root]))]
        while frames:
            node,children=frames[-1];child=next(children,None)
            if child is not None:
                if child not in indices:
                    indices[child]=low[child]=len(indices)
                    active.add(child);members.append(child)
                    frames.append((child,iter(graph.get(child,()))))
                elif child in active:low[node]=min(low[node],indices[child])
                continue
            frames.pop()
            if low[node]==indices[node]:
                component=[]
                while True:
                    member=members.pop();active.remove(member);component.append(member)
                    if member==node:break
                if len(component)>1 or node in graph.get(node,()):
                    cycles.append(sorted(component))
            if frames:
                parent=frames[-1][0];low[parent]=min(low[parent],low[node])
    return sorted(cycles)



def recover_cycle(t):
    probes=getattr(t.transfers,'probes',None) or getattr(t,'probes',None)
    # _movement only reads state; test the cheap rail-blocker condition first.
    eligible={vid:r for vid,rid in t.active.items() for r in (t.requests[rid],)
        if r['blockers'] and all(b['kind']=='rail' for b in r['blockers']) and _movement(t,r,allow_amended=True) is not None}
    # Clearing edges are rooted only in eligible members: with none, the graph
    # is empty and no SCC can exist, so skip the claim index and graph work.
    if not eligible:
        # Same derived cache entry and counters as the general path's empty graph.
        if t._recovery_cycle_cache is None or t._recovery_cycle_cache[0]!=():
            if probes:probes.count_work('traffic_cycle_scc_searches')
            t._recovery_cycle_cache=((),())
        elif probes:probes.count_work('traffic_cycle_scc_reuses')
        return False
    claims=t._claim_index()
    clearing,selected=_clearing_edges(t,eligible,claims)
    edges={vid:tuple(b['owner'] for b in r['blockers']) for vid,r in eligible.items()}
    edges.update(clearing)
    graph={vid:tuple(owner for owner in owners if owner in edges)
        for vid,owners in sorted(edges.items())}
    # Recheck live eligibility above on every deadline, including silent owner
    # mutations. SCCs depend only on this exact eligible adjacency, independently
    # of the broader physical route-search signature.
    adjacency=tuple(graph.items())
    cached=t._recovery_cycle_cache
    if cached is None or cached[0]!=adjacency:
        if probes:probes.count_work('traffic_cycle_scc_searches')
        cycles=strongly_connected_cycles(graph)
        t._recovery_cycle_cache=(adjacency,tuple(tuple(cycle) for cycle in cycles))
    else:
        if probes:probes.count_work('traffic_cycle_scc_reuses')
        # Route evidence owns its lists; it must not alias the derived cache.
        cycles=[list(cycle) for cycle in cached[1]]
    if not cycles:return False
    # Observation/derived memo only. No retry merely because maintenance advances time.
    signature=repr((graph,t.occupancy,
        {v:h['claims'] for v,h in t.hops.items()},t.targets,t.resources,t.causes,
        [(v,r['id'],r['remaining'],r['cumulative_seconds'],r.get('route_recovery')) for v,r in eligible.items()],selected))
    if t._recovery_signature==signature:return False
    t._recovery_signature=signature
    if probes:probes.count_work('traffic_cycle_recovery_searches')
    for cycle in cycles:
        for vid in sorted((v for v in cycle if v in eligible),key=lambda v:(eligible[v]['requested_at_us'],eligible[v]['id'],v)):
            r=eligible[vid];v=t.vehicles[vid];free=0
            if not _repeat_progress(r,v):continue
            route_key=t._hop_route_key(r,v)
            for hid,seconds in t._idle_destinations(v):
                if not seconds or t.holdings[hid]==r['target_pose']:continue
                waypoint=t.holdings[hid]
                if t._blockers(vid,t._envelope(waypoint,v['type_id']),[],claim_index=claims):continue
                free+=1
                if free>12:break
                # Keep fresh target occupancy/ranking and live member eligibility
                # above. Unrelated fleet changes must not rebuild a rejected path.
                key=(vid,r['id'],route_key,hid)
                cache=t._recovery_waypoint_rejections;cached=cache.get(key)
                if cached is not None:
                    if cached['permission']==_rejection_permission(t,vid,cached,claims):
                        cache.move_to_end(key)
                        if probes:probes.count_work('traffic_cycle_waypoint_rejections_reused')
                        continue
                    cache.pop(key)
                if probes:probes.count_work('traffic_cycle_waypoints_considered')
                try:
                    router=t._router(v['type_id'])
                    outward=router.path(v,waypoint);onward=router.path(waypoint,r['target_pose'])
                except ValueError:continue
                parts=outward['sublinks']+onward['sublinks']
                rails=tuple(sorted({p['rail_id'] for p in parts}))
                if any(t.causes['rail'][rid] for rid in rails):
                    _remember_rejection(t,key,{'kind':'fault','rails':rails},claims)
                    continue
                first,_,target=t._next_hop({'remaining':parts,'target_pose':r['target_pose']})
                if not first:continue
                t._safe(target,v['type_id'])
                if target in r.get('route_recovery',{}).get('recovery_poses',[]):continue
                if parts==r['remaining']:continue
                sweep=swept_intervals(t.model,first,t.halves[v['type_id']])
                resources=t._resources(first,sweep)
                if t._blockers(vid,sweep,resources,claim_index=claims):
                    _remember_rejection(t,key,{'kind':'hop','rails':rails,
                        'sweep':sweep,'resources':resources},claims)
                    continue
                bound=_movement(t,r,allow_amended=True)
                if bound is None:continue
                evidence={'policy':POLICY,'request_id':r['id'],'vehicle_id':vid,
                    'cycle_members':cycle,'waypoint_id':hid,'amended_at_us':t.scheduler.now_us,
                    'destination_pose_id':r['destination_pose_id'],'traveled_seconds':r['cumulative_seconds'],
                    'recovery_poses':deepcopy(r.get('route_recovery',{}).get('recovery_poses',[]))+[{k:v[k] for k in ('rail_id','offset_um')}],
                    'previous_remaining_seconds':t._duration(r['remaining'],v['type_id']),
                    'new_remaining_seconds':t._duration(parts,v['type_id'])}
                amend_route(t,r,bound,parts,evidence)
                if probes:probes.count_work('traffic_cycle_route_amendments')
                return True
    return False


def amend_route(t, r, bound, parts, evidence):
    """Replace a movement's remaining route by parts (same destination), keeping
    the traversed prefix; record evidence on the request, movement and demand."""
    m,d=bound;v=t.vehicles[r['vehicle_id']]
    # Remaining is a suffix of the original selected movement path.
    # Keep the already traversed prefix, including a partially used rail.
    full=m['path']['sublinks'];n=len(full)-len(r['remaining'])
    if n<0:raise RuntimeError('route recovery lost movement prefix')
    prefix=deepcopy(full[:n]);old=r['remaining'][0];original=full[n]
    if original['rail_id']!=old['rail_id'] or original['end_um']!=old['end_um']:
        raise RuntimeError('route recovery suffix ownership mismatch')
    if original['start_um']<old['start_um']:
        prefix.append({**original,'end_um':old['start_um']})
    if t._duration(prefix,v['type_id'])!=r['cumulative_seconds']:
        raise RuntimeError('route recovery elapsed path mismatch')
    t._validate_path(parts,v,r['target_pose'])
    total=prefix+deepcopy(parts)
    path={**m['path'],'sublinks':total,'seconds':t._duration(total,v['type_id'])}
    r['remaining']=deepcopy(parts);r['route_recovery']=evidence
    m['path']=path;m['route_recovery']=deepcopy(evidence)
    d['pickup_path' if m['phase']=='pickup' else 'loaded_path']=deepcopy(path)
    t.emit('traffic_route_amended',{**deepcopy(evidence),'path':deepcopy(path)})
    t.dirty()
