"""Incremental source releases and finite quotas; intent is not physical WIP."""
from collections import defaultdict
from fractions import Fraction
from heapq import heappop, heappush
from .inputs import validate_keys
from .sources.smt import decimal, integer
from .timebase import to_ticks
from .topology import index


def _weight(value):
    if isinstance(value,Fraction):
        if value<0: raise ValueError('negative product weight')
        return value
    return Fraction(decimal(value,'product weight'))


def allocate_quotas(count,weights):
    count=integer(count,'cohort count',True)
    if not isinstance(weights,dict) or any(not isinstance(p,str) or not p for p in weights):
        raise ValueError('product weights require product IDs')
    weights={p:_weight(v) for p,v in weights.items()}; total=sum(weights.values())
    if total<=0: raise ValueError('product weights total must be positive')
    exact={p:count*v/total for p,v in weights.items()}
    quotas={p:exact[p].numerator//exact[p].denominator for p in sorted(exact)}
    order=sorted(quotas,key=lambda p:(-(exact[p]-quotas[p]),p))
    for p in order[:count-sum(quotas.values())]: quotas[p]+=1
    return quotas


class ReleaseSource:
    def __init__(self,manufacturing,settings):
        validate_keys(settings,{'count','selected_streams','product_weights',
                                'release_time_scale','dual_route_validation'},'workload')
        # A count releases a fixed cohort of LOTs; without one, release goes on until the horizon.
        # The internal values 'finite'/'continuous' are part of the physical digest of saved
        # checkpoints, so they stay; outputs report the release mode as 'cohort'/'ongoing'.
        self.mode='finite' if 'count' in settings else 'continuous'
        self.count=integer(settings['count'],'cohort count',True) if self.mode=='finite' else None
        streams=index(manufacturing['model']['releases'],'release streams')
        products=sorted({s['product_id'] for s in streams.values()})
        names=settings.get('selected_streams',sorted(streams))
        if (not isinstance(names,list) or not names or len(names)!=len(set(names))
                or set(names)-set(streams)): raise ValueError('invalid selected release streams')
        self.streams={key:streams[key] for key in sorted(names)}
        rates={p:Fraction(0) for p in products}; self.source_intervals={}
        for s in self.streams.values():
            interval=Fraction(decimal(s['interval_seconds'],'source interval',True))
            integer(s['lots_per_release'],'lots_per_release',True)
            if type(s['first_at_us']) is not int or s['first_at_us']<0: raise ValueError('invalid source first release')
            self.source_intervals[s['id']]=interval
            rates[s['product_id']]+=Fraction(s['lots_per_release'])/interval
        total=sum(rates.values()); self.source_weights={p:rate/total for p,rate in rates.items()}
        raw=settings.get('product_weights',self.source_weights)
        if not isinstance(raw,dict) or set(raw)-set(products): raise ValueError('unknown product weights')
        weights={p:_weight(raw.get(p,0)) for p in products}; weight_sum=sum(weights.values())
        if weight_sum<=0: raise ValueError('product weights total must be positive')
        self.weights={p:v/weight_sum for p,v in weights.items()}
        if any(self.weights[p]>0 and rates[p]==0 for p in products):
            raise ValueError('positive product weight has no included source stream')
        self.quotas=allocate_quotas(self.count,self.weights) if self.mode=='finite' else None
        dual=settings.get('dual_route_validation',False)
        if type(dual) is not bool: raise ValueError('dual_route_validation must be boolean')
        if dual and (self.quotas is None or len(self.quotas)<2 or any(q==0 for q in self.quotas.values())):
            raise ValueError('dual-route validation requires positive finite quotas for both products')
        self.scale=Fraction(decimal(settings.get('release_time_scale',1),'release_time_scale',True))
        self.effective_intervals={s['id']:self.source_intervals[s['id']]*self.scale*
                                  self.source_weights[s['product_id']]/self.weights[s['product_id']]
                                  for s in self.streams.values() if self.weights[s['product_id']]>0}
        self.pending_streams=[]; self.sequence=0; self.realized_classes=defaultdict(int)
        self.realized_products={p:0 for p in products}
        for key in sorted(self.effective_intervals): self._push(key,0)

    def _push(self,key,index):
        s=self.streams[key]; occurrence=index//s['lots_per_release']
        scheduled=to_ticks(Fraction(s['first_at_us'],1000000)+occurrence*self.effective_intervals[key])
        heappush(self.pending_streams,(scheduled,key,index))

    def next_intent(self):
        while self.pending_streams:
            at,key,occurrence=heappop(self.pending_streams); s=self.streams[key]; product=s['product_id']
            if self.quotas is not None and self.realized_products[product]>=self.quotas[product]: continue
            self.sequence+=1; self.realized_classes[key]+=1; self.realized_products[product]+=1
            if self.quotas is None or self.realized_products[product]<self.quotas[product]: self._push(key,occurrence+1)
            return {'lot_id':f'{key}:{occurrence}','product_id':product,'route_id':s['route_id'],
                    'stream_id':key,'occurrence_index':occurrence,'source_occurrence_index':occurrence//s['lots_per_release'],
                    'release_sequence':self.sequence,'scheduled_at_us':at,'due_at_us':at+s['due_allowance_us'],
                    'due_allowance_us':s['due_allowance_us'],'priority':s['priority'],'super_hot':s['super_hot'],
                    'wafer_count':s['wafer_count']}
        return None

    def manifest(self):
        return {'policy':'source_rates_largest_remainder_v1','mode':'cohort' if self.mode=='finite' else 'ongoing','count':self.count,
                'selected_streams':sorted(self.streams),'source_weights':{p:str(w) for p,w in self.source_weights.items()},
                'effective_weights':{p:str(w) for p,w in self.weights.items()},'release_time_scale':str(self.scale),
                'effective_interval_seconds':{s:str(t) for s,t in self.effective_intervals.items()},
                'quotas':self.quotas,'realized_class_counts':dict(self.realized_classes)}


def admit(intent: dict,state: dict) -> bool:
    if intent['lot_id'] in state['lots']: raise ValueError('duplicate physical LOT admission')
    if type(state['now_us']) is not int or state['now_us']<intent['scheduled_at_us']:
        raise ValueError('physical admission precedes release intent')
    custody=state['custody']; owner='admission:'+intent['lot_id']
    available=next((slot for slot in sorted(state['entry_slots']) if custody.available(slot)),None)
    if available is None: return False
    carrier_id=intent['lot_id']+':carrier'
    if carrier_id in custody.locations: raise ValueError('duplicate physical carrier')
    with custody.batch_notifications():
        if not custody.reserve(available,owner): return False
        custody.place(carrier_id,available,owner)
        state['lots'][intent['lot_id']]={**intent,'carrier_id':carrier_id,'admitted_at_us':state['now_us'],
                                       'admission_delay_us':state['now_us']-intent['scheduled_at_us'],
                                       'status':'admitted','route_complete_at_us':None,'exited_at_us':None}
    return True
