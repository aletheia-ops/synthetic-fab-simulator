"""Integer micrometre geometry. Original rails remain physical resources."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from math import isqrt


def spatial_um(value):
    if isinstance(value, bool) or value is None:
        raise ValueError('expected finite spatial value')
    try:
        number = Decimal(str(value))
        if not number.is_finite(): raise ValueError('expected finite spatial value')
        return int((number * 1000).to_integral_value(rounding=ROUND_HALF_EVEN))
    except InvalidOperation:
        raise ValueError('expected finite spatial value') from None


def chord_length(a, b):
    squared = sum((x-y)**2 for x, y in zip(a, b, strict=True))
    floor = isqrt(squared)
    comparison = 4*squared - (2*floor+1)**2
    length = floor + (comparison > 0 or (comparison == 0 and floor % 2 == 1))
    if length <= 0: raise ValueError('zero-length rail')
    return length


def overlaps(a, b):
    """Touching endpoints are permitted; interiors must not overlap."""
    return max(a[0], b[0]) < min(a[1], b[1])


_RAIL_INDEX = [None, None]


def _rail_index(rails):
    # Prepared rails are immutable for a run; reuse the index while the same
    # list object is passed (the list is retained, so its identity is stable).
    if _RAIL_INDEX[0] is not rails or len(_RAIL_INDEX[1]) != len(rails):
        _RAIL_INDEX[0] = rails; _RAIL_INDEX[1] = {r['id']: r for r in rails}
    return _RAIL_INDEX[1]


def swept_intervals(topology, sublinks, half_um):
    """Prepared hops start/end safely within a rail; intermediate rails are whole."""
    model = topology.get('model', topology)
    rails = _rail_index(model['rails'])
    claims = {}
    for i, part in enumerate(sublinks):
        length = rails[part['rail_id']]['length_um']
        low = part['start_um'] - half_um if i == 0 else part['start_um']
        high = part['end_um'] + half_um if i == len(sublinks)-1 else part['end_um']
        claims.setdefault(part['rail_id'], []).append((max(0, low), min(length, high)))
    for rail_id, intervals in claims.items():
        merged = []
        for low, high in sorted(intervals):
            if merged and low <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(high, merged[-1][1]))
            else: merged.append((low, high))
        claims[rail_id] = merged
    return claims
