"""Artificial extra processing time (declared mock-inefficiency hook, docs/settings.md).

Default off. Each rule selects (route, step) pairs by route, optional area, tool
group or source step; at each selected execution start (LOT, batch, or cascade
unit) it delays the execution with its probability, by an extra time relative to
the sampled duration or absolute. Draws use one 'perturbation' stream per rule,
never the processing streams, so unaffected draws match an unperturbed run.

Cost: selection is precomputed to a (route_id, step_id) -> rules table, so an
execution start costs one dict lookup, plus one draw per selected rule.
"""
from fractions import Fraction
from .rng import sample_seconds
from .timebase import to_ticks

RULE_KEYS = {'id', 'routes', 'areas', 'tool_groups', 'steps', 'probability', 'extra'}


def _fraction(value, what, low=None, high=None):
    if not isinstance(value, str): raise ValueError(what + ' must be a decimal string')
    number = Fraction(value)
    if low is not None and number < low or high is not None and number > high: raise ValueError(what + ' out of range')
    return number


def validate(rules):
    if not isinstance(rules, list) or not rules: raise ValueError('extra_processing must be a non-empty list')
    seen = set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) - RULE_KEYS or not {'id', 'routes', 'probability', 'extra'} <= set(rule):
            raise ValueError('extra_processing rule needs id, routes, probability, extra')
        if not isinstance(rule['id'], str) or not rule['id'] or rule['id'] in seen: raise ValueError('invalid or duplicate rule id')
        seen.add(rule['id'])
        for key in ('routes', 'areas', 'tool_groups', 'steps'):
            if key in rule and (not isinstance(rule[key], list) or not rule[key] or not all(isinstance(x, str) and x for x in rule[key])):
                raise ValueError(f'extra_processing {key} must be a non-empty list of names')
        _fraction(rule['probability'], 'probability', 0, 1)
        extra = rule['extra']
        if not isinstance(extra, dict) or extra.get('kind') not in ('relative', 'absolute'): raise ValueError('extra.kind must be relative or absolute')
        spec = extra.get('factor' if extra['kind'] == 'relative' else 'seconds')
        if not isinstance(spec, dict): raise ValueError('extra needs factor (relative) or seconds (absolute)')
        import random
        sample_seconds(_spec(spec), random.Random(0))
    return rules


def _spec(spec):
    """Source vocabulary: distribution with mean/offset (uniform is mean +- offset)."""
    return {'distribution': spec.get('distribution'), 'mean_seconds': spec.get('mean'), 'offset_seconds': spec.get('offset', '0')}


class ExtraProcessing:
    def __init__(self, rules, routes, groups, streams, emit):
        self.streams, self.emit = streams, emit
        self.table = {}
        for rule in validate(rules):
            compiled = {**rule, 'p': Fraction(rule['probability']), 'spec': _spec(rule['extra'].get('factor') or rule['extra'].get('seconds')),
                        'relative': rule['extra']['kind'] == 'relative'}
            for route_id in rule['routes']:
                if route_id not in routes: raise ValueError('extra_processing: unknown route ' + route_id)
                for step in routes[route_id]['steps']:
                    group = groups.get(step.get('tool_group_id'), {})
                    if ('areas' in rule and group.get('area') not in rule['areas']
                            or 'tool_groups' in rule and step.get('tool_group_id') not in rule['tool_groups']
                            or 'steps' in rule and step['source_step_id'] not in rule['steps']):
                        continue
                    self.table.setdefault((route_id, step['source_step_id']), []).append(compiled)

    def extra_us(self, route_id, step_id, base_us, *, execution_id, tool_id, lot_ids, unit=None):
        rules = self.table.get((route_id, step_id))
        if not rules: return 0
        total = 0
        for rule in rules:
            rng = self.streams.get('perturbation', rule['id'])
            if Fraction(rng.random()) >= rule['p']: continue
            sampled = Fraction(sample_seconds(rule['spec'], rng))
            extra = to_ticks(sampled * Fraction(base_us, 1000000)) if rule['relative'] else to_ticks(sampled)
            total += extra
            self.emit('processing_perturbed', {'rule_id': rule['id'], 'execution_id': execution_id, 'tool_id': tool_id,
                'lot_ids': list(lot_ids), 'route_id': route_id, 'source_step_id': step_id, 'unit': unit,
                'base_us': base_us, 'extra_us': extra})
        return total
