"""Pausable phase clocks preserve samples and independently owned downtime."""
from .native_actions import native_action



class Timer:
    def __init__(self,scheduler,timer_id,duration_us,on_complete):
        if type(duration_us) is not int or duration_us<0: raise ValueError('invalid timer duration')
        if not isinstance(timer_id,str) or not timer_id or not callable(on_complete): raise ValueError('invalid timer identity/callback')
        self.scheduler=scheduler; self.id=timer_id; self.remaining_us=duration_us
        self.causes=set(); self.completed=False; self.cancelled=False; self._on_complete=on_complete
        self._kind='timer:'+timer_id; self._event=None; self._end=None
        scheduler.register(self._kind,self._complete); self._schedule()

    def _schedule(self):
        self._end=self.scheduler.now_us+self.remaining_us
        self._event=self.scheduler.schedule(self._end,0,self._kind,{})

    def _complete(self,payload):
        if self.causes or self.completed or self.cancelled: raise RuntimeError('invalid active timer completion')
        self.remaining_us=0; self.completed=True; self._event=None; self._end=None
        self.scheduler.unregister(self._kind); self._on_complete()

    def _check_time(self,now_us):
        if type(now_us) is not int or now_us!=self.scheduler.now_us: raise ValueError('timer time must equal scheduler time')

    def pause(self,now_us: int,cause_id: str) -> None:
        self._check_time(now_us)
        if not isinstance(cause_id,str) or not cause_id: raise ValueError('invalid timer cause')
        if self.completed or self.cancelled or cause_id in self.causes: return
        if not self.causes:
            self.remaining_us=self._end-now_us
            if self.remaining_us<0: raise RuntimeError('timer paused after unprocessed completion')
            self.scheduler.cancel(self._event); self._event=None; self._end=None
        self.causes.add(cause_id)

    def resume(self,now_us: int,cause_id: str) -> None:
        self._check_time(now_us)
        if self.completed or self.cancelled: return
        if cause_id not in self.causes: raise ValueError('unknown timer cause')
        self.causes.remove(cause_id)
        if not self.causes: self._schedule()

    def cancel(self):
        if self.completed or self.cancelled: return
        self.cancelled=True
        if self._event is not None: self.scheduler.cancel(self._event)
        self.scheduler.unregister(self._kind)



    def export_state(self) -> dict[str, object]:
        from .actions import export_action
        return {'id':self.id,'remaining_us':self.remaining_us,'causes':set(self.causes),
            'completed':self.completed,'cancelled':self.cancelled,'event':self._event,'end':self._end,
            'on_complete':export_action(self._on_complete)}

    @classmethod
    def restore(cls, scheduler, state: dict[str, object], actions):
        from .actions import restore_action
        from .checkpoint_format import CheckpointError
        fields={'id','remaining_us','causes','completed','cancelled','event','end','on_complete'}
        if (set(state)!=fields or not isinstance(state['id'],str) or not state['id']
                or type(state['remaining_us']) is not int or state['remaining_us']<0
                or type(state['causes']) is not set or any(not isinstance(c,str) or not c for c in state['causes'])
                or type(state['completed']) is not bool or type(state['cancelled']) is not bool):
            raise CheckpointError('invalid timer state')
        timer=cls.__new__(cls);timer.scheduler=scheduler;timer.id=state['id']
        timer.remaining_us=state['remaining_us'];timer.causes=set(state['causes'])
        timer.completed=state['completed'];timer.cancelled=state['cancelled']
        timer._kind='timer:'+timer.id;timer._event=state['event'];timer._end=state['end']
        timer._on_complete=restore_action(state['on_complete'],actions)
        active=not(timer.completed or timer.cancelled or timer.causes)
        if active and (type(timer._event) is not int or type(timer._end) is not int):
            raise CheckpointError('active timer missing deadline')
        if not timer.completed and not timer.cancelled:scheduler.register(timer._kind,timer._complete)
        return timer


