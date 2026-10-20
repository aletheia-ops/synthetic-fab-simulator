"""Finite, physically staged setup campaigns; custody remains authoritative."""
from fractions import Fraction

from .manufacturing import queue_rank


POLICY = 'priority_setup_fifo_campaign_v2'
# execution.setup_policy values. 'staged_campaign' (default): a managed tool
# changes recipe only through a staged campaign of minimum-run LOTs.
# 'switch_on_empty': a managed tool may change recipe once no work for its
# installed recipe remains, then keeps the new recipe for its minimum runs
# (the SMT2020 minimal run length); no campaigns are staged.
# 'group_least_setup': as switch_on_empty, but a tool keeps its installed recipe
# while a LOT of at least the asking LOT's priority waits for that recipe anywhere
# in the group (unassigned, or bound to a down tool) - the SMT2020 least-setup
# ranking over the tool group's queue (AutoSched rank_RSETUP with rule_LSSU).
SETUP_POLICIES = ('staged_campaign', 'switch_on_empty', 'group_least_setup')
SWITCH_POLICIES = ('switch_on_empty', 'group_least_setup')


class SetupCampaigns:
    def __init__(self, manufacturing, transfers, delivery_cost, emit, policy='staged_campaign'):
        if not callable(delivery_cost) or not callable(emit):
            raise ValueError('campaigns require delivery cost and event callbacks')
        if policy not in SETUP_POLICIES:
            raise ValueError('unknown setup policy')
        self.policy = policy
        self.manufacturing, self.transfers = manufacturing, transfers
        self.delivery_cost, self.emit = delivery_cost, emit
        self.records, self.active_by_tool, self.visit_owners = {}, {}, {}
        self.sequence = 0
        self.history_archive = None
        self.managed_tools = set()
        self._switch_cache = {}
        for tid, tool in manufacturing.tools.items():
            for route in manufacturing.rules.routes.values():
                for step in route['steps']:
                    if step['tool_group_id'] != tool['tool_group_id'] or not step.get('setup'):
                        continue
                    if any(t['new'] == step['setup']['id'] and t['minimum_runs'] > 0
                           for t in manufacturing.setups[tid].transitions):
                        if step.get('batch_min'):
                            raise ValueError('positive-minimum batch setup campaigns are unsupported')
                        self.managed_tools.add(tid)
        if getattr(manufacturing, 'campaigns', None) is not None:
            raise ValueError('manufacturing already has a campaign coordinator')
        manufacturing.campaigns = self
        self._dirty_tools=set(self.managed_tools);self._proposal_cache={}
        manufacturing.subscribe_resources(self.resource_changed)
        manufacturing.custody.subscribe_resources(self.resource_changed)

    def rebuild_derived(self):
        self._dirty_tools=set(self.managed_tools);self._proposal_cache={}

    def invalidate_tools(self, tool_ids=None):
        if tool_ids is None:ids=self.managed_tools
        else:
            ids=set(tool_ids)
            if ids-set(self.manufacturing.tools):ids=self.managed_tools
            else:ids=ids & self.managed_tools
        self._dirty_tools.update(ids)
        for tid in ids:self._proposal_cache.pop(tid,None)

    def resource_changed(self, change):
        if (change.slot_ids or change.carrier_ids or change.vehicle_ids or change.rail_ids
                or not (change.tool_ids or change.group_ids)):
            self.invalidate_tools();return
        groups=set(change.group_ids)
        for tid in change.tool_ids:
            if tid not in self.manufacturing.tools:self.invalidate_tools();return
            groups.add(self.manufacturing.tools[tid]['tool_group_id'])
        self.invalidate_tools(tid for tid in self.managed_tools
            if self.manufacturing.tools[tid]['tool_group_id'] in groups)

    def _dirty(self):
        self.invalidate_tools()
        scheduler = self.manufacturing.scheduler
        for name in ('admission', 'manufacturing', 'transfer'):
            if name in scheduler._passes:
                if name=='manufacturing':self.manufacturing.dirty()
                else:scheduler.mark_dirty(name)

    def _event(self, record):
        return {**record, 'members': [dict(member) for member in record['members']]}

    def _candidate(self, visit, tid):
        m = self.manufacturing
        lot = m.lots[visit['lot_id']]
        carrier = lot['carrier_id']
        if (visit['status'] != 'pending' or visit.get('execution_id') or visit.get('assembly_id')
                or visit['id'] in self.visit_owners or lot.get('admitted_at_us') is None
                or carrier in m.claims or lot.get('transfer_id')
                or tid not in m.rules.eligible_tools(visit['id'], [tid])):
            return None
        step = m._step(visit)
        if step['tool_group_id'] != m.tools[tid]['tool_group_id'] or step.get('batch_min'):
            return None
        state = m.setups[tid]
        if not step.get('setup') or step['setup']['id'] == state.current or not state.can_use(step):
            return None
        decision = state.decision(step)
        if decision['minimum_runs'] <= 0:
            return None
        origin = m.custody.locations.get(carrier)
        buffer_owner=getattr(m,'buffer_owner',lambda carrier,slot:None)(carrier,origin)
        if origin is None or m.custody.slots[origin]['reservation'] not in (None,buffer_owner):
            return None
        request = self.transfers.demands.get(self.transfers.active.get(carrier))
        if request and request['status'] != 'pending':
            return None
        if request and request.get('output_exchange_id') is not None:
            return None
        cost = self.delivery_cost(carrier, tid)
        if cost is None:
            return None
        # Upstream readiness is policy readiness for staging, not rack readiness.
        ranked = {**visit, 'queue_ready_at_us': visit.get('queue_ready_at_us')
                  if visit.get('queue_ready_at_us') is not None else visit['entered_at_us']}
        return {'visit': visit, 'carrier_id': carrier, 'origin': origin,
                'step': step, 'decision': decision, 'rank': queue_rank(ranked, step, state),
                'cost': Fraction(cost)}

    def _incoming_tools(self):
        # Tools with transport already committed under the installed recipe.
        t = self.transfers; incoming = set()
        for r in (t.demands[did] for did in t.active.values()):
            if (r['status'] not in ('pending', 'cancelled', 'reused')
                    and t.active.get(r['carrier_id']) == r['id']
                    and r.get('destination_id') in t.docks):
                incoming.add(t.docks[r['destination_id']].get('equipment_id'))
        return incoming

    def _proposals(self, tool_ids, incoming_tools=None):
        m = self.manufacturing
        proposals = []
        if self.policy in SWITCH_POLICIES:
            return proposals
        for tid in tool_ids:
            tool = m.tools[tid]
            if (tid in self.active_by_tool or not tool.get('available', True)
                    or tool.get('pm_pending') or m.causes[tid] or tool.get('assembly_id')):
                continue
            # Honor transport already committed under the installed recipe.
            if incoming_tools is None:incoming_tools = self._incoming_tools()
            if tid in incoming_tools:
                continue
            groups = {}
            for visit in m.rules.current_visits(tool['tool_group_id']):
                candidate = self._candidate(visit, tid)
                if candidate:
                    groups.setdefault(candidate['step']['setup']['id'], []).append(candidate)
            for target, candidates in groups.items():
                candidates.sort(key=lambda c: c['rank'])
                minimum = candidates[0]['decision']['minimum_runs']
                if len(candidates) < minimum:
                    continue
                chosen = candidates[:minimum]
                occupants = {c['origin']: c for c in chosen if c['origin'] in tool['rack_slots']}
                free = [sid for sid in sorted(tool['rack_slots']) if m.custody.available(sid)]
                if len(free) + len(occupants) < minimum:
                    continue
                remaining = iter(free)
                members = [{'visit_id': c['visit']['id'], 'carrier_id': c['carrier_id'],
                    'rack_slot_id': c['origin'] if c['origin'] in occupants else next(remaining),
                    'arrived': c['origin'] in occupants, 'completed': False} for c in chosen]
                rank = (chosen[0]['rank'], sum(c['cost'] for c in chosen), tid, target,
                        tuple(member['visit_id'] for member in members))
                proposals.append((rank, tid, target, members))
        return proposals

    def dispatch(self):
        m=self.manufacturing
        dirty=sorted(self._dirty_tools);self._dirty_tools.clear()
        incoming=self._incoming_tools() if dirty else None
        for tid in dirty:self._proposal_cache[tid]=self._proposals((tid,),incoming)
        # All surviving proposals still participate in the original global rank.
        proposals=[proposal for tid in sorted(self.managed_tools)
            for proposal in self._proposal_cache.get(tid,())]
        changed = False
        for _, tid, target, members in sorted(proposals):
            if tid in self.active_by_tool or any(x['visit_id'] in self.visit_owners for x in members):
                continue
            owner = f'setup-campaign:{self.sequence + 1:08}'
            destinations = [x['rack_slot_id'] for x in members if not x['arrived']]
            # Existing arrived members retain free physical custody, gated by membership.
            if not m.custody.reserve_many(destinations, owner):
                continue
            self.sequence += 1
            record = {'id': owner, 'tool_id': tid, 'setup_id': target,
                'status': 'ready' if all(x['arrived'] for x in members) else 'staging',
                'members': members, 'committed_at_us': m.scheduler.now_us,
                'completed_at_us': None}
            self.records[owner] = record
            self.active_by_tool[tid] = owner
            for member in members:
                self.visit_owners[member['visit_id']] = owner
            self.emit('setup_campaign_committed', self._event(record))
            self._dirty()
            changed = True
        return changed

    def admission(self, visit_id, tool_id):
        owner = self.visit_owners.get(visit_id)
        if owner:
            record = self.records[owner]
            if record['tool_id'] != tool_id:
                return None
            member = next(x for x in record['members'] if x['visit_id'] == visit_id)
            return (member['rack_slot_id'], owner) if not member['arrived'] else None
        return None

    def allows_admission(self, visit_id, tool_id):
        if tool_id not in self.managed_tools:
            return True
        owner = self.visit_owners.get(visit_id)
        if owner:
            return self.records[owner]['tool_id'] == tool_id
        m = self.manufacturing
        step = m._step(m.rules.visits[visit_id])
        target = (step.get('setup') or {}).get('id', m.setups[tool_id].current)
        if tool_id in self.active_by_tool:
            return False
        state = m.setups[tool_id]
        if self.policy in SWITCH_POLICIES:
            # A LOT already bound to this tool (on its rack or travelling to it)
            # stays admitted whatever the tool installs meanwhile; it waits on
            # the rack until the tool can serve it (can_commit).
            if self._bound(tool_id, visit_id):
                return True
            return state.can_use(step) and (target == state.current
                or self._switch_open(tool_id, target, visit_id))
        return state.can_use(step) and (target == state.current
            or state.decision(step)['minimum_runs'] == 0)

    def _bound(self, tool_id, visit_id):
        """The visit's carrier is on this tool's rack or travelling to it."""
        m = self.manufacturing; t = self.transfers
        carrier = m.lots[m.rules.visits[visit_id]['lot_id']]['carrier_id']
        if m.custody.locations.get(carrier) in m.tools[tool_id]['rack_slots']:
            return True
        demand = t.demands.get(t.active.get(carrier))
        return bool(demand and demand['status'] not in ('pending', 'cancelled')
                    and demand.get('destination_id') in t.docks
                    and t.docks[demand['destination_id']].get('equipment_id') == tool_id)

    def _installed_work_bound(self, tool_id, visit_id):
        m = self.manufacturing; current = m.setups[tool_id].current
        for visit in m.rules.current_visits(m.tools[tool_id]['tool_group_id']):
            if visit['id'] == visit_id or visit['status'] != 'pending' or visit.get('execution_id'):
                continue
            if ((m._step(visit).get('setup') or {}).get('id', current) == current
                    and self._bound(tool_id, visit['id'])):
                return True
        return False

    def _switch_open(self, tool_id, target, visit_id):
        """switch_on_empty: no work remains for the installed recipe at this tool,
        and no other recipe change is already queued here.

        A group visit belongs to this tool when its carrier is on this tool's rack
        or travelling to it. Unassigned visits (not on, or travelling to, any of
        the group's tools) count for the installed recipe unless another available
        tool of the group has the same recipe installed.

        A visit's verdict does not depend on the asking visit (except that the
        asking visit is skipped), so the first two blocking visits are cached per
        (tool, target, installed recipe) until custody or manufacturing state
        changes; transfer assignment asks this for every pending demand."""
        m = self.manufacturing
        current = m.setups[tool_id].current
        # group_least_setup: only LOTs of at least the asking priority hold the
        # recipe (a hot LOT may trigger a setup over regular same-recipe work).
        priority = m.rules.visits[visit_id]['priority'] if self.policy == 'group_least_setup' else None
        key = (tool_id, target, current, priority)
        epoch = (m.custody.version, m._wait_generation)
        cache = self._switch_cache
        if cache.get('epoch') != epoch:
            cache.clear();cache['epoch'] = epoch
        blockers = cache.get(key)
        if blockers is None:
            blockers = cache[key] = self._switch_blockers(tool_id, target, current, priority)
        return not blockers or (len(blockers) == 1 and blockers[0] == visit_id)

    def _switch_blockers(self, tool_id, target, current, priority=None):
        """Up to two group visits that keep the switch closed for this tool."""
        group_queue = self.policy == 'group_least_setup'
        m = self.manufacturing; t = self.transfers
        tool = m.tools[tool_id]; group = tool['tool_group_id']
        # Only the group's own tools can share the installed recipe; computed on
        # first need.
        shared = None
        def shares():
            index = getattr(m, 'resource_index', None)
            peers = (index.tools_by_group.get(group, ()) if index is not None
                     else [tid for tid, other in m.tools.items() if other['tool_group_id'] == group])
            return any(tid != tool_id and m.tools[tid].get('available', True)
                       and getattr(m.setups.get(tid), 'current', None) == current for tid in peers)
        blockers = []
        for visit in m.rules.current_visits(group):
            if visit['status'] != 'pending' or visit.get('execution_id'):
                continue
            lot = m.lots[visit['lot_id']]
            if lot.get('admitted_at_us') is None:
                continue
            step = m._step(visit)
            recipe = (step.get('setup') or {}).get('id', current)
            carrier = lot['carrier_id']
            slot = m.custody.slots.get(m.custody.locations.get(carrier), {})
            holder = slot.get('equipment_id') if slot.get('kind') == 'rack' else None
            demand = t.demands.get(t.active.get(carrier))
            if demand and demand['status'] not in ('pending', 'cancelled') and demand.get('destination_id') in t.docks:
                bound = t.docks[demand['destination_id']].get('equipment_id')
            else:
                bound = None
            if bound is not None and m.tools.get(bound, {}).get('tool_group_id') == group:
                owner = bound
            elif holder is not None and m.tools.get(holder, {}).get('tool_group_id') == group:
                owner = holder
            else:
                owner = None
            if owner is not None and owner != tool_id:
                # group_least_setup: a LOT bound to a down peer waits out its
                # downtime there; it still counts as group work for this recipe.
                if not group_queue or (m.tools[owner].get('available', True) and not m.causes[owner]):
                    continue
                owner = None
            if recipe == current:
                if owner == tool_id:
                    blocking = True
                elif group_queue:
                    blocking = (visit['priority'] >= priority
                                and tool_id in m.rules.eligible_tools(visit['id'], [tool_id]))
                else:
                    if shared is None:
                        shared = shares()
                    blocking = not shared and tool_id in m.rules.eligible_tools(visit['id'], [tool_id])
            else:
                blocking = owner == tool_id and recipe != target
            if blocking:
                blockers.append(visit['id'])
                if len(blockers) == 2:
                    break
        return tuple(blockers)

    def can_reside(self, visit_id, tool_id):
        owner = self.visit_owners.get(visit_id)
        if owner:
            return self.records[owner]['tool_id'] == tool_id
        return self.can_commit(visit_id, tool_id)

    def valid_admission(self, visit_id, tool_id, rack_id, owner):
        if owner and owner.startswith('setup-campaign:'):
            return self.admission(visit_id, tool_id) == (rack_id, owner)
        return self.allows_admission(visit_id, tool_id)

    def can_commit(self, visit_id, tool_id):
        if self.policy in SWITCH_POLICIES:
            if not self.allows_admission(visit_id, tool_id):
                return False
            # The change itself happens only when no LOT on this tool's rack or
            # travelling to it still needs the installed recipe: admissions can
            # race, so the empty-queue condition is rechecked at commit.
            m = self.manufacturing
            step = m._step(m.rules.visits[visit_id]); state = m.setups[tool_id]
            if not state.can_use(step):
                return False
            target = (step.get('setup') or {}).get('id', state.current)
            return target == state.current or not self._installed_work_bound(tool_id, visit_id)
        owner = self.visit_owners.get(visit_id)
        if owner:
            record = self.records[owner]
            return record['tool_id'] == tool_id and record['status'] in ('ready', 'serving')
        if tool_id in self.active_by_tool:
            # Drain already resident old-setup work before a staged change starts.
            record = self.records[self.active_by_tool[tool_id]]
            m = self.manufacturing
            step = m._step(m.rules.visits[visit_id])
            return record['status'] == 'staging' and (step.get('setup') or {}).get('id',
                m.setups[tool_id].current) == m.setups[tool_id].current
        return self.allows_admission(visit_id, tool_id)

    def committed(self, visit_id, tool_id):
        owner = self.visit_owners.get(visit_id)
        if not owner:
            return
        record = self.records[owner]
        if not self.can_commit(visit_id, tool_id):
            raise RuntimeError('setup campaign committed before physical readiness')
        if record['status'] == 'ready':
            record['status'] = 'serving'
            self.emit('setup_campaign_serving', self._event(record))

    def arrived(self, visit_id, rack_slot_id):
        owner = self.visit_owners.get(visit_id)
        if not owner:
            return
        record = self.records[owner]
        member = next(x for x in record['members'] if x['visit_id'] == visit_id)
        if (member['arrived'] or member['rack_slot_id'] != rack_slot_id
                or self.manufacturing.custody.locations.get(member['carrier_id']) != rack_slot_id):
            raise RuntimeError('setup campaign arrival ownership changed')
        member['arrived'] = True
        if all(x['arrived'] for x in record['members']):
            record['status'] = 'ready'
            self.emit('setup_campaign_ready', self._event(record))
        self._dirty()

    def completed(self, visit_id, tool_id):
        owner = self.visit_owners.get(visit_id)
        if not owner:
            return
        record = self.records[owner]
        member = next(x for x in record['members'] if x['visit_id'] == visit_id)
        if member['completed'] or record['tool_id'] != tool_id or record['status'] != 'serving':
            raise RuntimeError('setup campaign completion ownership changed')
        member['completed'] = True
        if all(x['completed'] for x in record['members']):
            record['status'] = 'completed'
            record['completed_at_us'] = self.manufacturing.scheduler.now_us
            del self.active_by_tool[tool_id]
            for item in record['members']:
                del self.visit_owners[item['visit_id']]
            self.emit('setup_campaign_completed', self._event(record))
        self._dirty()

    def drained(self, tool_id):
        return tool_id not in self.active_by_tool

    def describe(self):
        return [self._event(self.records[owner]) for _, owner in sorted(self.active_by_tool.items())]
