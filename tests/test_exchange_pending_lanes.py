from types import SimpleNamespace
from unittest.mock import Mock
from fab_sim.scheduler import Scheduler
from fab_sim.transfers import EquipmentExchange


def fixture():
    scheduler=Scheduler()
    manufacturing=SimpleNamespace(scheduler=scheduler, claims={}, tools=dict.fromkeys(('a','b','c')),
        custody=SimpleNamespace(subscribe=lambda callback:None))
    exchange=EquipmentExchange(manufacturing,lambda kind,payload:None)
    scheduler.register_pass('transfer',exchange.dispatch)
    return exchange


def enqueue(exchange,tool,identity,on_start=lambda:None,eligible=lambda:True):
    return exchange.handlers[tool].request(identity,'exchange',[],lambda:1,lambda:None,
        on_start=on_start,eligible=eligible)


def test_empty_lanes_are_not_dispatched_and_direct_handler_requests_are_seen():
    exchange=fixture()
    for handler in exchange.handlers.values():handler.dispatch=Mock(wraps=handler.dispatch)
    assert not exchange.dispatch()
    assert all(handler.dispatch.call_count==0 for handler in exchange.handlers.values())
    enqueue(exchange,'b','work')
    assert exchange.dispatch()
    assert exchange.handlers['b'].active['id']=='work'
    assert [exchange.handlers[t].dispatch.call_count for t in ('a','b','c')]==[0,1,0]


def test_same_pass_callback_visits_new_later_lane_but_defers_earlier_lane():
    exchange=fixture();order=[]
    def started():
        order.append('b')
        enqueue(exchange,'a','earlier',on_start=lambda:order.append('a'))
        enqueue(exchange,'c','later',on_start=lambda:order.append('c'))
    enqueue(exchange,'b','first',on_start=started)
    assert exchange.dispatch() and order==['b','c']
    assert exchange.dispatch() and order==['b','c','a']


def test_pending_blocked_and_faulted_lanes_retry_without_new_requests():
    exchange=fixture();ready=[False]
    enqueue(exchange,'a','blocked',eligible=lambda:ready[0])
    enqueue(exchange,'b','faulted');exchange.handlers['b'].pause('fault')
    assert not exchange.dispatch()
    ready[0]=True;exchange.handlers['b'].resume('fault')
    assert exchange.dispatch()
    assert all(exchange.handlers[t].active is not None for t in ('a','b'))


def test_cancellation_and_rebuild_do_not_retain_terminal_lanes():
    exchange=fixture();enqueue(exchange,'a','cancelled');enqueue(exchange,'c','live')
    exchange.handlers['a'].set_request_status('cancelled','cancelled')
    exchange.rebuild_derived()
    assert exchange._pending_tools=={'c'}
    assert exchange.dispatch() and not exchange._pending_tools


def test_callback_cancellation_skips_future_lane_and_exception_clears_frontier():
    exchange=fixture();enqueue(exchange,'c','future')
    enqueue(exchange,'a','first',on_start=lambda:exchange.handlers['c'].set_request_status('future','cancelled'))
    assert exchange.dispatch() and exchange.handlers['c'].active is None
    def failing():raise ValueError('eligibility failed')
    enqueue(exchange,'b','failure',eligible=failing)
    import pytest
    with pytest.raises(ValueError,match='eligibility failed'):exchange.dispatch()
    assert exchange._dispatch_frontier is None
    exchange.handlers['b'].set_request_status('failure','cancelled')
    assert not exchange.dispatch()


def test_handler_rebuild_refreshes_owner_pending_membership():
    exchange=fixture();enqueue(exchange,'b','live')
    exchange._pending_tools.clear()
    exchange.handlers['b'].rebuild_derived()
    assert exchange._pending_tools=={'b'}
    exchange.handlers['b'].requests['live']['status']='cancelled'
    exchange.rebuild_derived()
    assert not exchange._pending_tools and not exchange.handlers['b']._pending