def processing_duration_us(step, member_lot_ids, wafer_counts, streams):
    """Sample a committed non-cascading execution; quantize the total once."""
    from fractions import Fraction
    from .rng import sample_seconds
    from .timebase import to_ticks
    ids = tuple(member_lot_ids)
    if (not ids or len(ids) != len(set(ids)) or any(not isinstance(i,str) or not i for i in ids)
            or len(ids) != len(wafer_counts) or any(type(n) is not int or n <= 0 for n in wafer_counts)):
        raise ValueError('invalid processing membership')
    if step.get('cascade_interval_us') is not None:
        raise ValueError('cascade samples at actual unit starts')
    unit = step['processing_unit']
    if unit not in ('lot', 'wafer', 'batch') or (unit != 'batch' and len(ids) != 1):
        raise ValueError('invalid non-cascading processing unit/membership')
    owner = min(ids)
    count = wafer_counts[0] if unit == 'wafer' else 1
    total = sum((Fraction(sample_seconds(step['processing'], streams.get('processing',owner)))
                 for _ in range(count)), Fraction(0))
    return to_ticks(total)


def setup_duration_us(decision, member_lot_ids, streams):
    """Sample required committed setup once, sharing the batch draw owner."""
    from .rng import sample_seconds
    from .timebase import to_ticks
    ids = tuple(member_lot_ids)
    if not ids or len(ids) != len(set(ids)) or any(not isinstance(i,str) or not i for i in ids):
        raise ValueError('invalid setup membership')
    if not decision['required']:
        return 0
    if decision['duration_us'] is not None:
        value = decision['duration_us']
        if type(value) is not int or value < 0:
            raise ValueError('invalid setup duration')
        return value
    return to_ticks(sample_seconds(decision['distribution'], streams.get('processing',min(ids))))


