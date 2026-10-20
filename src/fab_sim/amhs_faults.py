"""Validated deterministic project faults, independent of native equipment clocks."""
class AMHSFaults:
    def __init__(self, traffic, injected_faults, *, initialize=True):
        self.traffic=traffic;self.scheduler=traffic.scheduler;self.records={}
        if not isinstance(injected_faults,list):raise ValueError('AMHS faults must be a list')
        keys={'id','target_type','target_id','onset_us','duration_us'}
        for record in injected_faults:
            if (not isinstance(record,dict) or set(record)!=keys or
                    not isinstance(record['id'],str) or not record['id'] or record['id'] in self.records or
                    not isinstance(record['target_id'],str) or not isinstance(record['target_type'],str) or
                    record['target_type'] not in traffic.causes or record['target_id'] not in traffic.causes[record['target_type']] or
                    type(record['onset_us']) is not int or record['onset_us']<self.scheduler.now_us or
                    type(record['duration_us']) is not int or record['duration_us']<=0):
                raise ValueError('invalid/duplicate AMHS injected fault')
            self.records[record['id']]={**record,'status':'scheduled'}
        # Register only after the complete list passes validation.
        for rid,r in self.records.items():
            start='amhs-fault:start:'+rid;recover='amhs-fault:recover:'+rid
            self.scheduler.register(start,lambda payload,r=r:self._start(r))
            self.scheduler.register(recover,lambda payload,r=r:self._recover(r))
            if initialize:
                self.scheduler.schedule(r['onset_us'],2,start,{})
                self.scheduler.schedule(r['onset_us']+r['duration_us'],1,recover,{})

    def _start(self, r):
        self.traffic.pause(r['target_id'],r['id'],target_type=r['target_type']);r['status']='active'
        self.scheduler.unregister('amhs-fault:start:'+r['id'])

    def _recover(self, r):
        self.traffic.recover(r['target_id'],r['id'],target_type=r['target_type']);r['status']='recovered'
        self.scheduler.unregister('amhs-fault:recover:'+r['id'])
