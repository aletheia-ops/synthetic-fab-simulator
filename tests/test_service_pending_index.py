from test_service import handlers
from test_equipment_exchange import factory
from test_manufacturing_facade import place


def membership(handler):
    assert set(handler._pending) == {rid for rid,r in handler.requests.items() if r['status']=='pending'}
    assert all(handler._pending[rid] is handler.requests[rid] for rid in handler._pending)


def test_pending_order_skips_blocked_head_and_preserves_id_ties():
    s,h,_,claims,_=handlers();order=[];draws=[]
    for rid in ('blocked','z','a'):
        h.request(rid,'load',[],lambda rid=rid:draws.append(rid) or 1,
                  lambda rid=rid:order.append(rid),eligible=lambda rid=rid:rid!='blocked')
    s.settle(0);membership(h)
    assert h.active['id']=='a' and draws==['a']
    s.settle(1);s.settle(2);membership(h)
    assert order==['a','z'] and list(h._pending)==['blocked']


def test_completion_callback_inserts_same_timestamp_request():
    s,h,_,_,_=handlers();order=[]
    def completed():
        order.append('first')
        h.request('next','load',[],lambda:0,lambda:order.append('next'))
    h.request('first','load',[],lambda:0,completed)
    s.settle(0)
    assert order==['first','next']
    membership(h);assert not h._pending


def test_exchange_cancel_removes_pending_and_active_membership():
    exchange,m,s,c,lots,events=factory(10)
    m.enter_visit('low','1');place(c,'clow','in')
    exchange.request('input','clow','in','rack')
    h=exchange.handlers['t'];membership(h)
    assert list(h._pending)==['input']
    exchange.cancel('input');membership(h);assert not h._pending


def test_completed_history_does_not_expand_pending_work():
    s,h,_,_,_=handlers()
    for n in range(10000):
        h.request(str(n),'load',[],lambda:0,lambda:None)
        s.settle(0)
        assert not h._pending
    assert len(h.requests)==10000
    # A historical dictionary that forbids enumeration proves dispatch no longer
    # sorts/scans completed records; constant live population alone cannot prove it.
    class NoHistoricalScan(dict):
        def values(self):
            raise AssertionError('history scanned during dispatch')
    h.requests=NoHistoricalScan(h.requests)
    h.request('new','load',[],lambda:0,lambda:None)
    s.settle(0)
    assert h.requests['new']['status']=='complete' and not h._pending


def test_rebuild_derived_restores_pending_order_and_aliases():
    s,h,_,_,_=handlers()
    h.request('z','load',[],lambda:1,lambda:None)
    h.request('a','load',[],lambda:1,lambda:None)
    h._pending={}
    h.rebuild_derived()
    assert [r['id'] for r in h.pending_requests()]==['a','z']
    membership(h)