class SerializedHandler:
    """One service or exchange lane; shared carrier claims prevent overlap.

    Duration callbacks sample only on actual start. Feasibility and completion
    callbacks bind this lane to physical custody/setup/pipeline state in the
    manufacturing controller; this primitive does not move carriers.
    """
    def __init__(self,scheduler,handler_id,carrier_claims,dirty,emit):
        if not isinstance(handler_id,str) or not handler_id:
            raise ValueError('handler ID required')
        self.scheduler,self.id,self.claims,self.dirty,self.emit=scheduler,handler_id,carrier_claims,dirty,emit
        self._pending_changed=None
        self.requests={};self.active=None;self.causes=set();self._pending={};self._pending_order=();self._pending_signature=None;self.history_archive=None;self._history_lookup=None

    def request(self,request_id,kind,carrier_ids,duration,on_complete,eligible=None,on_start=None):
        eligible=native_action(self,'true','',None,lambda:True) if eligible is None else eligible
        on_start=native_action(self,'noop','',None,lambda:None) if on_start is None else on_start
        carriers=tuple(carrier_ids)
        if (not isinstance(request_id,str) or not request_id or request_id in self.requests
                or kind not in ('setup','load','unload','exchange')
                or len(carriers)!=len(set(carriers)) or any(not isinstance(c,str) or not c for c in carriers)
                or not all(callable(c) for c in (duration,on_complete,eligible,on_start))):
            raise ValueError('invalid/duplicate service request')
        record={'id':request_id,'owner_id':self.id+':'+request_id,'kind':kind,'carriers':carriers,
                'queued_at_us':self.scheduler.now_us,'status':'pending','duration':duration,
                'on_complete':on_complete,'eligible':eligible,'on_start':on_start,
                'started_at_us':None,'completed_at_us':None}
        self.requests[request_id]=record;self._pending[request_id]=record
        self._pending_order=();self._pending_signature=None
        if self._pending_changed is not None:self._pending_changed(self)
        self.dirty()
        return request_id

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

    def rebuild_derived(self):
        self._pending={rid:r for rid,r in self.requests.items() if r['status']=='pending'}
        self._pending_order=();self._pending_signature=None
        if self._pending_changed is not None:self._pending_changed(self)

    def pending_requests(self):
        signature=tuple((rid,r['queued_at_us'],r['id']) for rid,r in self._pending.items())
        if signature!=self._pending_signature:
            self._pending_order=tuple(sorted(self._pending.values(),key=lambda r:(r['queued_at_us'],r['id'])))
            self._pending_signature=signature
        return self._pending_order

    def set_request_status(self,request_id,status):
        if status not in ('pending','active','complete','cancelled'):
            raise ValueError('invalid service request status')
        self._pending_order=();self._pending_signature=None
        r=self.requests[request_id];r['status']=status
        if status=='pending':self._pending[request_id]=r
        else:self._pending.pop(request_id,None)
        if self._pending_changed is not None:self._pending_changed(self)

    def _event(self,record):
        return {k:record[k] for k in ('id','owner_id','kind','carriers','queued_at_us','status','started_at_us','completed_at_us')}

    def dispatch(self):
        if self.active is not None or self.causes:return False
        for r in self.pending_requests():
            if r['status']!='pending' or any(c in self.claims for c in r['carriers']) or not r['eligible']():continue
            duration=r['duration']()
            if type(duration) is not int or duration<0:raise ValueError('invalid sampled service duration')
            if r['kind']=='exchange' and duration==0:raise ValueError('carrier exchange duration must be positive')
            for c in r['carriers']:self.claims[c]=r['owner_id']
            r.update(started_at_us=self.scheduler.now_us,duration_us=duration)
            self.set_request_status(r['id'],'active')
            self.active=r
            r['on_start']()
            from .actions import BoundAction, ActionRef
            completion=(r['on_complete'].registry.bind(ActionRef('service:complete',self.id,r['id']))
                if isinstance(r['on_complete'],BoundAction) else lambda:self._complete(r))
            r['timer']=Timer(self.scheduler,r['owner_id'],duration,completion)
            self.emit('service_started',self._event(r));return True
        return False

    def _complete(self,r):
        if self.active is not r or any(self.claims.get(c)!=r['owner_id'] for c in r['carriers']):
            raise RuntimeError('service completion ownership changed')
        for c in r['carriers']:del self.claims[c]
        r.update(completed_at_us=self.scheduler.now_us)
        self.set_request_status(r['id'],'complete')
        self.active=None
        self.emit('service_completed',self._event(r));r['on_complete']();self.dirty()

    def pause(self,cause_id):
        if not isinstance(cause_id,str) or not cause_id:raise ValueError('invalid downtime cause')
        if cause_id in self.causes:return
        self.causes.add(cause_id)
        if self.active:self.active['timer'].pause(self.scheduler.now_us,cause_id)

    def resume(self,cause_id):
        if cause_id not in self.causes:raise ValueError('unknown downtime cause')
        self.causes.remove(cause_id)
        if self.active:self.active['timer'].resume(self.scheduler.now_us,cause_id)
        self.dirty()



    def export_state(self) -> dict[str, object]:
        from .actions import export_action
        from copy import deepcopy
        requests={}
        for rid,r in self.requests.items():
            requests[rid]={k:(export_action(v) if k in ('duration','on_complete','eligible','on_start')
                else v.export_state() if k=='timer' else deepcopy(v)) for k,v in r.items()}
        return {'id':self.id,'requests':requests,'active':self.active['id'] if self.active else None,'causes':set(self.causes),'history_archive':deepcopy(self.history_archive)}

    @classmethod
    def restore(cls, scheduler, state: dict[str, object], actions, *, carrier_claims, dirty, emit):
        from .actions import restore_action
        from copy import deepcopy
        handler=cls(scheduler,state['id'],carrier_claims,dirty,emit)
        actions.services[handler.id]=handler
        handler.causes=set(state['causes'])
        handler.history_archive=deepcopy(state.get('history_archive'))
        for rid,r in state['requests'].items():
            handler.requests[rid]={k:(restore_action(v,actions) if k in ('duration','on_complete','eligible','on_start')
                else Timer.restore(scheduler,v,actions) if k=='timer' else deepcopy(v)) for k,v in r.items()}
        handler.active=handler.requests[state['active']] if state['active'] is not None else None
        handler.rebuild_derived()
        return handler


