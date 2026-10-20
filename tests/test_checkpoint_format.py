from decimal import Decimal
import json
import math
import pytest
from fab_sim.checkpoint_format import encode_value, decode_value, CheckpointError


@pytest.mark.parametrize('value', [None, True, False, 2**80, -0.0, 1.25,
    Decimal('1.2300'), Decimal('-0.00'), (1, 'x', ()), [1, False],
    {(1, 'x'): {3, 2}, 'empty': frozenset()}, b'\x00\xff'])
def test_exact_round_trip(value):
    result = decode_value(encode_value(value))
    assert type(result) is type(value)
    assert result == value
    if isinstance(value, Decimal):
        assert result.as_tuple() == value.as_tuple()
    if isinstance(value, float):
        assert result.hex() == value.hex()


def test_deterministic_container_order():
    assert encode_value({'b': 2, 'a': 1}) == encode_value({'a': 1, 'b': 2})
    assert encode_value({3, 1, 2}) == encode_value({2, 3, 1})


@pytest.mark.parametrize('value', [object(), lambda: None, math.inf, math.nan, Decimal('NaN')])
def test_unsupported_values_rejected(value):
    with pytest.raises(CheckpointError):
        encode_value(value)


@pytest.mark.parametrize('data', [b'garbage', b'["unknown", 1]', b'["int", "01"]',
    b'["dict", [[["str", "x"], ["int", "1"]], [["str", "x"], ["int", "2"]]]]',
    b'["float", "nan"]', b'["bool", 1]'])
def test_malformed_values_rejected(data):
    with pytest.raises(CheckpointError):
        decode_value(data)


def test_cycles_rejected():
    value = []; value.append(value)
    with pytest.raises(CheckpointError):
        encode_value(value)
