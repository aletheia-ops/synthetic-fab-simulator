"""Source route visits and setup rules, independent of physical dispatch.

These primitives consume prepared source records. They do not grant custody,
reserve processing capacity, or establish physical completion on their own.
"""
from .service import Cascade, dispatch_service, service_dispatch_required
from .native_actions import native_action

from fractions import Fraction

BATCH_POLICY = 'finite_root_remainder_fifo_v1'


_UNSET=object()


class SetupState:
    """Installed recipe and credits earned since the last actual change."""
    def __init__(self, current, transitions):
        self.current = current
        self.transitions = tuple(dict(t) for t in transitions)
        self.minimum_runs = 0
        self.completed_lots = 0

    def decision(self, step):
        setup = step.get('setup')
        if setup and setup['when'] not in ('need', 'always'):
            raise ValueError('unsupported setup WHEN')
        target = setup['id'] if setup else self.current
        required = bool(setup and (setup['when'] == 'always' or target != self.current))
        result = {'required': required, 'current': self.current, 'new': target,
                  'minimum_runs': 0, 'duration_us': 0, 'expected_us': Fraction(0),
                  'distribution': None}
        if not required:
            return result
        if setup['when'] not in ('need', 'always'):
            raise ValueError('unsupported setup WHEN')
        matches = [t for t in self.transitions if t['current'] == self.current and t['new'] == target]
        if not matches:
            matches = [t for t in self.transitions if t['current'] is None and t['new'] == target]
        if len(matches) > 1:
            raise ValueError('ambiguous setup transition')
        if matches:
            t = matches[0]
            result.update(duration_us=t['duration_us'], expected_us=Fraction(t['duration_us']),
                          minimum_runs=t['minimum_runs'])
        elif setup.get('fallback'):
            definition = dict(setup['fallback'])
            result.update(distribution=definition, duration_us=None,
                          expected_us=Fraction(definition['mean_seconds']) * 1_000_000)
        else:
            raise ValueError('missing required setup transition')
        return result

    def can_use(self, step):
        target = step['setup']['id'] if step.get('setup') else self.current
        return target == self.current or self.completed_lots >= self.minimum_runs

    def apply(self, decision):
        if decision['current'] != self.current:
            raise RuntimeError('stale setup decision')
        if not decision['required']:
            return
        if decision['new'] != self.current:
            if self.completed_lots < self.minimum_runs:
                raise RuntimeError('setup minimum-run credits not satisfied')
            self.current = decision['new']
            self.minimum_runs = decision['minimum_runs']
            self.completed_lots = 0

    def credit(self, lot_count):
        if type(lot_count) is not int or lot_count <= 0:
            raise ValueError('completed LOT credit must be positive')
        self.completed_lots += lot_count


def queue_rank(visit, step, setup):
    """Rank only already feasible candidates; never sample expected setup."""
    ready = visit.get('queue_ready_at_us')
    if type(ready) is not int or ready < 0:
        raise ValueError('candidate must have a physical ready time')
    return (-visit['priority'], setup.decision(step)['expected_us'], ready, visit['id'])


