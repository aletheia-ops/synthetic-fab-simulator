"""Event-driven safe hops on prepared parent-rail intervals and passages."""
from .native_actions import native_action

from bisect import bisect_right
from collections import OrderedDict
from fractions import Fraction
import json
from .geometry import overlaps, swept_intervals
from .routing import free_flow_path, speed_parts, travel_seconds
from .routing_index import FreeFlowCosts
from .derived_cache import DerivedCache
from .service import Timer
from .timebase import to_ticks
from .topology import index


_END=object()


class _LazyRanking:
    """Memoized prefix of one exact geometry-only idle ranking.

    Callers stop at the first free holding, so materializing every holding
    (with exact rational additions) on each cache fill is unnecessary. Order and
    values are those of the underlying ranking; equality compares the whole
    sequence, so tuple comparisons keep working.
    """
    __slots__=('_items','_source')
    __hash__=None
    def __init__(self,source):self._items=[];self._source=iter(source)
    def __iter__(self):
        items=self._items;i=0
        while True:
            if i<len(items):
                yield items[i];i+=1;continue
            if self._source is None:return
            item=next(self._source,_END)
            if item is _END:self._source=None;return
            items.append(item)
    def __eq__(self,other):
        return tuple(self)==(tuple(other) if isinstance(other,_LazyRanking) else other)
    def __len__(self):return sum(1 for _ in self)


