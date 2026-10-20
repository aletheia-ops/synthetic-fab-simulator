"""Fixed dispatch table for native physical actions, never supplied by a bundle."""
from .actions import ActionRef


def native_action(owner, kind, identity, payload, fallback):
    actions = getattr(owner, 'actions', None)
    return actions.bind(ActionRef(kind, identity, payload)) if actions else fallback


def bind_native_actions(owners, actions):
    m,t,traffic = owners.manufacturing,owners.transfers,owners.traffic
    for owner in (m,t,t.exchange,traffic,owners.handoffs): owner.actions=actions
    actions.register('noop', lambda owner,payload: None)
    actions.register('true', lambda owner,payload: True)
    actions.register('constant', lambda owner,payload: payload)
    actions.register('manufacturing:loaded', lambda eid,p:m._loaded(m.executions[eid]))
    actions.register('manufacturing:unload', lambda eid,p:m._unload(m.executions[eid]))
    actions.register('manufacturing:process', lambda eid,p:m.processing_complete(eid))
    actions.register('manufacturing:setup-duration', lambda eid,p:_setup_duration(m,eid,p))
    actions.register('manufacturing:setup-complete', lambda eid,p:_setup_complete(m,eid,p))
    actions.register('manufacturing:setup-start', lambda eid,p:_setup_start(m,eid,p))
    actions.register('manufacturing:cascade-sample', lambda eid,p:m._cascade_sample(m.executions[eid]))
    actions.register('manufacturing:cascade-complete', lambda eid,p,uid:m._cascade_unit_complete(m.executions[eid],uid))
    actions.register('manufacturing:cascade-eligible', lambda eid,p:_cascade_eligible(m,eid))
    actions.register('exchange:duration', lambda rid,p:t.exchange.durations[t.exchange.requests[rid]['tool_id']])
    actions.register('exchange:complete', lambda rid,p:t.exchange._complete(t.exchange.requests[rid]))
    actions.register('exchange:eligible', lambda rid,p:t.exchange._eligible(t.exchange.requests[rid]))
    actions.register('exchange:start', lambda rid,p:t.exchange._start(t.exchange.requests[rid]))
    actions.register('transfer:exchanged', lambda did,p:t._exchanged(t.demands[did]))
    actions.register('transfer:handled', lambda did,p:t._handled(t.demands[did],p))
    actions.register('transfer:admission-valid', lambda did,p:t._admission_valid(t.demands[did]))
    actions.register('handoff:start', lambda did,p:owners.handoffs.output_started(did))
    actions.register('handoff:complete', lambda did,p:owners.handoffs.output_completed(did))
    actions.register('traffic:arrive', lambda rid,p:traffic._arrive(traffic.requests[rid]))
    actions.register('movement:start', lambda mid,p:t.movement_started(mid))
    actions.register('movement:complete', lambda mid,p:t.arrived(t.movements[mid]['demand_id'],t.movements[mid]['phase']))
    actions.register('movement:cancel', lambda mid,p:t.cancelled_arrived(mid))
    actions.register('cascade:first', lambda pid,p:actions.pipelines[pid]._first_complete(p))
    actions.register('cascade:complete', lambda pid,p:actions.pipelines[pid]._complete(p))
    actions.register('cascade:dirty', lambda pid,p:actions.pipelines[pid].dirty())
    actions.native_kinds=frozenset(actions.handlers)
    actions.pipelines={}
    for handler in (*m.handlers.values(),*t.exchange.handlers.values()):
        handler.actions=actions;actions.services[handler.id]=handler
    for pipeline in m.pipelines.values():
        pipeline.actions=actions;actions.pipelines[pipeline.id]=pipeline


def _setup_duration(m,eid,payload):
    from .service import setup_duration_us
    return setup_duration_us(payload['decision'],payload['lot_ids'],m.streams)


def _setup_start(m,eid,decision):
    tid=m.executions[eid]['tool_id']
    if tid in m.pipelines:m.pipelines[tid].begin_setup(decision['new'])


def _setup_complete(m,eid,decision):
    tid=m.executions[eid]['tool_id'];m.setups[tid].apply(decision)
    if tid in m.pipelines:m.pipelines[tid].end_setup()
    m._load(m.executions[eid])


def _cascade_eligible(m,eid):
    tid=m.executions[eid]['tool_id']
    return m.tools[tid].get('available',True) and not m.causes[tid]


def validate_native_action(o,ref):
    """Validate durable action references after canonical owners are populated."""
    from .checkpoint_format import CheckpointError
    m,t=o.manufacturing,o.transfers
    kind,identity,payload=ref.kind,ref.owner_id,ref.payload
    try:
        if kind in ('noop','true','constant'):
            valid=identity=='' and (type(payload) is int and payload>=0 if kind=='constant' else payload is None)
        elif kind=='service:complete':
            valid=(identity in o.actions.services and type(payload) is str
                and payload in o.actions.services[identity].requests)
        elif kind.startswith('manufacturing:'):
            valid=identity in m.executions
            if kind=='manufacturing:setup-duration':
                valid=valid and type(payload) is dict and set(payload)=={'decision','lot_ids'}
                if valid:
                    ids=payload['lot_ids']
                    valid=(type(ids) is list and bool(ids) and all(type(i) is str and i in m.lots for i in ids)
                        and len(ids)==len(set(ids)) and _valid_setup_decision(payload['decision']))
            elif kind in ('manufacturing:setup-start','manufacturing:setup-complete'):
                valid=valid and _valid_setup_decision(payload)
            else:valid=valid and payload is None
        elif kind.startswith('exchange:'):valid=identity in t.exchange.requests and payload is None
        elif kind.startswith('transfer:'):
            valid=identity in t.demands and (payload in ('pickup','drop') if kind=='transfer:handled' else payload is None)
        elif kind.startswith('handoff:'):valid=identity in o.handoffs.records and payload is None
        elif kind=='traffic:arrive':valid=identity in o.traffic.requests and payload is None
        elif kind.startswith('movement:'):valid=identity in t.movements and payload is None
        elif kind.startswith('cascade:'):
            valid=identity in o.actions.pipelines
            if kind=='cascade:dirty':valid=valid and payload is None
            else:valid=valid and type(payload) is str and payload in o.actions.pipelines[identity].units
        else:valid=False
        if kind not in o.actions.native_kinds or not valid:raise ValueError('unknown owner or invalid payload')
    except (ValueError,TypeError,KeyError) as error:
        raise CheckpointError('invalid native action '+str(kind)+': '+str(error)) from error


def _valid_setup_decision(value):
    from fractions import Fraction
    fields={'required','current','new','minimum_runs','duration_us','expected_us','distribution'}
    return (type(value) is dict and set(value)==fields and type(value['required']) is bool
        and all(v is None or type(v) is str for v in (value['current'],value['new']))
        and type(value['minimum_runs']) is int and value['minimum_runs']>=0
        and (value['duration_us'] is None or type(value['duration_us']) is int and value['duration_us']>=0)
        and isinstance(value['expected_us'],Fraction) and value['expected_us']>=0
        and (value['distribution'] is None or type(value['distribution']) is dict))
