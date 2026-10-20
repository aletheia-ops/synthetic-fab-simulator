from types import SimpleNamespace


def test_obligations_assessed_in_one_fresh_pass_with_order_preserved():
    from fab_sim.runner import assessed_obligations
    class OnePass(dict):
        def items(self):
            self.calls=getattr(self,'calls',0)+1
            assert self.calls==1
            return super().items()
    clocks=OnePass([
        ('open',{'closed_at_us':None,'violated':False}),
        ('late-open',{'closed_at_us':None,'violated':True}),
        ('closed',{'closed_at_us':5,'violated':False}),
        ('late-closed',{'closed_at_us':5,'violated':True})])
    assert assessed_obligations(clocks)==(['late-open','late-closed'],['open'])
    # The next assessment must observe a newly violated clock immediately.
    clocks['open']['violated']=True;clocks.calls=0
    assert assessed_obligations(clocks)==(['open','late-open','late-closed'],[])


def test_transfer_assessment_reads_each_active_status_once():
    from fab_sim.transfers import Transfers
    reads=[]
    class Record(dict):
        def __getitem__(self,key):
            if key=='status':reads.append(self)
            return super().__getitem__(key)
    owner=SimpleNamespace(demands={'a':Record(status='pending'),'b':Record(status='drop_wait')},
        active={'ca':'a','cb':'b'},vehicles={'v':{'carrier_slot_id':'vslot'}},
        custody=SimpleNamespace(slots={'vslot':{'carrier_id':'loaded'}}))
    assert Transfers.transfer_counts(owner)=={'pending_transfer_count':1,'committed_transfer_count':1,'loaded_transfer_count':1}
    assert len(reads)==2
