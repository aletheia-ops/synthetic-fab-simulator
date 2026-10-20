"""Native per-instance downtime, committed-work drain and independent recovery."""
from decimal import Decimal
from fractions import Fraction
from math import ceil
import json
from .rng import sample_seconds
from .timebase import to_ticks


class Maintenance:
    def __init__(self, manufacturing, emit, *, exchange_handlers=None):
        self.manufacturing, self.emit = manufacturing, emit
        self.scheduler, self.streams, self.tools = manufacturing.scheduler, manufacturing.streams, manufacturing.tools
        self.exchange_handlers = exchange_handlers or {}
        # False under down-tool output: the port handler keeps moving carriers out.
        self.pause_exchange = True
        self.history_archive=None
        self.definitions, self.causes = {}, {}
        self._pending_by_tool = {}
        self._active_by_tool = {}
        self._bindings_by_tool = {}
        self._counter_bindings = ()
        self._counter_ceilings = {}
        self.initialized = False

    def initialize(self, topology, manufacturing, *, injected_faults=(), initialize=True):
        if self.initialized:
            raise RuntimeError('maintenance already initialized')
        equipment = [e for e in topology['model']['equipment'] if e['kind'] == 'ordinary']
        if (len({e['id'] for e in equipment}) != len(equipment)
                or {e['id'] for e in equipment} != set(self.tools)
                or any(e['tool_group_id'] != self.tools[e['id']]['tool_group_id'] for e in equipment)):
            raise ValueError('maintenance topology/runtime equipment mismatch')
        model = manufacturing['model']
        groups = {g['id']:g for g in model['tool_groups']}
        by_group = {}
        for e in equipment:
            by_group.setdefault(e['tool_group_id'], []).append(e['id'])
        for tids in by_group.values():
            tids.sort()
        self.scheduler.register('maintenance:due', self._due)
        self.scheduler.register('maintenance:recover', lambda p:self.recover(p['cause_id']))
        if not initialize:
            self.initialized=True
            self.manufacturing.dispatch_guards.insert(0,self.dispatch)
            return
        for tid in sorted(self.tools):
            gid = self.tools[tid]['tool_group_id']
            tids = by_group[gid]
            for d in model['maintenance']:
                if d['scope'] != 'toolgroup' or d['type'] not in ('calendar', 'counter'):
                    raise ValueError('unsupported maintenance scope/type')
                if d['scope_id'] != gid:
                    continue
                if type(d['interval']) is not int or d['interval'] <= 0:
                    raise ValueError('invalid maintenance interval')
                binding = self._bind(tid, 'pm', d)
                if d['type'] == 'counter':
                    if d['first']['distribution'] != 'constant':
                        raise ValueError('unsupported counter first threshold')
                    threshold = Fraction(d['first']['value']) * (tids.index(tid)+1) / len(tids)
                    if threshold < 0:
                        raise ValueError('negative counter threshold')
                    binding['next_threshold'] = threshold
                else:
                    first = d['first']
                    delay = (to_ticks(Fraction(first['mean_seconds']) * (tids.index(tid)+1) / len(tids))
                             if first['distribution'] == 'constant' else self._sample(binding, 'first'))
                    self._schedule(binding, self.scheduler.now_us + delay)
            for d in model['breakdowns']:
                if d['scope'] != 'area':
                    raise ValueError('unsupported breakdown scope')
                if d['scope_id'] == groups[gid]['area']:
                    binding = self._bind(tid, 'breakdown', d)
                    self._schedule(binding, self.scheduler.now_us + self._sample(binding, 'first'))
        seen = set()
        for d in injected_faults:
            if (set(d) != {'id','target_type','target_id','onset_us','duration_us'}
                    or not isinstance(d['id'], str) or not d['id'] or d['id'] in seen
                    or d['target_type'] != 'equipment' or d['target_id'] not in self.tools
                    or type(d['onset_us']) is not int or d['onset_us'] < self.scheduler.now_us
                    or type(d['duration_us']) is not int or d['duration_us'] <= 0):
                raise ValueError('invalid injected equipment fault')
            seen.add(d['id'])
            binding = self._bind(d['target_id'], 'injected', d)
            self._schedule(binding, d['onset_us'])
        self.initialized = True
        self.manufacturing.dispatch_guards.insert(0, self.dispatch)
        self.manufacturing.dirty()

    def _bind(self, tid, kind, definition):
        key = (tid, kind, definition['id'])
        if key in self.definitions:
            raise ValueError('duplicate equipment maintenance definition')
        binding = {'tool_id':tid, 'kind':kind, 'source':definition, 'sequence':0,
                   'rng_scope':json.dumps(['equipment',tid,kind,definition['id']],separators=(',',':'))}
        self.definitions[key] = binding
        self._bindings_by_tool[tid] = (*self._bindings_by_tool.get(tid,()), key)
        if kind=='pm' and definition['type']=='counter':
            self._counter_bindings = (*self._counter_bindings,key)
        return binding

    def rebuild_derived(self):
        self._pending_by_tool={};self._active_by_tool={};self._bindings_by_tool={}
        self._counter_bindings=tuple(k for k,b in self.definitions.items()
            if b['kind']=='pm' and b['source']['type']=='counter')
        for key,binding in self.definitions.items():
            tid=binding['tool_id']
            self._bindings_by_tool[tid]=(*self._bindings_by_tool.get(tid,()),key)
        for cid,r in self.causes.items():
            if r['status'] in ('pending','active'):
                target=self._pending_by_tool if r['status']=='pending' else self._active_by_tool
                target.setdefault(r['tool_id'],{})[cid]=r

    def _sample(self, binding, field):
        return to_ticks(sample_seconds(binding['source'][field], self.streams.get('maintenance',binding['rng_scope'])))

    def _new(self, binding, due):
        binding['sequence'] += 1
        cid = json.dumps([binding['kind'],binding['tool_id'],binding['source']['id'],binding['sequence']],separators=(',',':'))
        record = {'id':cid, 'kind':binding['kind'], 'tool_id':binding['tool_id'],
                  'definition_id':binding['source']['id'], 'due_at_us':due,
                  'status':'scheduled', 'started_at_us':None, 'recovery_at_us':None,
                  'recovered_at_us':None, 'binding':binding}
        self.causes[cid] = record
        return record

    def _schedule(self, binding, due):
        r = self._new(binding, due)
        r['event_id'] = self.scheduler.schedule(due, 2, 'maintenance:due', {'cause_id':r['id']})
        return r

    def _event(self, r):
        return {k:v for k,v in r.items() if k not in ('binding','event_id','recovery_event_id')}

    def _due(self, payload):
        r = self.causes[payload['cause_id']]
        if r['kind'] == 'pm':
            self.pending(r['id'])
        else:
            self.start(r['id'])

    def pending(self, cause_id):
        r = self.causes[cause_id]
        if r['kind'] != 'pm' or r['status'] != 'scheduled' or r['due_at_us'] > self.scheduler.now_us:
            raise RuntimeError('invalid pending maintenance cause')
        r['status'] = 'pending'
        self._pending_by_tool.setdefault(r['tool_id'], {})[cause_id] = r
        self.tools[r['tool_id']]['pm_pending'] = True
        self.emit('maintenance_pending', self._event(r))
        self.manufacturing.dirty()

    def _drained(self, tid):
        m, tool = self.manufacturing, self.tools[tid]
        aid = tool.get('assembly_id')
        exchange = self.exchange_handlers.get(tid)
        return ((m.campaigns is None or m.campaigns.drained(tid))
                and tool.get('active_execution_id') is None and not m.cascade_executions[tid]
                and (not aid or m.batches.assemblies[aid]['status'] in ('finished','cleared'))
                and m.handlers[tid].active is None and (exchange is None or exchange.active is None))

    def _counter_entries(self):
        # Derived per-binding rows, rebuilt when the binding inventory, the
        # definitions map is replaced (restore/initialize).
        cached=self._counter_ceilings.get('entries')
        if cached is not None and cached[0] is self._counter_bindings and cached[1] is self.definitions:return cached[2]
        entries=[]
        for binding_key in self._counter_bindings:
            binding=self.definitions[binding_key]
            if binding['kind']=='pm' and binding['source']['type']=='counter':
                entries.append([binding,binding['tool_id'],None,None])
        self._counter_ceilings['entries']=(self._counter_bindings,self.definitions,entries)
        return entries

    def dispatch(self):
        tools=self.tools;m=self.manufacturing
        # Counter thresholds change only here; counters only on processing.
        changed=getattr(m,'processed_changed',None)
        if hasattr(m,'processed_changed'):m.processed_changed=set()
        # An empty change set skips every counter entry below.
        for entry in (self._counter_entries() if changed is None or changed else ()):
            binding,tid,threshold,limit=entry
            if changed is not None and tid not in changed:continue
            tool=tools[tid];current=binding['next_threshold'];processed=tool['processed_wafers']
            # Integer wafer counts reach a rational threshold exactly when they
            # reach its ceiling; avoid a Fraction comparison per pass.
            if current is not threshold:
                limit=ceil(current) if type(current) in (int,Fraction,Decimal) else None
                entry[2]=current;entry[3]=limit
            if limit is not None and type(processed) is int and processed<limit:continue
            d=binding['source']
            while tool['processed_wafers'] >= binding['next_threshold']:
                r = self._new(binding, self.scheduler.now_us)
                binding['next_threshold'] += d['interval']
                self.pending(r['id'])
        # Retained causes are audit history; only pending causes can start.
        for tid in sorted(self._pending_by_tool):
            if self.manufacturing.causes[tid] or not self.tools[tid].get('available', True) or not self._drained(tid):
                continue
            pending = self._pending_by_tool[tid].values()
            self.start(min(pending,key=lambda r:(r['due_at_us'],r['id']))['id'])
        from .resource_changes import ResourceChange
        return ResourceChange()

    def start(self, cause_id):
        r = self.causes[cause_id]
        tid, binding = r['tool_id'], r['binding']
        if r['due_at_us'] > self.scheduler.now_us:
            raise RuntimeError('maintenance cause not due')
        if r['kind'] == 'pm':
            if (r['status'] != 'pending' or self.manufacturing.causes[tid]
                    or not self.tools[tid].get('available', True) or not self._drained(tid)):
                raise RuntimeError('PM requires available drained committed work')
            duration = self._sample(binding, 'duration')
        else:
            if r['status'] != 'scheduled':
                raise RuntimeError('fault already active or recovered')
            duration = binding['source']['duration_us'] if r['kind'] == 'injected' else self._sample(binding, 'duration')
        r.update(status='active',started_at_us=self.scheduler.now_us,duration_us=duration,
                 recovery_at_us=self.scheduler.now_us+duration)
        if r['kind'] == 'pm':
            pending = self._pending_by_tool[tid]
            del pending[cause_id]
            if not pending:
                del self._pending_by_tool[tid]
        self._active_by_tool.setdefault(tid,{})[cause_id]=r
        self.manufacturing.pause(tid, r['id'])
        if tid in self.exchange_handlers and self.pause_exchange:
            self.exchange_handlers[tid].pause(r['id'])
        r['recovery_event_id'] = self.scheduler.schedule(r['recovery_at_us'], 1, 'maintenance:recover', {'cause_id':r['id']})
        if r['kind'] == 'pm' and binding['source']['type'] == 'calendar':
            self._schedule(binding, self.scheduler.now_us + binding['source']['interval'])
        self.emit('maintenance_started', self._event(r))
        self.manufacturing.dirty()

    def long_downtime(self, tool_id, threshold_us):
        """An active downtime of the tool lasts at least threshold_us in total.

        Uses the duration fixed at the downtime's start, so the answer changes
        only when a downtime starts or ends (both resource changes)."""
        return any(r['duration_us'] >= threshold_us for r in self._active_by_tool.get(tool_id, {}).values())

    def release_exchange_pauses(self):
        """Resume port handlers paused by downtime (a checkpoint saved before
        down-tool output was enabled); recovery then finds them unpaused."""
        for tid in sorted(self.exchange_handlers):
            handler = self.exchange_handlers[tid]
            for cause in sorted(handler.causes & set(self.causes)):
                handler.resume(cause)

    def recover(self, cause_id):
        r = self.causes[cause_id]
        if r['status'] != 'active' or r['recovery_at_us'] != self.scheduler.now_us:
            raise RuntimeError('recovery requires actual active cause deadline')
        tid = r['tool_id']
        self.manufacturing.resume(tid, r['id'])
        if tid in self.exchange_handlers and r['id'] in self.exchange_handlers[tid].causes:
            self.exchange_handlers[tid].resume(r['id'])
        r.update(status='recovered',recovered_at_us=self.scheduler.now_us)
        active=self._active_by_tool[tid];del active[cause_id]
        if not active:del self._active_by_tool[tid]
        self.tools[tid]['pm_pending'] = bool(self._pending_by_tool.get(tid)) or any(
            v['kind']=='pm' for v in self._active_by_tool.get(tid,{}).values())
        if r['kind'] == 'breakdown':
            self._schedule(r['binding'], self.scheduler.now_us + self._sample(r['binding'], 'ttf'))
        self.emit('maintenance_recovered', self._event(r))
        self.manufacturing.dirty()
