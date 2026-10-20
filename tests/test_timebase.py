from decimal import Decimal
from fractions import Fraction
import pytest
from fab_sim.timebase import to_ticks

@pytest.mark.parametrize('value,expected',[(Decimal('0.0000005'),0),(Decimal('0.0000015'),2),(Fraction(1,3),333333),(Decimal('0.0000001'),0)])
def test_microsecond_quantization(value,expected):
    assert to_ticks(value)==expected

@pytest.mark.parametrize('value',[Decimal('-1'),Decimal('NaN'),Decimal('Infinity'),True])
def test_invalid_duration_rejects(value):
    with pytest.raises(ValueError):to_ticks(value)