class RouteRules:
    def __init__(self, source, scheduler, streams, lots, emit):
        self.routes = {r['id']: r for r in source['model']['routes']}
        self.steps = {rid: {s['source_step_id']: s for s in route['steps']}
                      for rid, route in self.routes.items()}
        self.scheduler, self.streams, self.lots, self.emit = scheduler, streams, lots, emit
        self.resource_changed=lambda lot_id:None
        self.visits, self.clocks = {}, {}
        self.clock_archive=None
        # [event_id, clock_id] of archived violated clocks, in event_id (creation) order,
        # so assessments keep reporting them after the bodies are archived.
        self.retired_violation_ids=[]
        # Ordered live ownership; terminal visits remain in the evidence ledger.
        self._active_visits = {}
        self._active_by_group = {}
        self._live_order={};self._next_live_order=0
        self.violation_count = 0
        self._sequence = 0
        # Read-only source graph and observational reachability cache. No draws
        # or route decisions are made while assessing possible batch supply.
        self._possible_next, self._predecessors, self._batch_reaches = {}, {}, {}
        for rid, route in self.routes.items():
            successors = {}
            for index, step in enumerate(route['steps']):
                if step['kind'] == 'transport_abstraction':
                    targets = {step['replacement']['next_executable_step_id']}
                else:
                    targets = {route['steps'][index + 1]['source_step_id']} if index + 1 < len(route['steps']) else set()
                    if step.get('rework') and Fraction(step['rework']['probability']) > 0:
                        targets.add(step['rework']['target_step_id'])
                successors[step['source_step_id']] = targets - {None}
            predecessors = {step: set() for step in successors}
            for origin, targets in successors.items():
                for target in targets:
                    predecessors[target].add(origin)
            self._possible_next[rid], self._predecessors[rid] = successors, predecessors
        self._deadline_kind = 'route:cqt_deadline'
        scheduler.register(self._deadline_kind, lambda payload: None)
        scheduler.add_deadline_check(self._check_deadlines)

    def can_supply_batch(self, lot_id, target_step_id):
        """Conservative possibility of another uncommitted matching visit.

        Ignore timing/capacity and retain all positive-probability rework paths.
        A committed/currently processing member has consumed its current visit;
        once processing chooses a successor, use that recorded choice instead.
        """
        lot = self.lots[lot_id]
        rid = lot['route_id']
        key = rid, target_step_id
        if key not in self._batch_reaches:
            reached = {target_step_id}
            pending = [target_step_id]
            while pending:
                for previous in self._predecessors[rid][pending.pop()]:
                    if previous not in reached:
                        reached.add(previous)
                        pending.append(previous)
            self._batch_reaches[key] = reached
        visit_id = lot.get('current_visit_id')
        if visit_id is None:
            return False
        visit = self.visits[visit_id]
        if visit['status'] == 'pending' and not visit.get('assembly_id'):
            starts = {visit['source_step_id']}
        elif visit['status'] in ('pending', 'processing'):
            starts = self._possible_next[rid][visit['source_step_id']]
        elif visit['status'] in ('processed', 'unloaded'):
            starts = {visit['next_source_step_id']}
        else:
            raise ValueError('unsupported visit status for batch supply assessment')
        return bool(starts & self._batch_reaches[key])

    def _next_step(self, route_id, step_id):
        steps = self.routes[route_id]['steps']
        index = next(i for i, s in enumerate(steps) if s['source_step_id'] == step_id)
        return steps[index + 1]['source_step_id'] if index + 1 < len(steps) else None

    def enter_visit(self, lot_id, source_step_id):
        lot = self.lots[lot_id]
        lot.setdefault('dedication', {})
        lot.setdefault('route_history', [])
        previous = lot.get('current_visit_id')
        if previous and self.visits[previous]['status'] != 'unloaded':
            raise RuntimeError('LOT already has an active visit')
        candidate = source_step_id
        seen = set()
        while candidate is not None:
            if candidate in seen:
                raise RuntimeError('cyclic abstraction mapping')
            seen.add(candidate)
            step = self.steps[lot['route_id']][candidate]
            if step['kind'] == 'transport_abstraction':
                self.emit('transport_abstraction_replaced', {'lot_id': lot_id, 'source_step_id': candidate})
                candidate = step['replacement']['next_executable_step_id']
                continue
            probability = Fraction(step.get('sampling_probability') or '1')
            execute = probability == 1 or (probability > 0 and
                      self.streams.get('processing', lot_id).random() < probability)
            if execute:
                break
            self.emit('visit_skipped', {'lot_id': lot_id, 'source_step_id': candidate})
            candidate = self._next_step(lot['route_id'], candidate)
        if candidate is None:
            lot['current_visit_id'] = None
            lot['route_complete_at_us'] = self.scheduler.now_us
            self.emit('route_completed', {'lot_id': lot_id})
            self.resource_changed(lot_id)
            return None
        visit_id = f'{lot_id}:visit:{len(lot["route_history"]) + 1}'
        self.visits[visit_id] = {'id': visit_id, 'lot_id': lot_id, 'source_step_id': candidate,
                                 'status': 'pending', 'priority': lot['priority'],
                                 'queue_ready_at_us': None, 'entered_at_us': self.scheduler.now_us,
                                 'processing_started_at_us': None, 'processing_completed_at_us': None}
        lot['current_visit_id'] = visit_id
        lot['route_history'].append(visit_id)
        self._active_visits[visit_id] = self.visits[visit_id]
        self._live_order[visit_id]=self._next_live_order;self._next_live_order+=1
        self._active_by_group.setdefault(step['tool_group_id'], {})[visit_id] = self.visits[visit_id]
        self.emit('visit_entered', dict(self.visits[visit_id]))
        self.resource_changed(lot_id)
        return visit_id

    def current_visits(self, tool_group_id=None):
        """Snapshot visits not yet unloaded, in original insertion order."""
        current=(self._active_visits if tool_group_id is None
                 else self._active_by_group.get(tool_group_id, {}))
        return tuple(current.values())

    def _components(self, visit):
        lot = self.lots[visit['lot_id']]
        return [c for c in self.routes[lot['route_id']]['dedication_components']
                if visit['source_step_id'] in c['source_step_ids']]

    def eligible_tools(self, visit_id, tool_ids):
        visit = self.visits[visit_id]
        bindings = self.lots[visit['lot_id']]['dedication']
        bound = {bindings[c['id']] for c in self._components(visit) if c['id'] in bindings}
        if len(bound) > 1:
            raise RuntimeError('conflicting dedication bindings')
        # Copy native sequences directly when no dedication predicate applies.
        # Custom iterables retain their original iteration/length-hint contract.
        if not bound and type(tool_ids) in (list,tuple):return list(tool_ids)
        return [tool for tool in tool_ids if not bound or tool in bound]

    def started(self, visit_id, tool_id):
        visit = self.visits[visit_id]
        if visit['status'] != 'pending':
            raise RuntimeError('visit cannot start twice or after completion')
        if not isinstance(tool_id, str) or not tool_id:
            raise ValueError('tool ID required')
        if not self.eligible_tools(visit_id, [tool_id]):
            raise RuntimeError('conflicting dedication tool')
        lot = self.lots[visit['lot_id']]
        for component in self._components(visit):
            lot['dedication'][component['id']] = tool_id
        visit.update(status='processing', tool_id=tool_id,
                     processing_started_at_us=self.scheduler.now_us)
        for clock in self.clocks.values():
            if clock['lot_id'] == visit['lot_id'] and clock['target_step_id'] == visit['source_step_id'] and clock['closed_at_us'] is None:
                if self.scheduler.now_us > clock['deadline_us']:
                    self._violate(clock)
                clock['closed_at_us'] = self.scheduler.now_us
                self.scheduler.cancel(clock['event_id'])
        self.emit('processing_started', dict(visit))
        self.resource_changed(visit['lot_id'])

    def processing_complete(self, visit_id):
        visit = self.visits[visit_id]
        if visit['status'] != 'processing':
            raise RuntimeError('visit must be processing to complete')
        lot = self.lots[visit['lot_id']]
        step = self.steps[lot['route_id']][visit['source_step_id']]
        next_step = self._next_step(lot['route_id'], visit['source_step_id'])
        rework = step.get('rework')
        if rework and Fraction(rework['probability']) > 0:
            if self.streams.get('processing', visit['lot_id']).random() < Fraction(rework['probability']):
                next_step = rework['target_step_id']
        visit.update(status='processed', processing_completed_at_us=self.scheduler.now_us,
                     next_source_step_id=next_step)
        if step.get('cqt'):
            definition = step['cqt']
            clock_id = visit_id + ':cqt'
            deadline = self.scheduler.now_us + definition['threshold_us']
            event_id = self.scheduler.schedule(deadline + 1, 3, self._deadline_kind, {'clock_id': clock_id})
            self.clocks[clock_id] = {'id': clock_id, 'lot_id': visit['lot_id'],
                                    'declaring_visit_id': visit_id, 'target_step_id': definition['target_step_id'],
                                    'deadline_us': deadline, 'closed_at_us': None, 'violated': False,
                                    'event_id': event_id}
        self.emit('processing_completed', dict(visit))
        self.resource_changed(visit['lot_id'])

    def after_unload(self, visit_id):
        visit = self.visits[visit_id]
        if visit['status'] != 'processed':
            raise RuntimeError('visit must complete processing before unload')
        visit.update(status='unloaded', unloaded_at_us=self.scheduler.now_us)
        del self._active_visits[visit_id]
        del self._live_order[visit_id]
        group=self.steps[self.lots[visit['lot_id']]['route_id']][visit['source_step_id']]['tool_group_id']
        del self._active_by_group[group][visit_id]
        self.emit('unload_completed', dict(visit))
        self.resource_changed(visit['lot_id'])
        return self.enter_visit(visit['lot_id'], visit['next_source_step_id'])

    def _violate(self, clock):
        if clock['violated']:
            return
        clock.update(violated=True, violated_at_us=self.scheduler.now_us,
                     lateness_us=self.scheduler.now_us - clock['deadline_us'])
        self.violation_count += 1
        self.emit('cqt_violation', dict(clock))

    def _check_deadlines(self):
        for clock in self.clocks.values():
            if clock['closed_at_us'] is None and self.scheduler.now_us > clock['deadline_us']:
                self._violate(clock)


