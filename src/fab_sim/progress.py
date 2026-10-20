"""Manufacturing observations, isolated from event ordering and stop policy."""
import math
import time
from copy import deepcopy

DAY_US = 86_400_000_000


class Progress:
    def __init__(self, lots, visits, products, *, report=None, interval=15,
                 clock=time.monotonic, context=lambda: {},retired=None):
        if (isinstance(interval, bool) or not isinstance(interval, (int, float))
                or not math.isfinite(interval) or interval <= 0 or not callable(clock)
                or report is not None and not callable(report) or not callable(context)):
            raise ValueError('invalid progress observation policy')
        self.lots, self.visits = lots, visits
        self.products = tuple(sorted(products))
        self.counts = {product: {'admitted': 0, 'max_entered_step': None, 'exited': 0}
                       for product in self.products}
        self.retired=deepcopy(retired or {'total':0,'products':{},'root_consumed':{}})
        self.exited_count = self.retired['total']
        for product,item in self.retired['products'].items():
            if product in self.counts:self.counts[product]={k:item[k] for k in ('admitted','exited','max_entered_step')}
        # Seed once for callers attaching an observer to existing state.
        for lot in lots.values():
            self.exited_count += lot.get('exited_at_us') is not None
            if lot['product_id'] in self.counts:
                counts=self.counts[lot['product_id']]
                counts['admitted'] += lot.get('admitted_at_us') is not None
                counts['exited'] += lot.get('exited_at_us') is not None
        for visit in visits.values():self._entered(visit)
        self.report, self.interval, self.clock, self.context = report, interval, clock, context
        self.last = {kind: None for kind in ('transfer_delivered', 'processing_completed', 'lot_exited')}
        self.first_exits = {product:min((value for value in [self.retired['products'].get(product,{}).get('first_exit_at_us'),
            *(lot.get('exited_at_us') for lot in lots.values() if lot['product_id']==product)] if value is not None),default=None) for product in self.products}
        self.next_report = None
        self.report_errors = 0

    def observe(self, kind, payload, now_us):
        if kind in self.last:
            self.last[kind] = now_us
        if kind == 'lot_exited':
            product = self.lots[payload['lot_id']]['product_id']
            self.exited_count += 1
            self.counts[product]['exited'] += 1
            if self.first_exits[product] is None:
                self.first_exits[product] = now_us
        elif kind == 'lot_admitted':
            if payload['product_id'] in self.counts:
                self.counts[payload['product_id']]['admitted'] += 1
        elif kind == 'visit_entered':self._entered(payload)

    def _entered(self, visit):
        product=self.lots[visit['lot_id']]['product_id'];step=visit['source_step_id']
        if product in self.counts and str(step).isdigit():
            counts=self.counts[product]
            counts['max_entered_step']=max(int(step),counts['max_entered_step'] or 0)

    def snapshot(self, now_us):
        counts = deepcopy(self.counts)
        milestone = (max(self.first_exits.values()) if self.first_exits
            and all(value is not None for value in self.first_exits.values()) else None)
        latest = max((value for value in self.last.values() if value is not None), default=0)
        return {'schema': 'fab-progress-v1', 'simulated_days': now_us / DAY_US,
            'now_us': now_us, 'products': counts, 'last_progress_at_us': dict(self.last),
            'last_progress_at_simulated_day': {k: None if v is None else v / DAY_US for k, v in self.last.items()},
            'first_exit_at_us': dict(self.first_exits), 'first_of_each_kind_at_us': milestone,
            'inactivity_warning': now_us - latest >= DAY_US,
            **self.context()}

    def maybe_report(self, now_us, settlements, *, force=False):
        if self.report is None: return
        try:
            wall = self.clock()
            if not isinstance(wall, (int, float)) or not math.isfinite(wall): return
            if not force and self.next_report is not None and wall < self.next_report: return
            self.next_report = wall + self.interval
            self.report({**self.snapshot(now_us), 'settlements': settlements})
        except Exception:
            # Reporting is optional observation; output ownership is unchanged.
            self.report_errors += 1


    def export_state(self) -> dict[str, object]:
        return deepcopy({k:getattr(self,k) for k in ('products','counts','exited_count','last','first_exits')})

    def restore_state(self, state: dict[str, object]) -> None:
        from .checkpoint_format import CheckpointError
        if (set(state)!={'products','counts','exited_count','last','first_exits'}
                or state['products']!=self.products or set(state['counts'])!=set(self.products)
                or set(state['first_exits'])!=set(self.products) or set(state['last'])!=set(self.last)
                or type(state['exited_count']) is not int or state['exited_count']<0):
            raise CheckpointError('invalid progress continuation')
        expected_first={p:min((value for value in [self.retired['products'].get(p,{}).get('first_exit_at_us'),
            *(lot.get('exited_at_us') for lot in self.lots.values() if lot['product_id']==p)] if value is not None),default=None) for p in self.products}
        if (state['counts']!=self.counts or state['exited_count']!=self.exited_count
                or state['first_exits']!=expected_first or any(
                    value is not None and (type(value) is not int or value<0) for value in state['last'].values())):
            raise CheckpointError('progress counters differ from retained lot/visit ledger')
        for k,v in deepcopy(state).items():setattr(self,k,v)
        self.next_report=None
