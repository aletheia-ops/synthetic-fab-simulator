"""Exact microsecond policy: nonnegative duration, nearest ties to even."""
from decimal import Decimal
from fractions import Fraction

def to_ticks(value: Decimal | Fraction | int) -> int:
    if isinstance(value,bool) or not isinstance(value,(Decimal,Fraction,int)):
        raise ValueError('duration must be an exact finite number')
    if isinstance(value,Decimal) and not value.is_finite():
        raise ValueError('duration must be finite')
    number=Fraction(value)
    if number<0:raise ValueError('duration must be nonnegative')
    scaled=number*1_000_000
    whole,remainder=divmod(scaled.numerator,scaled.denominator)
    if 2*remainder>scaled.denominator or (2*remainder==scaled.denominator and whole%2):whole+=1
    return whole