class BatchAssembly:
    """Commit finite, physically admitted members before their inbound transport.

    tools is runtime state, with prepared group IDs and explicit custody rack
    slot IDs. delivery_cost returns exact directed free-flow seconds or None
    for unreachable carriers. Service/transfer controllers invoke milestones;
    this component never moves a carrier or samples a duration.
    """
    def __init__(self, rules, custody, tools, setup_states, delivery_cost, emit, eligible_tool=lambda tid:True):
        self.rules, self.custody, self.tools = rules, custody, tools
        self.setup_states, self.delivery_cost, self.emit = setup_states, delivery_cost, emit
        self.eligible_tool = eligible_tool
        self._group_tools = None; self._group_tools_key = None
        self.exact_population = lambda lot,step:None
        self.held_out = lambda tid,lot:False
        self.super_hot_expedite = False
        self.assemblies = {};self.history_archive=None
        self._sequence = 0
        seen = set()
        for tid, tool in tools.items():
            if tool['id'] != tid or tid not in setup_states:
                raise ValueError('invalid batch tool/setup identity')
            slots = tool['rack_slots']
            if not slots or len(slots) != len(set(slots)) or seen.intersection(slots):
                raise ValueError('invalid/duplicate batch rack slots')
            for sid in slots:
                slot = custody.slots[sid]
                if slot['kind'] != 'rack' or slot['equipment_id'] != tid:
                    raise ValueError('batch rack belongs to another tool')
            seen.update(slots)

    def _candidate_base(self, visit):
        # Tool-independent part of _candidate: None, or (lot, carrier, origin, step).
        lot = self.rules.lots[visit['lot_id']]
        carrier = lot.get('carrier_id')
        origin = self.custody.locations.get(carrier)
        if (visit['status'] != 'pending' or visit.get('assembly_id')
                or visit.get('execution_id') or lot.get('transfer_id')
                or lot.get('admitted_at_us') is None or origin is None
                or type(visit.get('queue_ready_at_us')) is not int):
            return None
        slot = self.custody.slots[origin]
        buffer_owner=getattr(self,'buffer_owner',lambda carrier,slot:None)(carrier,origin)
        if slot['kind'] not in ('entry', 'storage', 'port', 'rack', 'track_buffer') or slot['reservation'] not in (None,buffer_owner):
            return None
        step = self.rules.steps[lot['route_id']][visit['source_step_id']]
        if not step.get('batch_min'):return None
        return lot, carrier, origin, step

    def _candidate(self, visit, tid, _bases=None):
        if _bases is None:base=self._candidate_base(visit)
        else:
            base=_bases.get(visit['id'],_UNSET)
            if base is _UNSET:base=_bases[visit['id']]=self._candidate_base(visit)
        if base is None:return None
        lot, carrier, origin, step = base
        if (step['tool_group_id'] != self.tools[tid]['tool_group_id']
                or tid not in self.rules.eligible_tools(visit['id'], [tid])
                or not self.setup_states[tid].can_use(step)):
            return None
        if lot['wafer_count'] != 25:
            raise ValueError('batch assembly requires whole 25-wafer LOTs')
        cost = Fraction(0) if origin in self.tools[tid]['rack_slots'] else self.delivery_cost(carrier, tid)
        if cost is None:
            return None
        cost = Fraction(cost)
        if cost < 0:
            raise ValueError('negative directed delivery cost')
        return {'visit_id': visit['id'], 'lot_id': visit['lot_id'], 'carrier_id': carrier,
                'origin_id': origin, 'cost': cost, 'step': step,
                'rank': queue_rank(visit, step, self.setup_states[tid])}

    def _tools_by_group(self):
        key=(id(self.tools),len(self.tools))
        if self._group_tools_key!=key:
            index={}
            for tid in sorted(self.tools):index.setdefault(self.tools[tid]['tool_group_id'],[]).append(tid)
            self._group_tools=index;self._group_tools_key=key
        return self._group_tools

    def _proposals(self, tool_groups=None):
        if tool_groups is None:tool_ids=sorted(self.tools)
        else:
            by_group=self._tools_by_group()
            tool_ids=sorted(tid for group in tool_groups for tid in by_group.get(group,()))
        bases={}
        for tid in tool_ids:
            tool = self.tools[tid]
            if tool_groups is not None and tool['tool_group_id'] not in tool_groups:
                continue
            if not self.eligible_tool(tid) or not tool.get('available', True) or tool.get('pm_pending') or tool.get('assembly_id') or tool.get('active_execution_id'):
                continue
            groups = {}
            for visit in self.rules.current_visits(tool['tool_group_id']):
                candidate = self._candidate(visit, tid, bases)
                if candidate is None:
                    continue
                lot = self.rules.lots[visit['lot_id']]
                if self.held_out(tid, lot):
                    continue
                step = candidate['step']
                setup = step.get('setup')
                key = (lot['product_id'], lot['route_id'], visit['source_step_id'],
                       setup['id'] if setup else None)
                groups.setdefault(key, []).append(candidate)
            racks = sorted(tool['rack_slots'])
            for candidates in groups.values():
                candidates.sort(key=lambda c: c['rank'])
                step = candidates[0]['step']
                # Source minima/maxima are wafer capacities; only whole LOTs fit.
                minimum = (step['batch_min'] + 24) // 25
                if self.super_hot_expedite and any(self.rules.lots[c['lot_id']].get('super_hot') for c in candidates):
                    minimum = 1  # A super-hot LOT does not wait for batch partners.
                maximum = min(step['batch_max'] // 25, len(racks))
                free = [sid for sid in racks if self.custody.available(sid)]
                occupants = {self.custody.slots[sid]['carrier_id']: sid for sid in racks
                             if self.custody.slots[sid]['carrier_id'] is not None}
                count = min(len(candidates), maximum, len(free) + len(occupants))
                population = self.exact_population(self.rules.lots[candidates[0]['lot_id']], step)
                if population is not None and (population+maximum-1)//maximum <= population//minimum:
                    # A remainder R is partitionable iff some number of batches
                    # fits between ceil(R/maximum) and floor(R/minimum).
                    count = next((size for size in range(count, minimum-1, -1)
                        if population >= size and
                        (population-size+maximum-1)//maximum <= (population-size)//minimum), 0)
                members = candidates[:count]
                if count < minimum or not set(occupants).issubset({m['carrier_id'] for m in members}):
                    continue
                assigned = []
                available = iter(free)
                for member in members:
                    sid = occupants.get(member['carrier_id'])
                    sid = sid if sid is not None else next(available)
                    assigned.append({'visit_id': member['visit_id'], 'lot_id': member['lot_id'],
                                     'carrier_id': member['carrier_id'], 'rack_slot_id': sid,
                                     'arrived': member['carrier_id'] in occupants, 'departed': False})
                rank = (members[0]['rank'], sum(m['cost'] for m in members), tid,
                        tuple(m['visit_id'] for m in members))
                yield rank, tid, assigned

    def commit(self, *, groups=None):
        proposals = sorted(self._proposals(groups), key=lambda p: p[0])
        for rank, tid, members in proposals:
            aid = f'batch:{self._sequence + 1}'
            destinations = [m['rack_slot_id'] for m in members if not m['arrived']]
            origins = {m['rack_slot_id']: m['carrier_id'] for m in members if m['arrived']}
            with self.custody.batch_notifications():
                if not self.custody.reserve_many(destinations, aid, origins=origins):
                    continue
                assembly = {'id': aid, 'tool_id': tid, 'members': members, 'status': 'assembling',
                            'committed_at_us': self.rules.scheduler.now_us,
                            'processing_started_at_us': None, 'processing_completed_at_us': None}
                self.assemblies[aid] = assembly
                self.tools[tid]['assembly_id'] = aid
                for member in members:
                    self.rules.visits[member['visit_id']]['assembly_id'] = aid
                    if member['arrived']:
                        self.custody.release(member['rack_slot_id'], aid)
                self._sequence += 1
            self.emit('batch_committed', self._record(assembly))
            return aid
        return None

    def _record(self, assembly):
        return {**assembly, 'members': [dict(m) for m in assembly['members']]}

    def _member(self, carrier_id, status):
        matches = [(a, m) for a in self.assemblies.values() if a['status'] == status
                   for m in a['members'] if m['carrier_id'] == carrier_id and not m['departed']]
        if len(matches) != 1:
            raise ValueError('carrier must belong to one active assembly')
        return matches[0]

    def arrived(self, carrier_id, rack_slot_id):
        assembly, member = self._member(carrier_id, 'assembling')
        if (assembly['status'] != 'assembling' or member['arrived']
                or member['rack_slot_id'] != rack_slot_id
                or self.custody.locations.get(carrier_id) != rack_slot_id
                or self.custody.slots[rack_slot_id]['carrier_id'] != carrier_id
                or self.custody.slots[rack_slot_id]['reservation'] is not None):
            raise ValueError('batch arrival requires matching completed custody exchange')
        member['arrived'] = True
        self.emit('batch_member_arrived', {'assembly_id': assembly['id'], **member})

    def ready(self, assembly_id, rack_owner=None):
        a = self.assemblies[assembly_id]
        return a['status'] == 'assembling' and all(
            m['arrived'] and self.custody.locations.get(m['carrier_id']) == m['rack_slot_id']
            and self.custody.slots[m['rack_slot_id']]['reservation'] in (None, rack_owner) for m in a['members'])

    def _owned_tool(self, assembly, active_states):
        tool = self.tools[assembly['tool_id']]
        if (tool.get('assembly_id') != assembly['id']
                or tool.get('active_execution_id') not in active_states):
            raise RuntimeError('batch tool ownership changed')
        return tool

    def _members_in(self, assembly, status):
        return all(self.rules.visits[m['visit_id']]['status'] == status
                   and self.rules.visits[m['visit_id']].get('assembly_id') == assembly['id']
                   and self.rules.lots[m['lot_id']]['current_visit_id'] == m['visit_id']
                   for m in assembly['members'])

    def started(self, assembly_id, rack_owner=None):
        a = self.assemblies[assembly_id]
        tool = self._owned_tool(a, (None, assembly_id))
        if not self.ready(assembly_id, rack_owner) or not tool.get('available', True):
            raise RuntimeError('batch is not physically ready/available')
        if not self._members_in(a, 'pending'):
            raise RuntimeError('batch start members are inconsistent')
        # Preflight every start before establishing any dedication binding.
        for member in a['members']:
            visit = self.rules.visits[member['visit_id']]
            lot = self.rules.lots[member['lot_id']]
            step = self.rules.steps[lot['route_id']][visit['source_step_id']]
            setup = self.setup_states[a['tool_id']]
            if (visit['status'] != 'pending' or a['tool_id'] not in self.rules.eligible_tools(visit['id'], [a['tool_id']])
                    or not setup.can_use(step) or (step.get('setup') and step['setup']['id'] != setup.current)):
                raise RuntimeError('batch start conflicts with visit/dedication/setup state')
        a.update(status='processing', processing_started_at_us=self.rules.scheduler.now_us)
        tool['active_execution_id'] = assembly_id
        for member in a['members']:
            self.rules.started(member['visit_id'], a['tool_id'])
        self.emit('batch_processing_started', self._record(a))

    def finished(self, assembly_id):
        a = self.assemblies[assembly_id]
        if a['status'] != 'processing':
            raise RuntimeError('batch must be processing to finish')
        self._owned_tool(a, (assembly_id,))
        if not self._members_in(a, 'processing'):
            raise RuntimeError('batch processing members are inconsistent')
        for member in a['members']:
            self.rules.processing_complete(member['visit_id'])
        self.setup_states[a['tool_id']].credit(len(a['members']))
        a.update(status='processed', processing_completed_at_us=self.rules.scheduler.now_us)
        self.emit('batch_processing_completed', self._record(a))

    def unloaded(self, assembly_id):
        a = self.assemblies[assembly_id]
        if a['status'] != 'processed':
            raise RuntimeError('batch must finish processing before unload')
        self._owned_tool(a, (assembly_id,))
        if not self._members_in(a, 'processed'):
            raise RuntimeError('batch unload members are inconsistent')
        for member in a['members']:
            self.rules.after_unload(member['visit_id'])
        a.update(status='finished', unloaded_at_us=self.rules.scheduler.now_us)
        self.tools[a['tool_id']]['active_execution_id'] = None
        self.emit('batch_unload_completed', self._record(a))

    def departed(self, carrier_id):
        a, member = self._member(carrier_id, 'finished')
        if a['status'] != 'finished' or self.custody.locations.get(carrier_id) in self.tools[a['tool_id']]['rack_slots']:
            raise ValueError('batch departure requires unload and actual rack clearance')
        member['departed'] = True
        if all(m['departed'] for m in a['members']):
            a.update(status='cleared', cleared_at_us=self.rules.scheduler.now_us)
            self.tools[a['tool_id']]['assembly_id'] = None
            self.emit('batch_cleared', self._record(a))


class Manufacturing:
    """Physical ordinary, batch and two-slot cascade manufacturing lifecycle.

    Runtime tools declare prepared group IDs and finite rack slot IDs. Transfers
    move carriers through Custody, then notify delivered; this controller never
    invents transport or frees a finished carrier's physical location.
    """
    def __init__(self, source, scheduler, streams, lots, custody, tools, emit, *, delivery_cost=None):
        from .service import SerializedHandler, Cascade
        from .resource_changes import DirtyResources
        self.scheduler, self.streams, self.lots = scheduler, streams, lots
        self.custody, self.tools, self.emit = custody, tools, emit
        self.rules = RouteRules(source, scheduler, streams, lots, emit)
        self._resource_listeners=[]
        self._wait_generation=0
        self._dirty_groups=DirtyResources()
        # Tools whose processed-wafer counter changed (None: check every tool).
        self.processed_changed=None
        self.rules.resource_changed=self._lot_changed
        self.groups = {g['id']: g for g in source['model']['tool_groups']}
        self.setups, self.handlers, self.claims, self.executions = {}, {}, {}, {}
        self.executions_archive=None
        self._active_execution_count = 0
        self.campaigns = None
        # Super-hot reservation (declared project policy, docs/settings.md):
        # None, or a callable returning the tools that are destinations of a
        # super-hot LOT in transfer; such a tool starts no other LOT.
        self.super_hot_holds = None
        # Artificial extra processing (docs/settings.md): None or ExtraProcessing.
        self.extra_processing = None
        self.causes = {tid: set() for tid in tools}
        self.dispatch_guards = []
        self.pipelines = {}
        self.cascade_executions = {tid: set() for tid in tools}
        seen = set()
        for tid, tool in tools.items():
            group = self.groups[tool['tool_group_id']]
            slots = tool['rack_slots']
            if tool['id'] != tid or not slots or len(slots) != len(set(slots)) or seen.intersection(slots):
                raise ValueError('invalid tool/rack identity')
            for sid in slots:
                slot = custody.slots[sid]
                if slot['kind'] != 'rack' or slot['equipment_id'] != tid:
                    raise ValueError('rack belongs to another tool')
            seen.update(slots)
            tool.setdefault('active_execution_id', None)
            tool.setdefault('processed_wafers', 0)
            self.setups[tid] = SetupState(tool.get('initial_setup', group['initial_setup']),
                                          source['model'].get('setup_transitions', []))
            self.handlers[tid] = SerializedHandler(scheduler, 'manufacturing:'+tid,
                                                   self.claims, lambda tid=tid:self.tool_changed(tid), emit)
        for tid, tool in tools.items():
            steps = [step for route in source['model']['routes'] for step in route['steps']
                     if step.get('tool_group_id') == tool['tool_group_id'] and step.get('cascade_interval_us')]
            if steps:
                modes = {step['processing_unit'] for step in steps}
                if len(modes) != 1 or not modes.issubset({'lot', 'wafer'}):
                    raise ValueError('cascade tool requires one supported processing unit')
                mode = next(iter(modes))
                if len(tool['rack_slots']) < (2 if mode == 'lot' else 1):
                    raise ValueError('insufficient cascade rack capacity')
                self.pipelines[tid] = Cascade(scheduler, 'pipeline:'+tid, mode, self.setups[tid].current,
                                              lambda tid=tid:self.tool_changed(tid), emit)
                tool['cascade_execution_ids'] = self.cascade_executions[tid]
        self.batches = BatchAssembly(self.rules, custody, tools, self.setups,
                                     delivery_cost or (lambda carrier, tid:None), emit,
                                     eligible_tool=lambda tid:not self.causes[tid] and tid not in self.pipelines)
        self.batches.held_out = self._held_out
        scheduler.register_pass('manufacturing', self.dispatch)
        custody.subscribe_resources(self.resource_changed)

    def dirty(self):
        self._dirty_groups.mark();self.processed_changed=None
        self.scheduler.mark_dirty('manufacturing')

    def _processed(self,tid):
        if self.processed_changed is not None:self.processed_changed.add(tid)

    def resource_changed(self,change):
        index=getattr(self,'resource_index',None)
        if index is None:
            self.dirty();return
        groups=set(change.group_ids)
        for tid in change.tool_ids:
            if tid not in index.group_by_tool:
                self.dirty();return
            groups.add(index.group_by_tool[tid])
        for sid in change.slot_ids:
            tid=index.tool_by_slot.get(sid)
            if tid is None:
                slot=self.custody.slots.get(sid) or {}
                kind=slot.get('kind')
                # A vehicle's carrier slot concerns only the carriers it moved
                # (listed in carrier_ids); a port concerns its tool's group.
                if kind=='vehicle':continue
                if kind=='port' and slot.get('equipment_id') in index.group_by_tool:
                    groups.add(index.group_by_tool[slot['equipment_id']]);continue
                # Shared entry/clearance/buffer slots can change global batch or
                # campaign feasibility. Keep their contender frontier broad.
                self.dirty();return
            groups.add(index.group_by_tool[tid])
        lookup=getattr(self,'carrier_lot_lookup',None)
        if change.carrier_ids and lookup is None:
            self.dirty();return
        for carrier in change.carrier_ids:
            try:lot=lookup(carrier)[1]
            except ValueError:
                self.dirty();return
            vid=lot.get('current_visit_id')
            if vid:groups.add(self._step(self.rules.visits[vid])['tool_group_id'])
        if groups-set(index.tools_by_group) or change.vehicle_ids or change.rail_ids:
            self.dirty();return
        if groups:
            self._dirty_groups.mark(groups);self.scheduler.mark_dirty('manufacturing')

    def tool_changed(self,tid):
        from .resource_changes import ResourceChange
        self._publish_resources(ResourceChange(tool_ids=frozenset({tid})))

    def subscribe_resources(self,listener):
        if not callable(listener):raise ValueError('manufacturing resource listener must be callable')
        self._resource_listeners.append(listener)

    def _publish_resources(self,change):
        self._wait_generation+=1
        self.resource_changed(change)
        for listener in tuple(self._resource_listeners):listener(change)

    def _lot_changed(self,lot_id):
        from .resource_changes import ResourceChange
        lot=self.lots[lot_id];carrier=lot.get('carrier_id');vid=lot.get('current_visit_id')
        group=self._step(self.rules.visits[vid])['tool_group_id'] if vid else None
        self._publish_resources(ResourceChange(carrier_ids=frozenset({carrier}) if carrier else frozenset(),
            group_ids=frozenset({group}) if group else frozenset()))

    def _execution_changed(self,e):
        from .resource_changes import ResourceChange
        tid=e['tool_id']
        self._publish_resources(ResourceChange(carrier_ids=frozenset(self._carriers(e)),
            tool_ids=frozenset({tid}),group_ids=frozenset({self.tools[tid]['tool_group_id']})))

    def enter_visit(self, lot_id, source_step_id):
        vid = self.rules.enter_visit(lot_id, source_step_id)
        if vid:
            self._visit_ready(vid)
        if vid:self._lot_changed(lot_id)
        return vid

    def _step(self, visit):
        lot = self.lots[visit['lot_id']]
        return self.rules.steps[lot['route_id']][visit['source_step_id']]

    def _rack_ready(self, vid):
        visit = self.rules.visits[vid]
        carrier = self.lots[visit['lot_id']].get('carrier_id')
        sid = self.custody.locations.get(carrier)
        if sid is None:
            return False
        slot = self.custody.slots[sid]
        tid = slot.get('equipment_id')
        if (slot['kind'] != 'rack' or tid not in self.tools
                or sid not in self.tools[tid]['rack_slots']
                or slot['carrier_id'] != carrier or slot['reservation'] is not None
                or self.tools[tid]['tool_group_id'] != self._step(visit)['tool_group_id']
                or not self.rules.eligible_tools(vid, [tid])):
            return False
        if visit['queue_ready_at_us'] is None:
            visit['queue_ready_at_us'] = self.scheduler.now_us
            self._wait_generation+=1
            self.emit('visit_rack_ready', {**visit, 'rack_slot_id': sid})
        visit['rack_slot_id'] = sid
        return True

    def _visit_ready(self, vid):
        visit = self.rules.visits[vid]
        if self._step(visit).get('batch_min'):
            lot = self.lots[visit['lot_id']]
            sid = self.custody.locations.get(lot.get('carrier_id'))
            if (sid is not None and lot.get('admitted_at_us') is not None
                    and self.custody.slots[sid]['kind'] in ('entry', 'storage', 'port', 'rack', 'track_buffer')
                    and visit['queue_ready_at_us'] is None):
                visit['queue_ready_at_us'] = self.scheduler.now_us
        else:
            self._rack_ready(vid)

    def delivered(self, carrier_id, rack_slot_id):
        lookup=getattr(self,'carrier_lot_lookup',None)
        matches = ([lookup(carrier_id)[1]] if lookup is not None else
                   [lot for lot in self.lots.values() if lot.get('carrier_id') == carrier_id])
        if len(matches) != 1:
            raise ValueError('delivery requires a unique LOT carrier')
        vid = matches[0].get('current_visit_id')
        if (vid and self._step(self.rules.visits[vid]).get('batch_min')
                and not self.rules.visits[vid].get('assembly_id')):
            raise ValueError('batch delivery requires committed assembly membership')
        aid = self.rules.visits[vid].get('assembly_id') if vid else None
        if aid:
            assembly, member = self.batches._member(carrier_id, 'assembling')
            if assembly['id'] != aid or member['arrived'] or member['rack_slot_id'] != rack_slot_id:
                raise ValueError('batch delivery differs from committed rack membership')
        if (vid is None or self.rules.visits[vid]['status'] != 'pending'
                or self.rules.visits[vid].get('execution_id')
                or self.custody.locations.get(carrier_id) != rack_slot_id
                or not self._rack_ready(vid)):
            raise ValueError('delivery requires matching unclaimed eligible rack')
        aid = self.rules.visits[vid].get('assembly_id')
        if aid:
            self.batches.arrived(carrier_id, rack_slot_id)
        if self.campaigns:
            self.campaigns.arrived(vid, rack_slot_id)
        self._lot_changed(self.rules.visits[vid]['lot_id'])

    def departed(self, carrier_id):
        self.batches.departed(carrier_id)
        self.dirty()
        from .resource_changes import ResourceChange
        self._publish_resources(ResourceChange(carrier_ids=frozenset({carrier_id})))

    def rack_visits(self, tool_id):
        """Current rack residents, preserving source visit insertion order."""
        visits={};lookup=getattr(self,'carrier_lot_lookup',None)
        for sid in self.tools[tool_id]['rack_slots']:
            carrier=self.custody.slots[sid]['carrier_id']
            if carrier is None:continue
            if lookup is not None:
                try:lot=lookup(carrier)[1]
                except ValueError:continue
            else:
                lot=next((lot for lot in self.lots.values() if lot.get('carrier_id')==carrier),None)
            vid=lot.get('current_visit_id') if lot else None
            if vid in self.rules._active_visits:
                visit=self.rules._active_visits[vid]
                if self._step(visit)['tool_group_id']==self.tools[tool_id]['tool_group_id']:
                    visits[vid]=visit
        return tuple(visits[vid] for vid in sorted(visits,key=self.rules._live_order.__getitem__))

    def active_execution_count(self):
        """Committed executions through unload, including batches and cascades."""
        return self._active_execution_count

    def _tool_dispatch_required(self,tid):
        # Shared batch/campaign proposal work has already run. These are only
        # native local exits, read live after guards and earlier callbacks.
        if (type(self) is not Manufacturing
                or getattr(self.rack_visits,'__func__',None) is not _RACK_VISITS_NATIVE
                or getattr(self._dispatch_tool,'__func__',None) is not _TOOL_DISPATCH_NATIVE
                or service_dispatch_required(self.handlers[tid])):
            return True
        if tid in self.pipelines:return self._cascade_dispatch_required(tid)
        tool=self.tools[tid]
        # Canonical ordering checks campaign drain before rack candidates and
        # before fault causes. Unknown callbacks may have required effects.
        if (tool['active_execution_id'] is None and tool.get('available',True)
                and tool.get('pm_pending') and self.campaigns):return True
        if self.causes[tid] or not tool.get('available',True):return False
        eid=tool['active_execution_id']
        if eid is not None:return self.executions[eid]['status']=='loaded'
        if tool.get('assembly_id'):return True
        return any(self.custody.slots[sid]['carrier_id'] is not None for sid in tool['rack_slots'])

    def _cascade_dispatch_required(self,tid):
        """False only when _dispatch_cascade(tid) would change nothing: no LOT on
        the tool's racks (the only candidates), no loaded execution to enqueue, and
        a pipeline that can neither advance a unit nor admit a pending one (the
        service lane is checked by the caller). Mirrors _dispatch_cascade,
        Cascade.dispatch and Cascade._advance."""
        pipe=self.pipelines[tid]
        if (type(pipe) is not Cascade or getattr(self._dispatch_cascade,'__func__',None) is not _CASCADE_DISPATCH_NATIVE):
            return True
        slots=self.custody.slots
        if any(slots[sid]['carrier_id'] is not None for sid in self.tools[tid]['rack_slots'] if sid in slots):return True
        executions=self.executions
        if any(executions[eid]['status']=='loaded' for eid in self.cascade_executions[tid]):return True
        if pipe.causes or pipe.setup_active:return False
        first,second=pipe.slots
        if first is not None:return pipe.units[first]['first_complete'] and second is None
        return bool(pipe.pending) and not (pipe.admission and not pipe.admission.completed)

    def dispatch(self):
        groups=self._dirty_groups.take()
        from .resource_changes import ResourceChange
        for guard in self.dispatch_guards:
            result=guard()
            # Unknown guards may mutate evidence without publishing a change.
            if not isinstance(result,ResourceChange):
                self._wait_generation+=1;groups=None;continue
            if any((result.slot_ids,result.carrier_ids,result.tool_ids,result.group_ids,
                    result.vehicle_ids,result.rail_ids)):
                self._wait_generation+=1
            if groups is not None:
                index=getattr(self,'resource_index',None)
                if (result.carrier_ids or result.slot_ids or result.vehicle_ids or result.rail_ids
                        or result.tool_ids and index is None):groups=None
                else:
                    groups=set(groups)|set(result.group_ids)
                    for tid in result.tool_ids:
                        if tid not in index.group_by_tool:groups=None;break
                        groups.add(index.group_by_tool[tid])
        # Guards can also publish changes through the normal owner hooks.
        # Peek without consuming them: the next same-time pass still owns them.
        if self.dispatch_guards and self._dirty_groups._full:groups=None
        elif groups is not None:groups=set(groups)|self._dirty_groups._ids
        assembly_id=self.batches.commit(groups=groups)
        changed = assembly_id is not None
        if changed:
            self.dirty()
            from .resource_changes import ResourceChange
            assembly=self.batches.assemblies[assembly_id];tid=assembly['tool_id']
            self._publish_resources(ResourceChange(
                carrier_ids=frozenset(member['carrier_id'] for member in assembly['members']),
                tool_ids=frozenset({tid}),group_ids=frozenset({self.tools[tid]['tool_group_id']})))
        index=getattr(self,'resource_index',None)
        tool_ids=index.dispatch_tools(groups) if index is not None else sorted(self.tools)
        for tid in tool_ids:
            tool = self.tools[tid]
            if groups is not None and tool['tool_group_id'] not in groups:
                continue
            if not self._tool_dispatch_required(tid):
                if getattr(self,'probes',None):self.probes.count_work('manufacturing_tool_early_exits')
                continue
            if getattr(self,'probes',None):self.probes.count_work('manufacturing_tools_considered')
            changed=self._dispatch_tool(tid) or changed
        if changed:self._wait_generation+=1
        return changed

    def _held_out(self, tid, lot):
        holds = self.super_hot_holds
        return holds is not None and not lot.get('super_hot') and tid in holds()

    def _dispatch_tool(self,tid):
        """Canonical ordered local decision; shared proposals run beforehand."""
        tool=self.tools[tid];changed=False
        if tid in self.pipelines:
            return self._dispatch_cascade(tid)
        if (tool['active_execution_id'] is None and tool.get('available', True)
                and (not tool.get('pm_pending') or self.campaigns and not self.campaigns.drained(tid))
                and not tool.get('assembly_id') and not self.causes[tid]):
            candidates = []
            for visit in self.rack_visits(tid):
                if getattr(self,'probes',None):self.probes.count_work('manufacturing_visits_considered')
                step = self._step(visit)
                lot = self.lots[visit['lot_id']]
                carrier = lot.get('carrier_id')
                sid = self.custody.locations.get(carrier)
                if (visit['status'] != 'pending' or visit.get('execution_id') or visit.get('assembly_id')
                        or lot.get('admitted_at_us') is None or lot.get('transfer_id')
                        or step.get('batch_min') or step.get('cascade_interval_us')
                        or sid not in tool['rack_slots'] or carrier in self.claims
                        or not self._rack_ready(visit['id']) or not self.setups[tid].can_use(step)
                        or self._held_out(tid, lot)
                        or self.campaigns and not self.campaigns.can_commit(visit['id'],tid)
                        or tool.get('pm_pending') and not (self.campaigns
                            and self.campaigns.visit_owners.get(visit['id'])
                            and self.campaigns.can_commit(visit['id'],tid))):
                    continue
                candidates.append((queue_rank(visit, step, self.setups[tid]), visit))
            if candidates:
                visit = min(candidates, key=lambda p:p[0])[1]
                self._commit(tid, visit)
                changed = True
        aid = tool.get('assembly_id')
        if (aid and tool['active_execution_id'] is None and tool.get('available', True)
                and not self.causes[tid] and self.batches.ready(aid)):
            self._commit_batch(aid)
            changed = True
        eid = tool['active_execution_id']
        if (eid and self.executions[eid]['status'] == 'loaded'
                and tool.get('available', True) and not self.causes[tid]):
            self._start(self.executions[eid])
            changed = True
        changed = dispatch_service(self.handlers[tid]) or changed
        return changed

    def _commit(self, tid, visit):
        with self.custody.batch_notifications():
            eid = 'execution:'+visit['id']
            carrier = self.lots[visit['lot_id']]['carrier_id']
            e = {'id': eid, 'visit_id': visit['id'], 'tool_id': tid, 'carrier_id': carrier,
                 'rack_slot_id': visit['rack_slot_id'], 'status': 'committed'}
            if not self.custody.reserve_many([], eid, origins={e['rack_slot_id']: carrier}):
                raise RuntimeError('execution rack claim changed')
            self.executions[eid] = e
            self._active_execution_count += 1
            if tid in self.pipelines:
                self.cascade_executions[tid].add(eid)
            else:
                self.tools[tid]['active_execution_id'] = eid
            visit['execution_id'] = eid
            if self.campaigns:
                self.campaigns.committed(visit['id'],tid)
            self.emit('execution_committed', dict(e))
            decision = self.setups[tid].decision(self._step(visit))
            if decision['required']:
                from .service import setup_duration_us
                def complete_setup():
                    self.setups[tid].apply(decision)
                    if tid in self.pipelines:
                        self.pipelines[tid].end_setup()
                    self._load(e)
                self.handlers[tid].request(eid+':setup', 'setup', [carrier],
                    native_action(self,'manufacturing:setup-duration',eid,{'decision':decision,'lot_ids':[visit['lot_id']]},lambda: setup_duration_us(decision,[visit['lot_id']],self.streams)),
                    native_action(self,'manufacturing:setup-complete',eid,decision,complete_setup),
                    on_start=native_action(self,'manufacturing:setup-start',eid,decision,(lambda:self.pipelines[tid].begin_setup(decision['new'])) if tid in self.pipelines else (lambda:None)))
            else:
                self._load(e)


    def _load(self, e):
        self._execution_changed(e)
        group = self.groups[self.tools[e['tool_id']]['tool_group_id']]
        self.handlers[e['tool_id']].request(e['id']+':load', 'load', self._carriers(e),
                                            native_action(self,'constant','',group['loading_us'],lambda:group['loading_us']), native_action(self,'manufacturing:loaded',e['id'],None,lambda:self._loaded(e)))

    def _loaded(self, e):
        e['status'] = 'loaded'
        self._execution_changed(e)

    def _start(self, e):
        if e.get('assembly_id'):
            return self._start_batch(e)
        from .service import Timer, processing_duration_us
        tid = e['tool_id']
        visit = self.rules.visits[e['visit_id']]
        carrier = e['carrier_id']
        if (self.tools[tid]['active_execution_id'] != e['id'] or carrier in self.claims
                or self.custody.locations.get(carrier) != e['rack_slot_id']
                or self.custody.slots[e['rack_slot_id']]['reservation'] != e['id']):
            raise RuntimeError('processing start ownership changed')
        lot = self.lots[visit['lot_id']]
        self.rules.started(visit['id'], tid)
        self.claims[carrier] = e['id']
        duration = processing_duration_us(self._step(visit), [visit['lot_id']], [lot['wafer_count']], self.streams)
        if self.extra_processing is not None:
            duration += self.extra_processing.extra_us(lot['route_id'], visit['source_step_id'], duration,
                execution_id=e['id'], tool_id=tid, lot_ids=[visit['lot_id']])
        e.update(status='processing', duration_us=duration)
        e['timer'] = Timer(self.scheduler, e['id'], duration, native_action(self,'manufacturing:process',e['id'],None,lambda:self.processing_complete(e['id'])))

    def processing_complete(self, execution_id):
        e = self.executions[execution_id]
        if e.get('assembly_id'):
            return self._complete_batch(e)
        if e['tool_id'] in self.pipelines:
            return self._complete_cascade(e)
        if (e['status'] != 'processing' or not e['timer'].completed
                or self.claims.get(e['carrier_id']) != e['id']
                or self.tools[e['tool_id']]['active_execution_id'] != e['id']):
            raise RuntimeError('processing completion ownership changed')
        del self.claims[e['carrier_id']]
        self.rules.processing_complete(e['visit_id'])
        self.setups[e['tool_id']].credit(1)
        if self.campaigns:
            self.campaigns.completed(e['visit_id'],e['tool_id'])
        lot = self.lots[self.rules.visits[e['visit_id']]['lot_id']]
        self.tools[e['tool_id']]['processed_wafers'] += lot['wafer_count'];self._processed(e['tool_id'])
        e['status'] = 'processed'
        group = self.groups[self.tools[e['tool_id']]['tool_group_id']]
        self.handlers[e['tool_id']].request(e['id']+':unload', 'unload', [e['carrier_id']],
                                            native_action(self,'constant','',group['unloading_us'],lambda:group['unloading_us']), native_action(self,'manufacturing:unload',e['id'],None,lambda:self._unload(e)))

    def _unload(self, e):
        if e.get('assembly_id'):
            return self._unload_batch(e)
        visit = self.rules.visits[e['visit_id']]
        if (e['status'] != 'processed' or visit['status'] != 'processed'
                or visit.get('execution_id') != e['id']
                or self.lots[visit['lot_id']]['current_visit_id'] != visit['id']
                or not self._owns_execution(e)
                or self.custody.locations.get(e['carrier_id']) != e['rack_slot_id']
                or self.custody.slots[e['rack_slot_id']]['reservation'] != e['id']):
            raise RuntimeError('unload completion ownership changed')
        with self.custody.batch_notifications():
            self.custody.release(e['rack_slot_id'], e['id'])
            next_visit = self.rules.after_unload(e['visit_id'])
            e['status'] = 'unloaded'
            self._active_execution_count -= 1
            if e['tool_id'] in self.pipelines:
                self.cascade_executions[e['tool_id']].remove(e['id'])
            else:
                self.tools[e['tool_id']]['active_execution_id'] = None
            if next_visit:
                self._visit_ready(next_visit)
            self.tool_changed(e['tool_id'])


    def pause(self, tool_id, cause_id):
        if cause_id in self.causes[tool_id]:
            return
        self.handlers[tool_id].pause(cause_id)
        self.causes[tool_id].add(cause_id)
        self._wait_generation+=1
        if tool_id in self.pipelines:
            self.pipelines[tool_id].pause(cause_id)
            return
        eid = self.tools[tool_id]['active_execution_id']
        if eid and self.executions[eid]['status'] == 'processing':
            self.executions[eid]['timer'].pause(self.scheduler.now_us, cause_id)

    def resume(self, tool_id, cause_id):
        if cause_id not in self.causes[tool_id]:
            raise ValueError('unknown equipment downtime cause')
        self.causes[tool_id].remove(cause_id)
        self._wait_generation+=1
        self.handlers[tool_id].resume(cause_id)
        if tool_id in self.pipelines:
            self.pipelines[tool_id].resume(cause_id)
            return
        eid = self.tools[tool_id]['active_execution_id']
        if eid and self.executions[eid]['status'] == 'processing':
            self.executions[eid]['timer'].resume(self.scheduler.now_us, cause_id)
        self.tool_changed(tool_id)

    def _carriers(self, e):
        return [m['carrier_id'] for m in e['members']] if e.get('assembly_id') else [e['carrier_id']]

    def _commit_batch(self, aid):
        from .service import setup_duration_us
        a = self.batches.assemblies[aid]
        tid = a['tool_id']
        members = [dict(m) for m in a['members']]
        e = {'id':aid, 'assembly_id':aid, 'tool_id':tid, 'members':members,
             'visit_id':members[0]['visit_id'], 'status':'committed'}
        with self.custody.batch_notifications():
            if not self.custody.reserve_many([], aid, origins={m['rack_slot_id']:m['carrier_id'] for m in members}):
                raise RuntimeError('batch execution rack ownership changed')
            self.executions[aid] = e
            self._active_execution_count += 1
            self.tools[tid]['active_execution_id'] = aid
            for m in members:
                self.rules.visits[m['visit_id']]['execution_id'] = aid
            self.emit('execution_committed', dict(e))
        decision = self.setups[tid].decision(self._step(self.rules.visits[e['visit_id']]))
        if decision['required']:
            def complete_setup():
                self.setups[tid].apply(decision)
                self._load(e)
            self.handlers[tid].request(aid+':setup', 'setup', self._carriers(e),
                native_action(self,'manufacturing:setup-duration',aid,{'decision':decision,'lot_ids':[member['lot_id'] for member in members]},lambda:setup_duration_us(decision,[member['lot_id'] for member in members],self.streams)), native_action(self,'manufacturing:setup-complete',aid,decision,complete_setup))
        else:
            self._load(e)

    def _start_batch(self, e):
        from .service import Timer, processing_duration_us
        tid = e['tool_id']
        if (any(c in self.claims for c in self._carriers(e))
                or any(self.custody.locations.get(m['carrier_id']) != m['rack_slot_id']
                       or self.custody.slots[m['rack_slot_id']]['reservation'] != e['id']
                       or self.rules.visits[m['visit_id']].get('execution_id') != e['id']
                       for m in e['members'])):
            raise RuntimeError('batch processing carrier ownership changed')
        self.batches.started(e['id'], rack_owner=e['id'])
        for c in self._carriers(e):
            self.claims[c] = e['id']
        step = self._step(self.rules.visits[e['visit_id']])
        duration = processing_duration_us(step, [m['lot_id'] for m in e['members']],
            [self.lots[m['lot_id']]['wafer_count'] for m in e['members']], self.streams)
        if self.extra_processing is not None:
            owner = min(m['lot_id'] for m in e['members'])
            duration += self.extra_processing.extra_us(self.lots[owner]['route_id'], step['source_step_id'], duration,
                execution_id=e['id'], tool_id=tid, lot_ids=sorted(m['lot_id'] for m in e['members']))
        e.update(status='processing', duration_us=duration)
        e['timer'] = Timer(self.scheduler, e['id'], duration, native_action(self,'manufacturing:process',e['id'],None,lambda:self.processing_complete(e['id'])))

    def _complete_batch(self, e):
        if (e['status'] != 'processing' or not e['timer'].completed
                or any(self.claims.get(c) != e['id'] for c in self._carriers(e))
                or any(self.rules.visits[m['visit_id']].get('execution_id') != e['id'] for m in e['members'])):
            raise RuntimeError('batch processing completion ownership changed')
        self.batches.finished(e['id'])
        for c in self._carriers(e):
            del self.claims[c]
        self.tools[e['tool_id']]['processed_wafers'] += sum(self.lots[m['lot_id']]['wafer_count'] for m in e['members']);self._processed(e['tool_id'])
        e['status'] = 'processed'
        group = self.groups[self.tools[e['tool_id']]['tool_group_id']]
        self.handlers[e['tool_id']].request(e['id']+':unload', 'unload', self._carriers(e),
                                           native_action(self,'constant','',group['unloading_us'],lambda:group['unloading_us']), native_action(self,'manufacturing:unload',e['id'],None,lambda:self._unload(e)))

    def _unload_batch(self, e):
        a = self.batches.assemblies[e['id']]
        self.batches._owned_tool(a, (e['id'],))
        if (e['status'] != 'processed' or a['status'] != 'processed'
                or not self.batches._members_in(a, 'processed')
                or any(self.rules.visits[m['visit_id']].get('execution_id') != e['id'] for m in e['members'])
                or any(self.custody.locations.get(m['carrier_id']) != m['rack_slot_id']
                       or self.custody.slots[m['rack_slot_id']]['reservation'] != e['id'] for m in e['members'])):
            raise RuntimeError('batch unload completion ownership changed')
        with self.custody.batch_notifications():
            for m in e['members']:
                self.custody.release(m['rack_slot_id'], e['id'])
            self.batches.unloaded(e['id'])
            e['status'] = 'unloaded'
            self._active_execution_count -= 1
            for m in e['members']:
                vid = self.lots[m['lot_id']]['current_visit_id']
                if vid:
                    self._visit_ready(vid)
        self.dirty()

    def _owns_execution(self, e):
        tid = e['tool_id']
        if tid in self.pipelines:
            return e['id'] in self.cascade_executions[tid]
        return self.tools[tid]['active_execution_id'] == e['id']

    def _dispatch_cascade(self, tid):
        tool, pipe = self.tools[tid], self.pipelines[tid]
        changed = False
        if (tool.get('available', True) and not self.causes[tid]
                and (not tool.get('pm_pending') or self.campaigns and not self.campaigns.drained(tid))):
            candidates = []
            # Only LOTs in this tool's rack slots can qualify; test that first
            # (the skipped conditions are pure, so the same visits reach
            # _rack_ready, in the same order).
            slots = self.custody.slots
            at_rack = {slots[sid]['carrier_id'] for sid in tool['rack_slots'] if sid in slots}
            at_rack.discard(None)
            for visit in self.rules.current_visits(tool['tool_group_id']) if at_rack else ():
                lot = self.lots[visit['lot_id']]
                carrier = lot.get('carrier_id')
                if carrier not in at_rack: continue
                step = self._step(visit)
                if (visit['status'] != 'pending' or visit.get('execution_id') or visit.get('assembly_id')
                        or not step.get('cascade_interval_us') or step['processing_unit'] != pipe.mode
                        or step.get('batch_min') or lot.get('admitted_at_us') is None or lot.get('transfer_id')
                        or self.custody.locations.get(carrier) not in tool['rack_slots']
                        or carrier in self.claims or not self._rack_ready(visit['id'])
                        or not self.setups[tid].can_use(step) or self._held_out(tid, lot)
                        or self.campaigns and not self.campaigns.can_commit(visit['id'],tid)
                        or tool.get('pm_pending') and not (self.campaigns
                            and self.campaigns.visit_owners.get(visit['id'])
                            and self.campaigns.can_commit(visit['id'],tid))):
                    continue
                decision = self.setups[tid].decision(step)
                if decision['required'] and (self.cascade_executions[tid] or any(pipe.slots)
                                             or pipe.setup_active or self.handlers[tid].active):
                    continue
                if pipe.setup_active:
                    continue
                candidates.append((queue_rank(visit, step, self.setups[tid]), visit))
            if candidates:
                self._commit(tid, min(candidates, key=lambda p:p[0])[1])
                changed = True
        for eid in sorted(self.cascade_executions[tid]):
            e = self.executions[eid]
            if e['status'] == 'loaded' and tool.get('available', True) and not self.causes[tid] and not pipe.setup_active:
                self._enqueue_cascade(e)
                changed = True
        changed = dispatch_service(self.handlers[tid]) or changed
        changed = pipe.dispatch() or changed
        return changed

    def _enqueue_cascade(self, e):
        visit = self.rules.visits[e['visit_id']]
        step = self._step(visit)
        lot = self.lots[visit['lot_id']]
        pipe = self.pipelines[e['tool_id']]
        e['status'] = 'queued'
        pipe.enqueue_lot(visit['lot_id'], lot['wafer_count'] if pipe.mode == 'wafer' else 1,
                         step['cascade_interval_us'], native_action(self,'manufacturing:cascade-sample',e['id'],None,lambda:self._cascade_sample(e)),
                         native_action(self,'manufacturing:cascade-complete',e['id'],None,lambda uid:self._cascade_unit_complete(e,uid)), self.setups[e['tool_id']].current,
                         eligible=native_action(self,'manufacturing:cascade-eligible',e['id'],None,lambda:self.tools[e['tool_id']].get('available',True) and not self.causes[e['tool_id']]),
                         execution_id=e['id'])

    def _cascade_ownership(self, e):
        visit = self.rules.visits[e['visit_id']]
        slot = self.custody.slots[e['rack_slot_id']]
        return (self._owns_execution(e) and visit.get('execution_id') == e['id']
                and self.lots[visit['lot_id']]['current_visit_id'] == visit['id']
                and self.custody.locations.get(e['carrier_id']) == e['rack_slot_id']
                and slot['carrier_id'] == e['carrier_id'] and slot['reservation'] == e['id'])

    def _cascade_sample(self, e):
        from .rng import sample_seconds
        from .timebase import to_ticks
        visit = self.rules.visits[e['visit_id']]
        if not self._cascade_ownership(e):
            raise RuntimeError('cascade start ownership changed')
        first = e['status'] == 'queued'
        if first:
            if e['carrier_id'] in self.claims:
                raise RuntimeError('cascade carrier claimed by another service')
            self.rules.started(e['visit_id'], e['tool_id'])
            self.claims[e['carrier_id']] = e['id']
            e['status'] = 'processing'
        elif e['status'] != 'processing' or self.claims.get(e['carrier_id']) != e['id']:
            raise RuntimeError('cascade continuation ownership changed')
        duration = to_ticks(sample_seconds(self._step(visit)['processing'], self.streams.get('processing', visit['lot_id'])))
        if first and self.extra_processing is not None:
            # One decision per execution: drawn at the first unit, sized to the
            # whole LOT (unit time x units), and added to that first unit.
            step = self._step(visit); lot = self.lots[visit['lot_id']]
            units = lot['wafer_count'] if step['processing_unit'] == 'wafer' else 1
            duration += self.extra_processing.extra_us(lot['route_id'], visit['source_step_id'], duration * units,
                execution_id=e['id'], tool_id=e['tool_id'], lot_ids=[visit['lot_id']], unit='cascade')
        return duration

    def _cascade_unit_complete(self, e, uid):
        pipe = self.pipelines[e['tool_id']]
        unit = pipe.units[uid]
        visit = self.rules.visits[e['visit_id']]
        if (unit['status'] != 'complete' or unit['execution_id'] != e['id']
                or not self._cascade_ownership(e)
                or self.claims.get(e['carrier_id']) != e['id']):
            raise RuntimeError('cascade unit completion ownership changed')
        self.tools[e['tool_id']]['processed_wafers'] += 1 if pipe.mode == 'wafer' else self.lots[visit['lot_id']]['wafer_count'];self._processed(e['tool_id'])
        if pipe.executions[e['id']]['remaining'] == 0:
            self.processing_complete(e['id'])

    def _complete_cascade(self, e):
        pipe = self.pipelines[e['tool_id']]
        visit = self.rules.visits[e['visit_id']]
        if (e['status'] != 'processing' or pipe.executions[e['id']]['remaining'] != 0
                or not self._cascade_ownership(e)
                or self.claims.get(e['carrier_id']) != e['id']):
            raise RuntimeError('cascade processing completion ownership changed')
        self.rules.processing_complete(e['visit_id'])
        self.setups[e['tool_id']].credit(1)
        if self.campaigns:
            self.campaigns.completed(e['visit_id'],e['tool_id'])
        del self.claims[e['carrier_id']]
        e['status'] = 'processed'
        group = self.groups[self.tools[e['tool_id']]['tool_group_id']]
        self.handlers[e['tool_id']].request(e['id']+':unload', 'unload', [e['carrier_id']],
                                           native_action(self,'constant','',group['unloading_us'],lambda:group['unloading_us']), native_action(self,'manufacturing:unload',e['id'],None,lambda:self._unload(e)))

_RACK_VISITS_NATIVE=Manufacturing.rack_visits

_TOOL_DISPATCH_NATIVE=Manufacturing._dispatch_tool
_CASCADE_DISPATCH_NATIVE=Manufacturing._dispatch_cascade
