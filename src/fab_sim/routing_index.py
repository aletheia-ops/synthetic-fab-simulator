"""Run-local exact travel-cost index for an immutable physical topology."""
from collections import OrderedDict
from copy import deepcopy
from fractions import Fraction
from heapq import heapify,heappop,heappush,merge
from hashlib import sha256
from .derived_cache import DerivedCache
from math import lcm
from .routing import speed_parts, travel_seconds


class _Unscalable(Exception):
    pass


class FreeFlowCosts:
    """Reuse full-edge costs and node rows; retain at most a fixed entry budget.

    Input topology, vehicle speeds and free-flow policy are snapshotted once.
    Build a new index when those physical inputs change. Occupancy, permission
    and fault state do not participate in this ranking calculation.
    """
    def __init__(self,topology,vehicle_type_id,*,max_cached_entries=524288,cache=None):
        if type(max_cached_entries) is not int or max_cached_entries<=0:
            raise ValueError('invalid routing cache entry budget')
        model=topology.get('model',topology)
        self.rails={r['id']:deepcopy(r) for r in model['rails']}
        self.vehicle=deepcopy(next(v for v in model['vehicle_types'] if v['id']==vehicle_type_id))
        self.policy=deepcopy(model['policy']);self.outgoing={};self.incoming={}
        for rail in self.rails.values():
            cost=self._cost(rail,0,rail['length_um'])
            self.outgoing.setdefault(rail['from_node'],[]).append((rail['to_node'],cost))
            self.incoming.setdefault(rail['to_node'],[]).append((rail['from_node'],cost))
        self.max_cached_entries=max_cached_entries;self.rows=OrderedDict();self.cached_entries=0
        self.stats={'searches':0,'hits':0,'misses':0,'evictions':0,'projected_destinations':0,'idle_rank_hits':0,'reverse_searches':0,'path_searches':0}
        self.cache=cache if cache is not None else DerivedCache(512*1024*1024)
        self._shared_rows=cache is not None
        self._namespace=sha256(repr((self.rails,self.vehicle,self.policy)).encode()).hexdigest()
        self._segment_namespace='segments:'+self._namespace
        # Every free-flow cost is a sum of distance/speed terms. Scaling by the
        # lcm of the speed numerators makes them exact integers, so integer
        # Dijkstra and sorting give the same order, ties and values.
        self._scale=None;self._full_segments={};self._outgoing_rails=None;self._idle_heads={}
        try:
            scale=1
            for rail in self.rails.values():
                for _,_,speed in speed_parts(rail,0,rail['length_um'],self.vehicle,self.policy):
                    scale=lcm(scale,Fraction(speed).numerator)
            outgoing={};incoming={}
            for node,edges in self.outgoing.items():
                outgoing[node]=[(end,self._as_int(cost,scale)) for end,cost in edges]
            for node,edges in self.incoming.items():
                incoming[node]=[(start,self._as_int(cost,scale)) for start,cost in edges]
            self._scale=scale;self._outgoing_int=outgoing;self._incoming_int=incoming
        except (_Unscalable,ValueError,TypeError):
            self._scale=None

    @staticmethod
    def _as_int(value,scale):
        scaled=Fraction(value)*scale
        if scaled.denominator!=1:raise _Unscalable
        return scaled.numerator

    def _icost(self,rail,low,high):
        return self._as_int(self._cost(rail,low,high),self._scale)

    def _row_int(self,start_node):
        namespace='irows:'+self._namespace
        value=self.cache.get(namespace,start_node)
        if value is not None:self.stats['hits']+=1;return value
        self.stats['misses']+=1;self.stats['searches']+=1
        queue=[(0,start_node)];best={start_node:0};outgoing=self._outgoing_int
        while queue:
            seconds,node=heappop(queue)
            if seconds!=best[node]:continue
            for end,cost in outgoing.get(node,()):
                candidate=seconds+cost
                if end not in best or candidate<best[end]:
                    best[end]=candidate;heappush(queue,(candidate,end))
        if len(best)<=self.max_cached_entries:self.cache.put(namespace,start_node,best)
        return best

    def _cost(self,rail,low,high):
        return travel_seconds(rail,low,high,self.vehicle,self.policy)

    def _indexed_cost(self,rail,low,high):
        # Bulk ranking repeatedly projects the same immutable dock/vehicle
        # offsets. Traffic's small hop/idle calculations keep the direct path.
        key=(rail['id'],low,high);value=self.cache.get(self._segment_namespace,key)
        if value is None:
            value=self._cost(rail,low,high)
            self.cache.put(self._segment_namespace,key,value)
        return value

    def _indexed_icost(self,rail,low,high):
        key=(rail['id'],low,high);namespace='i'+self._segment_namespace
        value=self.cache.get(namespace,key)
        if value is None:
            value=self._icost(rail,low,high)
            self.cache.put(namespace,key,value)
        return value

    def _pose(self,pose):
        if pose['rail_id'] not in self.rails:raise ValueError('unknown pose rail')
        rail=self.rails[pose['rail_id']];offset=pose['offset_um']
        if type(offset) is not int or not 0<=offset<=rail['length_um']:
            raise ValueError('invalid rail offset')
        return rail

    def _row(self,start_node):
        if self._shared_rows:
            value=self.cache.get('rows:'+self._namespace,start_node)
            if value is not None:self.stats['hits']+=1;return value
        if start_node in self.rows:
            self.stats['hits']+=1;self.rows.move_to_end(start_node)
            return self.rows[start_node]
        self.stats['misses']+=1;self.stats['searches']+=1
        queue=[(Fraction(0),start_node)];best={start_node:Fraction(0)}
        while queue:
            seconds,node=heappop(queue)
            if seconds!=best[node]:continue
            for end,cost in self.outgoing.get(node,()):
                candidate=seconds+cost
                if end not in best or candidate<best[end]:
                    best[end]=candidate;heappush(queue,(candidate,end))
        if self._shared_rows:
            if len(best)<=self.max_cached_entries:self.cache.put('rows:'+self._namespace,start_node,best)
            return best
        if len(best)<=self.max_cached_entries:
            while self.cached_entries+len(best)>self.max_cached_entries:
                _,removed=self.rows.popitem(last=False);self.cached_entries-=len(removed)
                self.stats['evictions']+=1
            self.rows[start_node]=best;self.cached_entries+=len(best)
        return best

    def cost_key(self):
        """Exact, order-preserving integer key for costs() results, or None.

        Scaled costs are Fraction(n, scale) in lowest terms, so the reduced
        denominator divides scale and the key recovers n exactly."""
        if self._scale is None or not self._shared_rows:return None
        scale=self._scale
        return lambda cost:cost.numerator*(scale//cost.denominator)

    def costs(self,start,destinations):
        origin=self._pose(start)
        targets=[(pid,pose,self._pose(pose)) for pid,pose in destinations.items()]
        if not targets:return {}
        if self._scale is not None and self._shared_rows:
            # Same sums on exact scaled integers; Fraction(n, scale) equals
            # the rational result and normalizes identically. A private
            # router keeps its entry-budgeted rational row cache instead.
            row=self._row_int(origin['to_node']);segment=self._indexed_icost;scale=self._scale
            tail=segment(origin,start['offset_um'],origin['length_um'])
            result={};self.stats['projected_destinations']+=len(targets)
            for pid,pose,rail in targets:
                best=None
                if rail['id']==origin['id'] and pose['offset_um']>=start['offset_um']:
                    best=segment(rail,start['offset_um'],pose['offset_um'])
                if rail['from_node'] in row:
                    candidate=tail+row[rail['from_node']]+segment(rail,0,pose['offset_um'])
                    if best is None or candidate<best:best=candidate
                if best is not None:result[pid]=Fraction(best,scale)
            return result
        row=self._row(origin['to_node']);tail=self._indexed_cost(origin,start['offset_um'],origin['length_um'])
        result={};self.stats['projected_destinations']+=len(targets)
        for pid,pose,rail in targets:
            candidates=[]
            if rail['id']==origin['id'] and pose['offset_um']>=start['offset_um']:
                candidates.append(self._indexed_cost(rail,start['offset_um'],pose['offset_um']))
            if rail['from_node'] in row:
                candidates.append(tail+row[rail['from_node']]+self._indexed_cost(rail,0,pose['offset_um']))
            if candidates:result[pid]=min(candidates)
        return result

    def costs_to(self,origins,end):
        """Exact reverse costs; offsets share a topology-only node row."""
        target=self._pose(end)
        prepared=[(oid,pose,self._pose(pose)) for oid,pose in origins.items()]
        if not prepared:return {}
        if self._scale is not None and self._shared_rows:return self._costs_to_int(target,end,prepared)
        namespace='reverse:'+self._namespace;key=target['from_node']
        row=self.cache.get(namespace,key)
        if row is None:
            self.stats['reverse_searches']+=1
            queue=[(Fraction(0),key)];row={key:Fraction(0)}
            while queue:
                seconds,node=heappop(queue)
                if seconds!=row[node]:continue
                for previous,cost in self.incoming.get(node,()):
                    candidate=seconds+cost
                    if previous not in row or candidate<row[previous]:
                        row[previous]=candidate;heappush(queue,(candidate,previous))
            if len(row)<=self.max_cached_entries:self.cache.put(namespace,key,row)
        head=self._indexed_cost(target,0,end['offset_um']);result={}
        for oid,pose,rail in prepared:
            candidates=[]
            if rail['id']==target['id'] and end['offset_um']>=pose['offset_um']:
                candidates.append(self._indexed_cost(rail,pose['offset_um'],end['offset_um']))
            if rail['to_node'] in row:
                candidates.append(self._indexed_cost(rail,pose['offset_um'],rail['length_um'])+row[rail['to_node']]+head)
            if candidates:result[oid]=min(candidates)
        return result

    def _costs_to_int(self,target,end,prepared):
        namespace='ireverse:'+self._namespace;key=target['from_node']
        row=self.cache.get(namespace,key)
        if row is None:
            self.stats['reverse_searches']+=1
            queue=[(0,key)];row={key:0};incoming=self._incoming_int
            while queue:
                seconds,node=heappop(queue)
                if seconds!=row[node]:continue
                for previous,cost in incoming.get(node,()):
                    candidate=seconds+cost
                    if previous not in row or candidate<row[previous]:
                        row[previous]=candidate;heappush(queue,(candidate,previous))
            if len(row)<=self.max_cached_entries:self.cache.put(namespace,key,row)
        segment=self._indexed_icost;scale=self._scale
        head=segment(target,0,end['offset_um']);result={}
        for oid,pose,rail in prepared:
            best=None
            if rail['id']==target['id'] and end['offset_um']>=pose['offset_um']:
                best=segment(rail,pose['offset_um'],end['offset_um'])
            if rail['to_node'] in row:
                candidate=segment(rail,pose['offset_um'],rail['length_um'])+row[rail['to_node']]+head
                if best is None or candidate<best:best=candidate
            if best is not None:result[oid]=Fraction(best,scale)
        return result

    def reachable(self,start,end):
        """Exact static reachability without constructing caller-owned paths.

        Use the same immutable topology and byte budget as path/cost ranking.
        Validate offsets even on hits; False is a retained answer, not a miss.
        """
        origin=self._pose(start);target=self._pose(end)
        key=(origin['id'],start['offset_um'],target['id'],end['offset_um'])
        namespace='reachable:'+self._namespace
        answer=self.cache.get(namespace,key)
        if answer is None:
            row=(self._row_int(origin['to_node']) if self._scale is not None and self._shared_rows
                else self._row(origin['to_node']))
            answer=(origin['id']==target['id'] and end['offset_um']>=start['offset_um']
                or target['from_node'] in row)
            self.cache.put(namespace,key,answer)
        return answer

    def path(self,start,end,penalties=None):
        """Bounded exact path reuse; each caller owns its returned containers.

        penalties: optional {rail_id: integer seconds} added to the ranking of
        paths entering those rails (congestion-aware routing); the returned
        seconds stay free-flow travel time. Penalized searches are not cached.
        """
        self._pose(start);self._pose(end)
        if penalties and self._scale is not None:
            self.stats['path_searches']+=1
            try:seconds,parts=self._path_int(start,end,penalties)
            except _Unscalable:seconds=None
            if seconds is not None:
                return {'seconds':seconds,'sublinks':[{'id':p[0],'rail_id':p[1],'start_um':p[2],'end_um':p[3]} for p in parts]}
        key=(start['rail_id'],start['offset_um'],end['rail_id'],end['offset_um'])
        namespace='paths:'+self._namespace;packed=self.cache.get(namespace,key)
        if packed is None:
            self.stats['path_searches']+=1
            packed=None
            if self._scale is not None:
                try:packed=self._path_int(start,end)
                except _Unscalable:packed=None
            if packed is None:
                from .routing import free_flow_path
                model={'rails':list(self.rails.values()),'vehicle_types':[self.vehicle],'policy':self.policy}
                path=free_flow_path(model,start,end,self.vehicle['id'])
                packed=(path['seconds'],tuple((p['id'],p['rail_id'],p['start_um'],p['end_um']) for p in path['sublinks']))
            self.cache.put(namespace,key,packed)
        seconds,parts=packed
        return {'seconds':seconds,'sublinks':[{'id':p[0],'rail_id':p[1],'start_um':p[2],'end_um':p[3]} for p in parts]}

    _native_path = path

    def _segment_int(self,rail,low,high):
        pieces=speed_parts(rail,low,high,self.vehicle,self.policy)
        parts=tuple((f"{rail['id']}@{a}:{b}",rail['id'],a,b) for a,b,_ in pieces)
        cost=self._as_int(sum((Fraction(b-a)/speed for a,b,speed in pieces),Fraction(0)),self._scale)
        return parts,cost

    def _path_int(self,start,end,penalties=None):
        """routing.free_flow_path on exact scaled-integer costs.

        Same lexicographic (cost, sublink-ID sequence) search and candidate
        rule; integer costs order exactly as the rational ones.
        """
        rails=self.rails;origin,target=rails[start['rail_id']],rails[end['rail_id']]
        full=self._full_segments
        outgoing=self._outgoing_rails
        if outgoing is None:
            outgoing={}
            for r in rails.values():outgoing.setdefault(r['from_node'],[]).append(r)
            self._outgoing_rails=outgoing
        def whole(r):
            value=full.get(r['id'])
            if value is None:
                parts,cost=self._segment_int(r,0,r['length_um'])
                value=full[r['id']]=(parts,tuple(p[0] for p in parts),cost)
            return value
        # Ranking cost = free-flow cost + penalties of entered rails; the
        # free-flow cost of the chosen path is carried alongside and returned.
        scale=self._scale
        penalty=({rid:self._as_int(seconds,scale) for rid,seconds in penalties.items()} if penalties else {})
        candidates=[]
        if origin['id']==target['id'] and end['offset_um']>=start['offset_um']:
            parts,cost=self._segment_int(origin,start['offset_um'],end['offset_um'])
            candidates.append((cost,tuple(p[0] for p in parts),parts,cost))
        parts,cost=self._segment_int(origin,start['offset_um'],origin['length_um'])
        queue=[(cost,tuple(p[0] for p in parts),origin['to_node'],parts,cost)];best={}
        tail_target=None
        while queue:
            cost,ids,node,parts,free=heappop(queue)
            if node in best and best[node]<=(cost,ids):continue
            best[node]=(cost,ids)
            if candidates and cost>min(candidates)[0]:continue
            if node==target['from_node']:
                if tail_target is None:tail_target=self._segment_int(target,0,end['offset_um'])
                tail,extra=tail_target
                added=penalty.get(target['id'],0)
                candidates.append((cost+extra+added,ids+tuple(p[0] for p in tail),parts+tail,free+extra))
            for r in outgoing.get(node,()):
                tail,tail_ids,extra=whole(r)
                heappush(queue,(cost+extra+penalty.get(r['id'],0),ids+tail_ids,r['to_node'],parts+tail,free+extra))
        if not candidates:raise ValueError('unreachable directed destination')
        _,_,parts,free=min(candidates)
        return (Fraction(free,scale),parts)

    @staticmethod
    def target_key(destinations):
        return sha256(repr(tuple((pid,p['rail_id'],p['offset_um']) for pid,p in destinations.items())).encode()).digest()

    def idle_order(self,start,holdings,*,target_key=None):
        if self._scale is not None:
            try:return self._idle_order_int(start,holdings,target_key=target_key)
            except _Unscalable:pass
        return self._idle_order_exact(start,holdings,target_key=target_key)

    def _idle_order_int(self,start,holdings,*,target_key=None):
        """Holdings in (cost, id) order, found by an incremental Dijkstra.

        Equal to sorting every reachable holding by its exact scaled cost:
        min(direct cost ahead on the origin rail, tail + node distance + head).
        Nodes are expanded before holdings of equal cost and every holding lies
        strictly after its rail's start node, so a holding is only emitted once
        every cheaper (or equal-cost, lower-id) holding is already queued.
        Callers stop at the first free holding, so the search stays local.
        """
        origin=self._pose(start);scale=self._scale
        signature=self.target_key(holdings) if target_key is None else target_key
        by_node=self._idle_heads.get(signature)
        if by_node is None:
            by_node={};same={}
            for pid,pose in holdings.items():
                rail=self._pose(pose)
                by_node.setdefault(rail['from_node'],[]).append((pid,self._icost(rail,0,pose['offset_um'])))
                same.setdefault(rail['id'],[]).append((pid,pose['offset_um']))
            by_node=self._idle_heads[signature]=(by_node,same)
        heads,same=by_node
        tail=self._icost(origin,start['offset_um'],origin['length_um'])
        queue=[(tail,0,origin['to_node'])]
        for pid,offset in same.get(origin['id'],()):
            if offset>=start['offset_um']:queue.append((self._icost(origin,start['offset_um'],offset),1,pid))
        heapify(queue)
        return self._idle_walk(queue,heads,scale)

    def _idle_walk(self,queue,heads,scale):
        settled=set();emitted=set();outgoing=self._outgoing_int
        while queue:
            cost,kind,ident=heappop(queue)
            if kind:
                if ident in emitted:continue
                emitted.add(ident);yield ident,Fraction(cost,scale)
                continue
            if ident in settled:continue
            settled.add(ident)
            for pid,head in heads.get(ident,()):
                if pid not in emitted:heappush(queue,(cost+head,1,pid))
            for end,weight in outgoing.get(ident,()):
                if end not in settled:heappush(queue,(cost+weight,0,end))

    def _idle_order_exact(self,start,holdings,*,target_key=None):
        origin=self._pose(start)
        signature=self.target_key(holdings) if target_key is None else target_key
        key=(origin['id'],signature);namespace='idle:'+self._namespace
        prepared=self.cache.get(namespace,key);row=None
        if prepared is None:
            row=self._row(origin['to_node']);off=[];same=[]
            for pid,pose in holdings.items():
                rail=self._pose(pose)
                if rail['id']==origin['id']:same.append((pid,pose['offset_um']))
                elif rail['from_node'] in row:
                    off.append((pid,row[rail['from_node']]+self._cost(rail,0,pose['offset_um'])))
            prepared=(tuple(sorted(off,key=lambda x:(x[1],x[0]))),tuple(same))
            self.cache.put(namespace,key,prepared)
        else:self.stats['idle_rank_hits']+=1
        off,same=prepared;tail=self._cost(origin,start['offset_um'],origin['length_um'])
        direct=[]
        if same and row is None:row=self._row(origin['to_node'])
        for pid,offset in same:
            candidates=[]
            if offset>=start['offset_um']:candidates.append(self._cost(origin,start['offset_um'],offset))
            if origin['from_node'] in row:
                candidates.append(tail+row[origin['from_node']]+self._cost(origin,0,offset))
            if candidates:direct.append((pid,min(candidates)))
        return merge(((pid,tail+cost) for pid,cost in off),
                     sorted(direct,key=lambda x:(x[1],x[0])),key=lambda x:(x[1],x[0]))

    def cache_info(self):
        info=self.cache.namespace_stats('rows:'+self._namespace) if self._shared_rows else {'entries':len(self.rows),'items':self.cached_entries}
        return {**self.stats,'cached_rows':info['entries'],'cached_entries':info['items'],
            'max_cached_entries':self.max_cached_entries}
