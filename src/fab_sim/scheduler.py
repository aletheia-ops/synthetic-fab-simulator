"""One integer-tick owner, causal state events and ordered dirty closure."""
from collections import deque
from contextlib import nullcontext
from heapq import heappop, heappush

PASS_ORDER = ('admission','manufacturing','transfer','traffic')


class TransitionBudgetExceeded(RuntimeError):
    def __init__(self,evidence):
        super().__init__('same-timestamp transition budget exceeded; suspected loop or excessive burst')
        self.evidence=evidence


class Scheduler:
    def __init__(self,transition_budget=100000,recent_limit=32):
        if type(transition_budget) is not int or transition_budget<=0: raise ValueError('invalid transition budget')
        if type(recent_limit) is not int or recent_limit<=0: raise ValueError('invalid recent limit')
        self.now_us=0; self._heap=[]; self._sequence=0; self._pending=set(); self._cancelled=set()
        self._handlers={}; self._passes={}; self.dirty=set(); self._deadline_checks=[]
        self.transition_budget=transition_budget; self.recent=deque(maxlen=recent_limit)
        self._settling=False; self.handled_events=0; self.pass_calls=0

    def register(self,kind,handler):
        if not isinstance(kind,str) or not kind or kind in self._handlers or not callable(handler):
            raise ValueError('missing/duplicate event handler')
        self._handlers[kind]=handler

    def unregister(self,kind):
        self._handlers.pop(kind,None)

    def register_pass(self,name,handler):
        if name not in PASS_ORDER or name in self._passes or not callable(handler): raise ValueError('invalid/duplicate dispatch pass')
        self._passes[name]=handler

    def mark_dirty(self,pass_name):
        if pass_name not in self._passes: raise ValueError('unknown dispatch pass')
        self.dirty.add(pass_name)

    def add_deadline_check(self,handler):
        if not callable(handler): raise ValueError('invalid deadline check')
        self._deadline_checks.append(handler)

    def schedule(self,at_us: int,phase: int,kind: str,payload: dict) -> int:
        if type(at_us) is not int or at_us<self.now_us: raise ValueError('event time is invalid or in the past')
        if type(phase) is not int or phase not in range(4): raise ValueError('unknown event phase')
        if kind not in self._handlers: raise ValueError('unknown event kind')
        if not isinstance(payload,dict): raise ValueError('event payload must be a record')
        self._sequence+=1; sequence=self._sequence
        heappush(self._heap,(at_us,phase,sequence,kind,payload)); self._pending.add(sequence)
        return sequence

    def cancel(self,sequence):
        if sequence in self._pending: self._cancelled.add(sequence)

    def _discard_cancelled(self):
        while self._heap and self._heap[0][2] in self._cancelled:
            sequence=heappop(self._heap)[2]; self._cancelled.remove(sequence); self._pending.remove(sequence)

    @property
    def next_time_us(self):
        self._discard_cancelled()
        return self._heap[0][0] if self._heap else None

    def _observe(self,kind):
        probes=getattr(self,'probes',None)
        return probes.handler(kind) if probes is not None else nullcontext()

    def settle(self,at_us: int) -> None:
        if type(at_us) is not int or at_us<self.now_us: raise ValueError('invalid settlement time')
        if self._settling: raise ValueError('scheduler settlement cannot reenter')
        if self.next_time_us is not None and self.next_time_us<at_us:
            raise ValueError('cannot skip unsettled earlier events')
        self.now_us=at_us; steps=0; self._settling=True
        def consume(kind,sequence=None,phase=None):
            nonlocal steps
            if steps>=self.transition_budget:
                raise TransitionBudgetExceeded({'reason':'same_timestamp_budget','simulation_time_us':at_us,
                                                'steps':steps,'recent':list(self.recent)})
            steps+=1
            self.recent.append({'kind':kind[:200],'sequence':sequence,'phase':phase,'simulation_time_us':at_us})
        try:
            while True:
                while self.next_time_us==at_us:
                    _,phase,sequence,kind,payload=heappop(self._heap); self._pending.remove(sequence)
                    consume(kind,sequence,phase)
                    with self._observe('scheduler_events'):self._handlers[kind](payload)
                    self.handled_events+=1
                if self.dirty:
                    name=next(name for name in PASS_ORDER if name in self.dirty)
                    self.dirty.remove(name); consume('dispatch:'+name)
                    with self._observe('dispatch:'+name):result=self._passes[name]()
                    probes=getattr(self,'probes',None)
                    if result is False and probes is not None:probes.count_work('dispatch_no_change:'+name)
                    self.pass_calls+=1
                    continue
                # Target starts at an inclusive CQT deadline have already dispatched.
                for check in self._deadline_checks:
                    consume('deadline')
                    with self._observe('deadline_checks'):check()
                if not self.dirty and self.next_time_us!=at_us: break
        finally: self._settling=False


    def export_state(self) -> dict[str, object]:
        from copy import deepcopy
        from .checkpoint_format import CheckpointError
        if self._settling or self.dirty:
            raise CheckpointError('checkpoint requires a fully settled scheduler')
        return deepcopy({'now_us':self.now_us, 'heap':self._heap, 'sequence':self._sequence,
            'pending':self._pending, 'cancelled':self._cancelled, 'handlers':sorted(self._handlers),
            'passes':list(self._passes), 'transition_budget':self.transition_budget,
            'recent':list(self.recent), 'recent_limit':self.recent.maxlen,
            'handled_events':self.handled_events, 'pass_calls':self.pass_calls})

    def restore_state(self, state: dict[str, object]) -> None:
        from copy import deepcopy
        from .checkpoint_format import CheckpointError
        fields={'now_us','heap','sequence','pending','cancelled','handlers','passes',
                'transition_budget','recent','recent_limit','handled_events','pass_calls'}
        try:
            if self._settling or set(state)!=fields:raise ValueError('invalid scheduler state fields')
            for key in ('now_us','sequence','handled_events','pass_calls'):
                if type(state[key]) is not int or state[key]<0:raise ValueError('invalid scheduler counter')
            if any(type(state[k]) is not int or state[k]<=0 for k in ('transition_budget','recent_limit')):
                raise ValueError('invalid scheduler guard')
            if sorted(self._handlers)!=state['handlers'] or list(self._passes)!=state['passes']:
                raise ValueError('scheduler handler/pass identity mismatch')
            heap=state['heap'];pending=state['pending'];cancelled=state['cancelled']
            if type(heap) is not list or type(pending) is not set or type(cancelled) is not set:
                raise ValueError('invalid scheduler containers')
            sequences=set()
            for event in heap:
                if type(event) is not tuple or len(event)!=5:raise ValueError('invalid event')
                tick,phase,sequence,kind,payload=event
                if (type(tick) is not int or tick<state['now_us'] or type(phase) is not int or phase not in range(4)
                        or type(sequence) is not int or not 0<sequence<=state['sequence'] or sequence in sequences
                        or type(payload) is not dict or (sequence not in cancelled and kind not in self._handlers)):
                    raise ValueError('invalid checkpoint event')
                sequences.add(sequence)
            if sequences!=pending or not cancelled<=pending:raise ValueError('invalid scheduler ownership')
            if any(heap[(i-1)//2][:3]>heap[i][:3] for i in range(1,len(heap))):
                raise ValueError('invalid scheduler heap order')
            if type(state['recent']) is not list or len(state['recent'])>state['recent_limit']:
                raise ValueError('invalid scheduler history')
        except (ValueError, TypeError, KeyError) as error:
            raise CheckpointError('invalid scheduler state: '+str(error)) from error
        self.now_us=state['now_us'];self._heap=deepcopy(heap);self._sequence=state['sequence']
        self._pending=set(pending);self._cancelled=set(cancelled);self.dirty.clear()
        self.transition_budget=state['transition_budget'];self.recent=deque(deepcopy(state['recent']),maxlen=state['recent_limit'])
        self.handled_events=state['handled_events'];self.pass_calls=state['pass_calls']