class Cascade:
    """Two internal slots for committed LOT or wafer units; no carrier moves."""
    def __init__(self,scheduler,pipeline_id,mode,current_setup,dirty,emit):
        if mode not in ('lot','wafer') or not isinstance(pipeline_id,str) or not pipeline_id:
            raise ValueError('invalid cascade mode/identity')
        self.scheduler,self.id,self.mode,self.current_setup,self.dirty,self.emit=scheduler,pipeline_id,mode,current_setup,dirty,emit
        self.units={};self.executions={};self.pending=[];self.slots=[None,None]
        self.units_archive=None;self.history_archive=None;self._history_lookup=None
        self.admission=None;self.causes=set();self.setup_active=False;self.setup_target=None;self.active_execution=None

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

    def enqueue_lot(self,lot_id,count,interval_us,duration,on_complete,setup_id,eligible=lambda:True,*,execution_id=None):
        execution_id=lot_id if execution_id is None else execution_id
        if (not isinstance(lot_id,str) or not lot_id or not isinstance(execution_id,str) or not execution_id or execution_id in self.executions
                or type(count) is not int or count<=0 or (self.mode=='lot' and count!=1)
                or type(interval_us) is not int or interval_us<=0
                or not all(callable(c) for c in (duration,on_complete,eligible))):
            raise ValueError('invalid/duplicate cascade commitment')
        self.executions[execution_id]={'lot_id':lot_id,'remaining':count}
        for index in range(count):
            uid=f'{execution_id}:{self.mode}:{index}'
            self.units[uid]={'id':uid,'lot_id':lot_id,'execution_id':execution_id,'index':index,'interval_us':interval_us,
                             'duration':duration,'on_complete':on_complete,'eligible':eligible,'setup_id':setup_id,
                             'status':'pending','started_at_us':None,'completed_at_us':None,'first_complete':False}
            self.pending.append(uid)
        self.dirty()

    def _timers(self):
        values=[self.admission] if self.admission else []
        for uid in self.slots:
            if uid:values.append(self.units[uid]['timer'])
        return values

    def _event(self,u):
        return {k:u[k] for k in ('id','lot_id','execution_id','index','status','started_at_us','completed_at_us')}

    def dispatch(self):
        if self.causes or self.setup_active:return False
        changed=self._advance()
        if self.slots[0] is not None or (self.admission and not self.admission.completed):return changed
        for uid in self.pending:
            u=self.units[uid]
            if (u['setup_id']!=self.current_setup or not u['eligible']()
                    or (self.mode=='wafer' and self.active_execution not in (None,u['execution_id']))):continue
            if self.mode=='lot' and any(self.units[v]['lot_id']==u['lot_id'] for v in self.slots if v):continue
            p=u['duration']()
            if type(p) is not int or p<0:raise ValueError('invalid sampled cascade duration')
            self.pending.remove(uid)
            if self.mode=='wafer':self.active_execution=u['execution_id']
            a=min(p,u['interval_us']);b=p-a
            u.update(status='slot1',started_at_us=self.scheduler.now_us,processing_us=p,
                     first_service_us=a,second_service_us=b)
            self.slots[0]=uid
            u['timer']=Timer(self.scheduler,self.id+':slot1:'+uid,a,native_action(self,'cascade:first',self.id,uid,lambda uid=uid:self._first_complete(uid)))
            self.admission=Timer(self.scheduler,self.id+':interval:'+uid,u['interval_us'],native_action(self,'cascade:dirty',self.id,None,self.dirty))
            self.emit('cascade_unit_started',self._event(u));return True
        return changed

    def _advance(self):
        # This is causal completion of already admitted work, not new dispatch.
        # Free slot-2 transfer (including b=0) settles before phase-2 faults.
        if self.causes or self.setup_active:return False
        if self.slots[0] and self.units[self.slots[0]]['first_complete'] and self.slots[1] is None:
            uid=self.slots[0];u=self.units[uid]
            self.slots=[None,uid];u.update(status='slot2',second_started_at_us=self.scheduler.now_us)
            u['timer']=Timer(self.scheduler,self.id+':slot2:'+uid,u['second_service_us'],native_action(self,'cascade:complete',self.id,uid,lambda uid=uid:self._complete(uid)))
            self.emit('cascade_slot2_started',self._event(u));return True
        return False

    def _first_complete(self,uid):
        if self.slots[0]!=uid:raise RuntimeError('first-slot ownership changed')
        self.units[uid]['first_complete']=True;self._advance();self.dirty()

    def _complete(self,uid):
        if self.slots[1]!=uid:raise RuntimeError('second-slot ownership changed')
        u=self.units[uid];self.slots[1]=None
        u.update(status='complete',completed_at_us=self.scheduler.now_us)
        execution=self.executions[u['execution_id']];execution['remaining']-=1
        if self.mode=='wafer' and execution['remaining']==0:self.active_execution=None
        self.emit('cascade_unit_completed',self._event(u));u['on_complete'](uid);self._advance();self.dirty()

    def begin_setup(self,target):
        if (any(self.slots) or self.setup_active or self.causes
                or (self.mode=='wafer' and self.active_execution is not None)):
            raise RuntimeError('setup requires available drained pipeline and committed wafer LOT')
        self.setup_active=True;self.setup_target=target
        if self.admission:self.admission.pause(self.scheduler.now_us,'setup')

    def end_setup(self):
        if not self.setup_active:raise RuntimeError('no active setup')
        self.current_setup=self.setup_target;self.setup_active=False;self.setup_target=None
        if self.admission and not self.admission.completed:self.admission.resume(self.scheduler.now_us,'setup')
        self.dirty()

    def pause(self,cause_id):
        if not isinstance(cause_id,str) or not cause_id:raise ValueError('invalid downtime cause')
        if cause_id in self.causes:return
        self.causes.add(cause_id)
        for timer in self._timers():timer.pause(self.scheduler.now_us,'fault:'+cause_id)

    def resume(self,cause_id):
        if cause_id not in self.causes:raise ValueError('unknown downtime cause')
        self.causes.remove(cause_id)
        for timer in self._timers():
            if not timer.completed:timer.resume(self.scheduler.now_us,'fault:'+cause_id)
        self.dirty()


# Captured identities make unknown service contracts take the exhaustive path.
_NATIVE_SERVICE_DISPATCH = SerializedHandler.dispatch
_NATIVE_PENDING_REQUESTS = SerializedHandler.pending_requests


def service_dispatch_required(handler):
    """Dispatch only native runnable lanes; custom hooks remain exhaustive.

    These exits depend solely on current lane state, not cached eligibility.
    A queued request, completion or repair is therefore visible immediately,
    including work enqueued by an earlier callback in the same ordered pass.
    """
    if (type(handler) is SerializedHandler
            and getattr(handler.dispatch,'__func__',None) is _NATIVE_SERVICE_DISPATCH
            and getattr(handler.pending_requests,'__func__',None) is _NATIVE_PENDING_REQUESTS
            and type(handler._pending) is dict
            and (handler.active is not None or handler.causes or not handler._pending)):
        return False
    return True


def dispatch_service(handler):
    return handler.dispatch() if service_dispatch_required(handler) else False