class Traffic:
    """Safe-hop permission, with optional transfer binding and empty idle clearing.

    Whole swept claims remain owned to a prepared safe endpoint. Moving pose is
    derived from exact route speed and active elapsed ticks, without polling.
    """
    def __init__(self, topology, scheduler, vehicles, emit, *, transfers=None, manage_idle=None,geometry_cache=None,hop_lookahead_um=0,bay_entry_divert=False,queue_divert=False):
        self.model=topology.get('model',topology);self.scheduler=scheduler;self.vehicles=vehicles;self.emit=emit
        self.rails=index(self.model['rails'],'traffic rails');self.types=index(self.model['vehicle_types'],'vehicle types')
        self.holdings=index(self.model['holding_points'],'holding points');self.docks=index(self.model['docks'],'docks')
        self.poses={**self.holdings,**self.docks};self.halves=self.model['traffic']['type_half_um']
        # Prepared dock geometry is immutable; faults affect availability only.
        self._dock_offsets_by_rail={}
        for dock in self.docks.values():
            self._dock_offsets_by_rail.setdefault(dock['rail_id'],[]).append(dock['offset_um'])
        self._dock_offsets_by_rail={rid:tuple(sorted(offsets))
            for rid,offsets in self._dock_offsets_by_rail.items()}
        self.passages=self.model['traffic']['passages'];self.junctions={r['node_id']:r['id'] for r in self.model['traffic']['junctions']}
        self._passage_index=None
        self._claims_version=0;self._claim_cache=None;self._service_set=None
        # Idle-plan memo inputs: per-rail claim versions while the index is
        # maintained; the epoch invalidates every memo when it is not.
        self._rail_versions={};self._claim_epoch=0;self._idle_memo={}
        # Free-run hops (declared project policy, docs/settings.md): 0 keeps
        # the original one-holding-pitch hop.
        if type(hop_lookahead_um) is not int or hop_lookahead_um<0:raise ValueError('invalid hop lookahead')
        self.hop_lookahead_um=hop_lookahead_um
        # Bay-entry divert (declared project policy, docs/settings.md): a
        # vehicle on an interbay whose next hop turns into an intrabay and is
        # blocked by stopped vehicles continues along the interbay instead.
        if type(bay_entry_divert) is not bool:raise ValueError('invalid bay entry divert')
        self.bay_entry_divert=bay_entry_divert
        # Persistent-queue divert (declared project policy, docs/settings.md):
        # see traffic_queue_divert.
        if type(queue_divert) is not bool:raise ValueError('invalid queue divert')
        self.queue_divert=queue_divert;self._queue_divert_graph=None
        # Congestion reroute (declared project policy): queue diverts plan on
        # congestion-penalized costs (requires transfers.congestion_routing_s).
        kinds={b['id']:b.get('type') for b in self.model.get('bays',())}
        self._rail_bay_kind={rid:kinds.get(r.get('bay_id')) for rid,r in self.rails.items()}
        self.holding_by_rail={};self.holding_offsets={}
        for h in self.holdings.values():self.holding_by_rail.setdefault(h['rail_id'],[]).append((h['offset_um'],h['id']))
        for rid,hs in self.holding_by_rail.items():
            hs.sort();self.holding_offsets[rid]=[p[0] for p in hs]
        self.history_archive=None;self._history_lookup=None;self._idle_requests={}
        self.requests={};self.active={};self.hops={};self.targets={};self.resources={};self.occupancy={}
        self.transfers=transfers;self.manage_idle=bool(transfers) if manage_idle is None else manage_idle
        self.idle_wait={};self.idle_sequence=0
        self._idle_rankings=OrderedDict()
        self._idle_routers={}
        self._geometry_cache=geometry_cache if geometry_cache is not None else DerivedCache(512*1024*1024)
        self._idle_target_key=FreeFlowCosts.target_key(self.holdings)
        self._idle_plans=OrderedDict()
        self._recovery_signature=None
        self._recovery_cycle_cache=None
        self._recovery_waypoint_rejections=OrderedDict()
        self._denied_hops={}
        self.causes={'oht':{vid:set() for vid in vehicles},'rail':{rid:set() for rid in self.rails},
                     'port':{pid:set() for pid in self.docks} if transfers is not None else {}}
        self.base_available={}
        if transfers is not None:
            if transfers.scheduler is not scheduler or transfers.vehicles is not vehicles:
                raise ValueError('traffic and transfers must share scheduler and live vehicles')
            if getattr(transfers,'traffic',None) is not None:raise ValueError('transfers already bound to traffic')
            transfers.traffic=self
        for vid,v in sorted(vehicles.items()):
            self._safe(v,v['type_id']);v.setdefault('movement_id',None)
            occupied=self._envelope(v,v['type_id'])
            if self._blockers(vid,occupied,[]):raise ValueError('initial traffic occupancy overlaps')
            self.occupancy[vid]=occupied
        scheduler.register_pass('traffic',self.dispatch)
        scheduler.add_deadline_check(self._recover_cycles)
        if self.manage_idle or transfers is not None:self.dirty()

    def dirty(self):self.scheduler.mark_dirty('traffic')

    def claims_changed(self):
        """Record claims changed outside this class (tests, external tools)."""
        self._claims_version+=1;self._claim_cache=None;self._claim_epoch+=1;self._service_set=None;self.dirty()

    def resource_changed(self,change):
        # Swept intervals and shared passage ranks still have a broad frontier.
        self.dirty()

    def _safe(self, pose, type_id):
        rid=pose['rail_id'];offset=pose['offset_um'];half=self.halves[type_id]
        if (rid not in self.rails or type(offset) is not int or offset-half<0
                or offset+half>self.rails[rid]['length_um']
                or rid in self._passages_by_rail()):
            raise ValueError('traffic endpoint lacks safe full-body stopping space')

    def _envelope(self, pose, type_id):
        half=self.halves[type_id];return {pose['rail_id']:[(pose['offset_um']-half,pose['offset_um']+half)]}

    def _duration(self, parts, type_id):
        return sum((travel_seconds(self.rails[p['rail_id']],p['start_um'],p['end_um'],
                                   self.types[type_id],self.model['policy']) for p in parts),Fraction(0))

    def _validate_path(self, parts, origin, target):
        previous=dict(origin)
        for p in parts:
            rail=self.rails[p['rail_id']]
            if (type(p['start_um']) is not int or type(p['end_um']) is not int
                    or not 0<=p['start_um']<p['end_um']<=rail['length_um']):
                raise ValueError('invalid directed traffic sublink')
            if p['rail_id']==previous['rail_id']:
                connected=(p['start_um']==previous['offset_um'] or
                    previous['offset_um']==rail['length_um'] and p['start_um']==0
                    and rail['to_node']==rail['from_node'])
            else:
                prior=self.rails[previous['rail_id']]
                connected=(previous['offset_um']==prior['length_um'] and p['start_um']==0
                           and prior['to_node']==rail['from_node'])
            if not connected:raise ValueError('disconnected traffic path')
            previous={'rail_id':p['rail_id'],'offset_um':p['end_um']}
        if any(previous[k]!=target[k] for k in ('rail_id','offset_um')):raise ValueError('traffic path misses endpoint')

    def request(self, vehicle_id: str, destination_pose_id: str, request_id: str, *,
                path=None, requested_at_us=None, reposition=False, on_start=None,
                on_complete=None, on_cancel=None, _sequenced=False) -> None:
        on_start=native_action(self,'noop','',None,lambda:None) if on_start is None else on_start
        on_complete=native_action(self,'noop','',None,lambda:None) if on_complete is None else on_complete
        on_cancel=native_action(self,'noop','',None,lambda:None) if on_cancel is None else on_cancel
        if (not isinstance(request_id,str) or not request_id or request_id in self.requests
                or vehicle_id in self.active or not all(callable(c) for c in (on_start,on_complete,on_cancel))):
            raise ValueError('invalid/duplicate traffic request')
        v=self.vehicles[vehicle_id];target=self.poses[destination_pose_id]
        if v.get('movement_id') not in (None,request_id):raise ValueError('vehicle already owns another movement')
        if reposition and (destination_pose_id not in self.holdings or v.get('assignment_id')):
            raise ValueError('reposition requires an unassigned vehicle and holding target')
        self._safe(target,v['type_id'])
        origin={k:v[k] for k in ('rail_id','offset_um')}
        selected=path if path is not None else self._router(v['type_id']).path(origin,target)
        parts=[dict(p) for p in selected['sublinks']];self._validate_path(parts,origin,target)
        seconds=self._duration(parts,v['type_id'])
        if selected['seconds']!=seconds:raise ValueError('traffic path cost differs from effective speed policy')
        age=self.scheduler.now_us if requested_at_us is None else requested_at_us
        if type(age) is not int or not 0<=age<=self.scheduler.now_us:raise ValueError('invalid traffic request age')
        r={'id':request_id,'vehicle_id':vehicle_id,'destination_pose_id':destination_pose_id,
           'target_pose':{k:target[k] for k in ('rail_id','offset_um')},'remaining':parts,
           'requested_at_us':age,'status':'pending','reposition':bool(reposition),
           'cancel_requested':False,'cumulative_seconds':Fraction(0),'boundary_us':0,
           'waiting_since_us':self.scheduler.now_us,'wait_us':0,'blockers':[],
           'started_at_us':None,'completed_at_us':None,
           'on_start':on_start,'on_complete':on_complete,'on_cancel':on_cancel}
        self.requests[request_id]=r;self.active[vehicle_id]=request_id;v['movement_id']=request_id
        if reposition:
            self._idle_requests[request_id]=r
            claim=self._envelope(target,v['type_id'])
            if not self._blockers(vehicle_id,claim,[],claim_index=self._claim_index()):self.targets[vehicle_id]=claim;self._set_claims(2,vehicle_id,claim)
        self.dirty()

    def _archive_lookup(self):
        if self._history_lookup is None:
            actions=getattr(self,'actions',None)
            lookup=getattr(actions,'history_lookup',None) if actions else None
            if lookup is None:
                from .history_lookup import HistoryLookup
                lookup=HistoryLookup()
                if actions:actions.history_lookup=lookup
            self._history_lookup=lookup
        return self._history_lookup

    def _next_hop(self, r):
        taken=[];remaining=r['remaining']
        for i,p in enumerate(remaining):
            hs=self.holding_by_rail.get(p['rail_id'],[])
            begin=bisect_right(self.holding_offsets.get(p['rail_id'],[]),p['start_um'])
            candidate=hs[begin] if begin<len(hs) and hs[begin][0]<=p['end_um'] else None
            if candidate:
                offset,hid=candidate;taken.append({**p,'end_um':offset})
                tail=([{**p,'start_um':offset}] if offset<p['end_um'] else [])+remaining[i+1:]
                return taken,tail,{k:self.holdings[hid][k] for k in ('rail_id','offset_um')}
            taken.append(p)
        return taken,[],dict(r['target_pose'])

    @staticmethod
    def _join_parts(parts, more):
        # Rejoin a routing sublink that holding boundaries split, so the hop
        # sweeps and times exactly like one traversal of that sublink.
        joined=list(parts)
        for part in more:
            last=joined[-1] if joined else None
            if (last is not None and last.get('id')==part.get('id') and last['rail_id']==part['rail_id']
                    and last['end_um']==part['start_um']):
                joined[-1]={**last,'end_um':part['end_um']}
            else:joined.append(part)
        return joined

    def _extend_hop(self, r, v, parts, tail, target, claims, resources, claim_index):
        """Free-run hop: continue through later holding centres while clear.

        Extends only while the whole swept interval is unclaimed by others, no
        rail on it is faulted, no junction/ZCU resource beyond the first hop's
        is needed and the extra distance stays within hop_lookahead_um. The
        result is a hop the original rules would have produced as a chain of
        unobstructed one-pitch hops, with the same cumulative timing; its
        claims are simply held until the farther arrival.
        """
        half=self.halves[v['type_id']];base=set(resources);vid=r['vehicle_id']
        if self._blockers(vid,claims,resources,claim_index=claim_index):return parts,tail,target,claims,resources
        if any(self.causes['rail'][p['rail_id']] for p in parts):return parts,tail,target,claims,resources
        travelled=0
        while tail:
            more,rest,stop=self._next_hop({'remaining':tail,'target_pose':r['target_pose']})
            extra=sum(p['end_um']-p['start_um'] for p in more)
            if not more or travelled+extra>self.hop_lookahead_um:break
            try:self._safe(stop,v['type_id'])
            except ValueError:break
            joined=self._join_parts(parts,more)
            swept=swept_intervals(self.model,joined,half);needed=self._resources(joined,swept)
            if (set(needed)!=base or any(self.causes['rail'][p['rail_id']] for p in more)
                    or self._blockers(vid,swept,needed,claim_index=claim_index)):break
            parts,tail,target,claims,resources=joined,rest,stop,swept,needed;travelled+=extra
        return parts,tail,target,claims,resources

    def _divert_at_bay_entry(self, r, v, parts, blocked, claim_index):
        """Keep the interbay moving: re-route a vehicle that would wait on an
        interbay to turn into an intrabay held by stopped vehicles.

        Applies to assigned movements only, when every blocker is a rail claim
        of a vehicle that is not moving. The new route reaches an interbay
        holding without entering any intrabay, then continues to the same
        destination; its first hop must be clear. Returns True if amended."""
        kinds=self._rail_bay_kind
        if kinds.get(v['rail_id'])!='INTERBAY':return False
        if not any(kinds.get(p['rail_id'])=='INTRABAY' for p in parts):return False
        # Only behind a jam: every blocker is itself waiting on a blocked hop
        # (not loading at a port, parked or moving).
        def waiting(owner):
            other=self.requests.get(self.active.get(owner))
            return (owner not in self.hops and other is not None and other['status']=='pending'
                    and bool(other['blockers']))
        if not all(b['kind']=='rail' and waiting(b['owner']) for b in blocked):return False
        # Bounded: a movement diverts at most a few times, so routes stay finite.
        if len(r.get('route_recovery',{}).get('recovery_poses',[]))>=4:return False
        from .traffic_cycle_recovery import _movement,amend_route
        bound=_movement(self,r,allow_amended=True,require_persistent=False)
        if bound is None:return False
        vid=r['vehicle_id'];router=self._router(v['type_id']);tried=0
        for hid,seconds in self._idle_destinations(v):
            if not seconds:continue
            waypoint=self.holdings[hid]
            if kinds.get(waypoint['rail_id'])!='INTERBAY':continue
            if self._blockers(vid,self._envelope(waypoint,v['type_id']),[],claim_index=claim_index):continue
            tried+=1
            if tried>12:break
            try:outward=router.path(v,waypoint);onward=router.path(waypoint,r['target_pose'])
            except ValueError:continue
            if any(kinds.get(p['rail_id'])!='INTERBAY' for p in outward['sublinks']):continue
            new=outward['sublinks']+onward['sublinks']
            if new==r['remaining'] or any(self.causes['rail'][p['rail_id']] for p in new):continue
            first,_,target=self._next_hop({'remaining':new,'target_pose':r['target_pose']})
            if not first:continue
            try:self._safe(target,v['type_id'])
            except ValueError:continue
            sweep=swept_intervals(self.model,first,self.halves[v['type_id']])
            if self._blockers(vid,sweep,self._resources(first,sweep),claim_index=claim_index):continue
            evidence={'policy':'bay-entry-divert-v1','request_id':r['id'],'vehicle_id':vid,
                'blocked_by':[dict(b) for b in blocked],'waypoint_id':hid,'amended_at_us':self.scheduler.now_us,
                'destination_pose_id':r['destination_pose_id'],'traveled_seconds':r['cumulative_seconds'],
                'recovery_poses':[dict(p) for p in r.get('route_recovery',{}).get('recovery_poses',[])]+[{k:v[k] for k in ('rail_id','offset_um')}],
                'previous_remaining_seconds':self._duration(r['remaining'],v['type_id']),
                'new_remaining_seconds':self._duration(new,v['type_id'])}
            amend_route(self,r,bound,new,evidence)
            return True
        return False

    def _guarded_stop(self, r, parts, claim_index):
        """Positive assigned advance before the first body claim on this rail.

        This splits the existing route; it never selects a new destination or
        bypasses custody. Reserved targets and moving sweeps count as bodies.
        The dispatcher rechecks the ordinary full sweep, resources and faults.
        """
        from .traffic_cycle_recovery import _movement
        if _movement(self,r,allow_amended=True,require_persistent=False) is None:return None
        vid=r['vehicle_id'];v=self.vehicles[vid]
        if not parts or parts[0]['rail_id']!=v['rail_id'] or parts[0]['start_um']!=v['offset_um']:return None
        rail=v['rail_id'];start=v['offset_um'];end=parts[0]['end_um'];half=self.halves[v['type_id']]
        for owner,(low,high) in claim_index.get(rail,()):
            if owner!=vid and high>start-half and low<end+half:end=min(end,low-half)
        if not start<end<parts[0]['end_um']:return None
        target={'rail_id':rail,'offset_um':end}
        try:self._safe(target,v['type_id'])
        except ValueError:return None
        short=[{**parts[0],'end_um':end}]
        tail=[{**r['remaining'][0],'start_um':end}]+r['remaining'][1:]
        return short,tail,target

    def _passages_by_rail(self):
        # Prepared passages are immutable; rebuild only if the list is replaced.
        cached=self._passage_index
        if cached is None or cached[0] is not self.passages:
            index={}
            for p in self.passages:
                for rid in p['rail_ids']:index.setdefault(rid,[]).append(p['id'])
            cached=self._passage_index=(self.passages,index)
        return cached[1]

    def _resources(self, parts, claims):
        index=self._passages_by_rail()
        resources={'zcu:'+pid for rid in claims for pid in index.get(rid,())}
        for a,b in zip(parts,parts[1:]):
            if (a['rail_id']!=b['rail_id'] or a['end_um']==self.rails[a['rail_id']]['length_um'] and b['start_um']==0):
                node=self.rails[a['rail_id']]['to_node']
                if node in self.junctions:resources.add(self.junctions[node])
        return sorted(resources)

    def _claim_index(self):
        # Rail -> [(owner, interval)] over occupancy, hop sweeps and targets.
        # Claims change only through this class, which keeps the index exact
        # via _set_claims; claims_changed() drops it for outside mutation.
        # Entry order within a rail is not meaningful.
        if self._claim_cache is None:self._claim_cache=self._build_claim_index()
        return self._claim_cache[0]

    def _build_claim_index(self):
        by_rail={};by_owner={};rails={}
        for slot,records in enumerate((self.occupancy,self.hops,self.targets)):
            for vid,record in records.items():
                claims=record['claims'] if slot==1 else record
                rails[slot,vid]=tuple(claims)
                for rid,intervals in claims.items():by_owner.setdefault(rid,{})[slot,vid]=intervals
        for rid,owners in by_owner.items():
            by_rail[rid]=[(vid,interval) for (_,vid),intervals in owners.items() for interval in intervals]
        return by_rail,by_owner,rails

    def _set_claims(self, slot, vid, claims):
        """Replace one owner's indexed claims: slot 0 occupancy, 1 hop sweep, 2 target; None removes."""
        self._claims_version+=1
        if self._claim_cache is None:self._claim_epoch+=1;return
        by_rail,by_owner,rails=self._claim_cache;key=(slot,vid)
        old=rails.pop(key,())
        if claims:rails[key]=tuple(claims)
        versions=self._rail_versions
        for rid in {*old,*(claims or ())}:
            versions[rid]=versions.get(rid,0)+1
            owners=by_owner.setdefault(rid,{})
            owners.pop(key,None)
            if claims and rid in claims:owners[key]=claims[rid]
            if owners:by_rail[rid]=[(owner,interval) for (_,owner),intervals in owners.items() for interval in intervals]
            else:del by_owner[rid];by_rail.pop(rid,None)

    def _blockers(self, owner, claims, resources, *, claim_index=None):
        blockers=set()
        if claim_index is None:
            for kind,records in (('occupancy',self.occupancy),('hop',self.hops),('holding',self.targets)):
                for vid,record in records.items():
                    if vid==owner:continue
                    other=record['claims'] if kind=='hop' else record
                    for rid,intervals in claims.items():
                        if any(overlaps(a,b) for a in intervals for b in other.get(rid,[])):
                            blockers.add(('rail',rid,vid))
        else:
            for rid,intervals in claims.items():
                for vid,other in claim_index.get(rid,()):
                    if vid!=owner and any(overlaps(interval,other) for interval in intervals):
                        blockers.add(('rail',rid,vid))
        for resource in resources:
            if self.resources.get(resource) not in (None,owner):blockers.add(('passage',resource,self.resources[resource]))
        return [{'kind':kind,'id':rid,'owner':vid} for kind,rid,vid in sorted(blockers)]

    def _waiting(self, r, blockers):
        if r['blockers']!=blockers:
            r['blockers']=blockers;self.emit('traffic_wait_changed',{'request_id':r['id'],'blockers':blockers})
            return True
        return False

    def _hop_route_key(self, r, v):
        # Freeze mutable route evidence, including in-place changes, not object IDs.
        return (v['type_id'],v['rail_id'],v['offset_um'],
                tuple(sorted(r['target_pose'].items())),
                tuple(tuple(sorted(p.items())) for p in r['remaining']))

    def _route_snapshot(self, r, v):
        # Same evidence as _hop_route_key, copied for direct comparison.
        return (v['type_id'],v['rail_id'],v['offset_um'],dict(r['target_pose']),[dict(p) for p in r['remaining']])

    def _route_unchanged(self, r, v, snapshot):
        return (snapshot[0]==v['type_id'] and snapshot[1]==v['rail_id'] and snapshot[2]==v['offset_um']
                and snapshot[3]==r['target_pose'] and snapshot[4]==r['remaining'])

    def _claims_stamp(self, claims, claim_index):
        # Claim-index generation of the rails a permission key read; None when
        # the key was not built from the live index.
        cache=self._claim_cache
        if cache is None or cache[0] is not claim_index:return None
        versions=self._rail_versions
        return (self._claim_epoch,tuple((rid,versions.get(rid,0)) for rid in claims))

    def _claims_unchanged(self, stamp, claim_index):
        # Same live index generation and no claim change on those rails since
        # the stamp: the rail part of the permission key is unchanged.
        cache=self._claim_cache
        if stamp is None or cache is None or cache[0] is not claim_index or stamp[0]!=self._claim_epoch:return False
        versions=self._rail_versions
        return all(versions.get(rid,0)==version for rid,version in stamp[1])

    def _hop_permission_key(self, vid, claims, resources, rails, claim_index):
        # Include every potential new blocker on swept rails, not just old owners.
        # Moving poses do not change held sweeps until the safe endpoint arrives.
        # Index entry order is arbitrary; sort so equal claim sets give equal keys.
        return (tuple((rid,tuple(sorted((owner,tuple(interval)) for owner,interval in claim_index.get(rid,())
                                if owner!=vid))) for rid in claims),
                tuple((resource,self.resources.get(resource)) for resource in resources),
                tuple((rid,tuple(sorted(self.causes['rail'][rid]))) for rid in rails))

    def dispatch(self) -> bool:
        probes=getattr(getattr(self,'transfers',None),'probes',None) or getattr(self,'probes',None)
        if probes:
            with probes.handler('traffic_dispatch'):return self._dispatch()
        return self._dispatch()

    def _dispatch(self) -> bool:
        changed=self._bridge();claim_index=None
        # Dispatch only current vehicle work; terminal idle evidence can be archived.
        current=(self.requests[rid] for rid in self.active.values())
        for r in sorted(current,key=lambda r:(r['requested_at_us'],r['id'],r['vehicle_id'])):
            if r['status']!='pending':continue
            probes=getattr(getattr(self,'transfers',None),'probes',None) or getattr(self,'probes',None)
            if probes:probes.count_work('traffic_requests_considered')
            self._check_owner(r)
            vid=r['vehicle_id'];v=self.vehicles[vid]
            if not v.get('available',True):
                if self._waiting(r,[{'kind':'fault','id':'oht:'+vid,'owner':c} for c in sorted(self.causes['oht'][vid])]):claim_index=None
                continue
            if r['reposition'] and vid not in self.targets:
                claims=self._envelope(r['target_pose'],v['type_id'])
                if claim_index is None:claim_index=self._claim_index()
                blocked=self._blockers(vid,claims,[],claim_index=claim_index)
                if blocked:
                    if self._waiting(r,blocked):claim_index=None
                    continue
                self.targets[vid]=claims;self._set_claims(2,vid,claims);claim_index=None
            if not r['remaining']:
                self._finish(r);changed=True;claim_index=None;continue
            cached=self._denied_hops.get(r['id'])
            if cached is not None and self._route_unchanged(r,v,cached['route']):
                if claim_index is None:claim_index=self._claim_index()
                if self._claims_unchanged(cached.get('stamp'),claim_index):
                    # No claim on the swept rails changed since the key was built.
                    old=cached['permission'];resources,rails=cached['resources'],cached['rails']
                    same=(old[1]==tuple((resource,self.resources.get(resource)) for resource in resources)
                          and old[2]==tuple((rid,tuple(sorted(self.causes['rail'][rid]))) for rid in rails))
                else:
                    same=self._hop_permission_key(vid,cached['claims'],cached['resources'],cached['rails'],claim_index)==cached['permission']
                if same and r['blockers']==cached['blockers']:
                    if probes:probes.count_work('traffic_denied_checks_reused')
                    continue
            parts,tail,target=self._next_hop(r);self._safe(target,v['type_id'])
            claims=swept_intervals(self.model,parts,self.halves[v['type_id']]);resources=self._resources(parts,claims)
            if claim_index is None:claim_index=self._claim_index()
            if self.hop_lookahead_um and tail:
                parts,tail,target,claims,resources=self._extend_hop(r,v,parts,tail,target,claims,resources,claim_index)
            rails=tuple(sorted({p['rail_id'] for p in parts}))
            permission=self._hop_permission_key(vid,claims,resources,rails,claim_index)
            stamp=self._claims_stamp(claims,claim_index)
            blocked=self._blockers(vid,claims,resources,claim_index=claim_index)
            blocked.extend({'kind':'fault','id':'rail:'+rid,'owner':c}
                for rid in sorted({p['rail_id'] for p in parts}) for c in sorted(self.causes['rail'][rid]))
            if blocked and all(b['kind']=='rail' for b in blocked):
                if probes:probes.count_work('traffic_guarded_stop_checks')
                guarded=self._guarded_stop(r,parts,claim_index)
                if guarded is not None:
                    parts,tail,target=guarded
                    claims=swept_intervals(self.model,parts,self.halves[v['type_id']]);resources=self._resources(parts,claims)
                    rails=tuple(sorted({p['rail_id'] for p in parts}))
                    permission=self._hop_permission_key(vid,claims,resources,rails,claim_index)
                    stamp=self._claims_stamp(claims,claim_index)
                    blocked=self._blockers(vid,claims,resources,claim_index=claim_index)
                    blocked.extend({'kind':'fault','id':'rail:'+rid,'owner':c}
                        for rid in rails for c in sorted(self.causes['rail'][rid]))
                    if not blocked and probes:probes.count_work('traffic_guarded_stop_hops')
            if (blocked and self.bay_entry_divert
                    and self._divert_at_bay_entry(r,v,parts,blocked,claim_index)):
                if probes:probes.count_work('traffic_bay_entry_diverts')
                self._denied_hops.pop(r['id'],None)
                changed=True;claim_index=None;continue
            if blocked:
                if probes:probes.count_work('traffic_blocked_hop_attempts')
                if self._waiting(r,blocked):claim_index=None
                elif probes:probes.count_work('traffic_unchanged_blocked_hop_attempts')
                # Wait emission may synchronously cancel or replace this owner.
                if (r['status']=='pending' and self.requests.get(r['id']) is r
                        and self.active.get(vid)==r['id'] and v['movement_id']==r['id']):
                    self._denied_hops[r['id']]={'route':self._route_snapshot(r,v),'permission':permission,
                        'stamp':stamp,'claims':claims,'resources':resources,'rails':rails,
                        'blockers':[dict(b) for b in blocked]}
                continue
            self._denied_hops.pop(r['id'],None)
            seconds=self._duration(parts,v['type_id']);boundary=to_ticks(r['cumulative_seconds']+seconds)
            hop={'parts':parts,'claims':claims,'resources':resources,'tail':tail,'target_pose':target,
                 'seconds':seconds,'boundary_us':boundary,'duration_us':boundary-r['boundary_us'],
                 'distance_um':sum(p['end_um']-p['start_um'] for p in parts),'started_at_us':self.scheduler.now_us,
                 'request_id':r['id'],'active_elapsed_us':0,'active_since_us':self.scheduler.now_us}
            self.hops[vid]=hop;self._set_claims(1,vid,claims)
            for resource in resources:self.resources[resource]=vid
            r['wait_us']+=self.scheduler.now_us-r['waiting_since_us'];r['status']='moving';r['blockers']=[]
            first=r['started_at_us'] is None
            if first:r['started_at_us']=self.scheduler.now_us
            hop['timer']=Timer(self.scheduler,'traffic:'+r['id']+':'+str(len(r['remaining']))+':'+str(boundary),
                               hop['duration_us'],native_action(self,'traffic:arrive',r['id'],None,lambda r=r:self._arrive(r)))
            self.emit('traffic_hop_started',self._hop_event(r,hop))
            changed=True
            if first:
                # The start callback may change other owners; rebuild.
                r['on_start']();claim_index=None
        # claim_index is None or exact here (reset after any foreign callback).
        return self._plan_idle(claim_index) or changed

    def _recover_cycles(self):
        # Scheduler checks run only after all ordered dirty passes close.
        from .traffic_cycle_recovery import recover_cycle
        probes=getattr(getattr(self,'transfers',None),'probes',None) or getattr(self,'probes',None)
        if self.queue_divert:
            from .traffic_queue_divert import divert_queues
            if probes:
                with probes.handler('traffic_queue_divert'):
                    if divert_queues(self):return True
            elif divert_queues(self):return True
        if probes:
            with probes.handler('traffic_cycle_recovery'):return recover_cycle(self)
        return recover_cycle(self)

    def _bridge(self):
        if self.transfers is None:return False
        changed=False
        # A vehicle owns at most one live movement, including cancelled travel
        # that must still reach its safe endpoint. Idle traffic has no transfer.
        current={v.get('movement_id') for v in self.vehicles.values()}
        for mid in sorted(mid for mid in current if mid in self.transfers.movements):
            m=self.transfers.movements[mid]
            if m['status']!='requested' or m['cancelled'] or mid in self.requests:continue
            r=self.transfers.demands[m['demand_id']]
            target=self.transfers.dock_id(r['origin_id'] if m['phase']=='pickup' else r['destination_id'])
            self.request(m['vehicle_id'],target,mid,path=m['path'],requested_at_us=m['queued_at_us'],
                on_start=native_action(self,'movement:start',mid,None,lambda mid=mid:self.transfers.movement_started(mid)),
                on_complete=native_action(self,'movement:complete',mid,None,lambda m=m:self.transfers.arrived(m['demand_id'],m['phase'])),
                on_cancel=native_action(self,'movement:cancel',mid,None,lambda mid=mid:self.transfers.cancelled_arrived(mid)))
            changed=True
        return changed

    def can_yield(self, vehicle_id):
        rid=self.active.get(vehicle_id)
        if rid is None:return False
        r=self.requests[rid];v=self.vehicles[vehicle_id]
        self._check_owner(r)
        return (r['reposition'] and r['status']=='pending' and vehicle_id not in self.hops
                and any(all(v[k]==h[k] for k in ('rail_id','offset_um')) for h in self.holdings.values()))

    def yield_for_assignment(self, vehicle_id):
        if not self.can_yield(vehicle_id):raise RuntimeError('idle movement cannot yield before a safe holding boundary')
        self.cancel(self.active[vehicle_id])

    def _empty(self, vid):
        v=self.vehicles[vid]
        return not v.get('assignment_id') and (self.transfers is None or
            self.transfers.custody.slots[v['carrier_slot_id']]['carrier_id'] is None)

    def _router(self,kind):
        if kind not in self._idle_routers:
            self._idle_routers[kind]=FreeFlowCosts(self.model,kind,cache=self._geometry_cache)
        return self._idle_routers[kind]

    def _idle_destinations(self, vehicle):
        # Cache geometry-only rank, never occupancy, passages or fault permission.
        key=tuple(vehicle[k] for k in ('rail_id','offset_um','type_id'))
        if key not in self._idle_rankings:
            # Share immutable graph rows across new offsets; physical permissions
            # remain fresh in _plan_idle. Routers are derived, bounded per type.
            kind=vehicle['type_id']
            self._idle_rankings[key]=_LazyRanking(self._router(kind).idle_order(vehicle,self.holdings,target_key=self._idle_target_key))
            if len(self._idle_rankings)>128:self._idle_rankings.popitem(last=False)
        self._idle_rankings.move_to_end(key)
        return self._idle_rankings[key]

    def _idle_plan(self, vehicle, target):
        key=tuple(vehicle[k] for k in ('rail_id','offset_um','type_id'))+(target,)
        if key not in self._idle_plans:
            path=self._router(vehicle['type_id']).path(vehicle,self.holdings[target])
            parts,_,_=self._next_hop({'remaining':path['sublinks'],'target_pose':self.holdings[target]})
            claims=swept_intervals(self.model,parts,self.halves[vehicle['type_id']])
            self._idle_plans[key]=(path,claims,self._resources(parts,claims))
            if len(self._idle_plans)>128:self._idle_plans.popitem(last=False)
        self._idle_plans.move_to_end(key)
        return self._idle_plans[key]

    def _service_vehicles(self):
        # Vehicles whose envelope overlaps a service dock. Occupancy changes
        # only on arrival (kept current there) or via claims_changed().
        if self._service_set is None:
            self._service_set={vid for vid in self.vehicles if self._at_service(self.occupancy[vid])}
        return self._service_set

    def _at_service(self, occupied):
        half=self.model['traffic']['common_half_um'];docks=self._dock_offsets_by_rail
        for rid,intervals in occupied.items():
            offsets=docks.get(rid)
            if not offsets:continue
            start,end=intervals[0]
            # Touching envelopes do not overlap: both offset bounds are open.
            candidate=bisect_right(offsets,start-half)
            if candidate<len(offsets) and offsets[candidate]<end+half:return True
        return False

    def _plan_idle(self,claim_index=None):
        if not self.manage_idle:return False
        # Idle dependencies describe an empty, unassigned vehicle's clearing
        # attempt. Once assigned or loaded, only its current transport blockers
        # may root clearing; former idle targets must not activate other vehicles.
        for vid in tuple(self.idle_wait):
            if not self._empty(vid):self.idle_wait.pop(vid)
        blockers={b['owner'] for rid in self.active.values() for r in (self.requests[rid],)
                  if r['status']=='pending' for b in r['blockers'] if b['kind']!='fault'}
        # Wait evidence must not create its own clearing need. Follow only
        # chains rooted in live pending movement or a service obstruction.
        blockers.update(vid for vid in self.idle_wait
                        if self._at_service(self.occupancy[vid]))
        frontier=list(blockers)
        while frontier:
            for blocker in self.idle_wait.get(frontier.pop(),()):
                owner=blocker['owner']
                if owner not in blockers:
                    blockers.add(owner);frontier.append(owner)
        changed=False;probes=getattr(getattr(self,'transfers',None),'probes',None) or getattr(self,'probes',None)
        if claim_index is None:claim_index=self._claim_index()
        # Only blockers, vehicles at a service dock and vehicles with idle wait
        # evidence can act or change state below; visit them in vehicle order.
        vehicles=self.vehicles
        candidates=(blockers|self._service_vehicles()|set(self.idle_wait))&vehicles.keys()
        for vid in sorted(candidates):
            v=vehicles[vid]
            if vid in self.active or v.get('movement_id') or not v.get('available',True):continue
            # Cheap tests first. Loaded or assigned vehicles have no idle wait
            # (removed above), so the pop below is a no-op for them.
            if vid not in blockers and not self._at_service(self.occupancy[vid]):
                self.idle_wait.pop(vid,None);continue
            if not self._empty(vid):continue
            memo=self._idle_memo.get(vid);pose=(v['rail_id'],v['offset_um'],v['type_id'],self._idle_target_key)
            if (memo is not None and memo[0]==pose and memo[1]==self._claim_epoch
                    and all(self._rail_versions.get(rid,0)==version for rid,version in memo[2])
                    and all(self.resources.get(resource)==owner for resource,owner in memo[3])):
                # Every input of the evaluation below is unchanged: same waits.
                selected=None;waiting=[dict(b) for b in memo[4]]
                if probes:probes.count_work('traffic_idle_memo_hits')
            else:
                if probes:probes.count_work('traffic_idle_evaluations')
                waiting=[];selected=None;read_rails=set();read_resources=()
                for hid,seconds in self._idle_destinations(v):
                    if seconds==0:continue
                    envelope=self._envelope(self.holdings[hid],v['type_id']);read_rails.update(envelope)
                    blocked=self._blockers(vid,envelope,[],claim_index=claim_index)
                    if not blocked:selected=hid;break
                    waiting.extend(blocked)
                path=None
                if selected is not None:
                    path,claims,resources=self._idle_plan(v,selected)
                    read_rails.update(claims);read_resources=resources
                    blocked=self._blockers(vid,claims,resources,claim_index=claim_index)
                    # A blocked follower's far target can prevent its leader from
                    # advancing. Retain stationary occupancy until the first swept
                    # hop clears occupancy/passage blockers. Dispatch checks faults.
                    if blocked:waiting.extend(blocked);selected=None
                if selected is None:
                    waiting=[dict(zip(('kind','id','owner'),b)) for b in sorted({tuple(b[k] for k in ('kind','id','owner')) for b in waiting})]
                    self._idle_memo[vid]=(pose,self._claim_epoch,
                        tuple((rid,self._rail_versions.get(rid,0)) for rid in read_rails),
                        tuple((resource,self.resources.get(resource)) for resource in read_resources),
                        tuple(dict(b) for b in waiting))
            if selected is None:
                if self.idle_wait.get(vid)!=waiting:
                    # Idle wait evidence is output only; it changes no claim.
                    self.idle_wait[vid]=waiting;self.emit('traffic_idle_wait_changed',{'vehicle_id':vid,'blockers':waiting})
                    self.dirty();changed=True
                continue
            self.idle_wait.pop(vid,None);self.idle_sequence+=1
            self.request(vid,selected,f'idle:{self.idle_sequence:08}',reposition=True,path=path,_sequenced=True)
            # request() indexes the new target claim, so later vehicles in this pass see it.
            changed=True
        return changed

    def _hop_event(self, r, hop):
        return {'request_id':r['id'],'vehicle_id':r['vehicle_id'],
                **{k:v for k,v in hop.items() if k not in ('timer','tail')}}

    def _arrive(self, r):
        vid=r['vehicle_id'];hop=self.hops[vid]
        if (r['status']!='moving' or self.active.get(vid)!=r['id'] or hop['request_id']!=r['id']
                or self.vehicles[vid]['movement_id']!=r['id']
                or any(self.resources.get(resource)!=vid for resource in hop['resources'])):
            raise RuntimeError('traffic completion ownership changed')
        self.vehicles[vid].update(hop['target_pose']);self.occupancy[vid]=self._envelope(hop['target_pose'],self.vehicles[vid]['type_id'])
        self._set_claims(0,vid,self.occupancy[vid])
        for resource in hop['resources']:del self.resources[resource]
        del self.hops[vid];self._set_claims(1,vid,None)
        if self._service_set is not None:
            if self._at_service(self.occupancy[vid]):self._service_set.add(vid)
            else:self._service_set.discard(vid)
        r.update(remaining=hop['tail'],cumulative_seconds=r['cumulative_seconds']+hop['seconds'],boundary_us=hop['boundary_us'],
                 status='pending',waiting_since_us=self.scheduler.now_us)
        self.emit('traffic_hop_arrived',self._hop_event(r,hop))
        if r['cancel_requested'] or not r['remaining']:self._finish(r)
        if self.transfers is not None:
            notify=getattr(self.transfers,'traffic_changed',None)
            if notify is None:self.transfers.dirty()
            else:notify(vid)
        self.dirty()

    def _finish(self, r):
        self._check_owner(r)
        vid=r['vehicle_id'];cancelled=r['cancel_requested']
        if r['status']=='pending':r['wait_us']+=self.scheduler.now_us-r['waiting_since_us']
        self._denied_hops.pop(r['id'],None)
        r['status']='cancelled' if cancelled else 'complete';r['completed_at_us']=self.scheduler.now_us
        self.targets.pop(vid,None);self._set_claims(2,vid,None);del self.active[vid];self.vehicles[vid]['movement_id']=None
        self.emit('traffic_cancelled' if cancelled else 'traffic_arrived',{'request_id':r['id'],'vehicle_id':vid,
            'pose':self.pose(vid),'completed_at_us':self.scheduler.now_us})
        r['on_cancel' if cancelled else 'on_complete']();self.dirty()

    def cancel(self, request_id):
        if request_id not in self.requests:
            raise ValueError('traffic request already finished')
        r=self.requests[request_id]
        if r['status'] not in ('pending','moving'):raise ValueError('traffic request already finished')
        self._check_owner(r)
        if self.transfers is not None and request_id in self.transfers.movements:
            movement=self.transfers.movements[request_id];demand=self.transfers.demands[movement['demand_id']]
            if demand['status']!='cancelled':
                self.transfers.cancel(demand['id']);return
        r['cancel_requested']=True
        if r['status']=='pending':self._finish(r)
        self.dirty()

    def _check_owner(self, r):
        vid=r['vehicle_id']
        if (self.requests.get(r['id']) is not r or self.active.get(vid)!=r['id']
                or self.vehicles[vid]['movement_id']!=r['id']):
            raise RuntimeError('traffic request ownership changed')

    def pose(self, vehicle_id):
        v=self.vehicles[vehicle_id];hop=self.hops.get(vehicle_id)
        if hop is None:return {k:v[k] for k in ('rail_id','offset_um')}
        elapsed=Fraction(hop['active_elapsed_us']+(self.scheduler.now_us-hop['active_since_us']
                        if hop['active_since_us'] is not None else 0),1000000)
        for p in hop['parts']:
            for low,high,speed in speed_parts(self.rails[p['rail_id']],p['start_um'],p['end_um'],self.types[v['type_id']],self.model['policy']):
                seconds=Fraction(high-low)/speed
                if elapsed<seconds:return {'rail_id':p['rail_id'],'offset_um':Fraction(low)+elapsed*speed}
                elapsed-=seconds
        return dict(hop['target_pose'])

    def _fault_target(self, target_id, target_type):
        if target_type is None:
            matches=[kind for kind,targets in self.causes.items() if target_id in targets]
            if len(matches)!=1:raise ValueError('fault target is unknown or ambiguous')
            target_type=matches[0]
        if target_type not in self.causes or target_id not in self.causes[target_type]:
            raise ValueError('unknown AMHS fault target')
        return target_type

    def pause(self, target_id, cause_id, *, target_type=None):
        kind=self._fault_target(target_id,target_type)
        if not isinstance(cause_id,str) or not cause_id:raise ValueError('fault requires cause identity')
        causes=self.causes[kind][target_id]
        if cause_id in causes:return
        if kind=='oht' and not causes:
            self.base_available[target_id]=self.vehicles[target_id].get('available',True)
            self.vehicles[target_id]['available']=False
        causes.add(cause_id);token=json.dumps([kind,target_id,cause_id],separators=(',',':'))
        for vid,hop in self.hops.items():
            if (kind=='oht' and vid==target_id or
                    kind=='rail' and any(p['rail_id']==target_id for p in hop['parts'])):
                if not hop['timer'].causes:
                    hop['active_elapsed_us']+=self.scheduler.now_us-hop['active_since_us'];hop['active_since_us']=None
                hop['timer'].pause(self.scheduler.now_us,token)
        if self.transfers is not None:self.transfers.fault_changed(kind,target_id,token,paused=True)
        self.emit('amhs_fault_started',{'target_type':kind,'target_id':target_id,'cause_id':cause_id});self.dirty()

    def recover(self, target_id, cause_id, *, target_type=None):
        kind=self._fault_target(target_id,target_type);causes=self.causes[kind][target_id]
        if cause_id not in causes:raise ValueError('unknown AMHS fault cause')
        causes.remove(cause_id);token=json.dumps([kind,target_id,cause_id],separators=(',',':'))
        if kind=='oht' and not causes:self.vehicles[target_id]['available']=self.base_available.pop(target_id)
        for hop in self.hops.values():
            if token in hop['timer'].causes:
                hop['timer'].resume(self.scheduler.now_us,token)
                if not hop['timer'].causes:hop['active_since_us']=self.scheduler.now_us
        if self.transfers is not None:self.transfers.fault_changed(kind,target_id,token,paused=False)
        self.emit('amhs_fault_recovered',{'target_type':kind,'target_id':target_id,'cause_id':cause_id});self.dirty()
