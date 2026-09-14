"""Exact decimal parsing of Kalshi's fixed-point fields."""

from __future__ import annotations

from decimal import Decimal

import pytest

from kalshi_router.errors import SchemaError
from kalshi_router.fixedpoint import parse_fixed_point


def test_count_fp_ten_dot_zero_zero_is_ten():
    parsed = parse_fixed_point("10.00", "count_fp")
    assert parsed.value == Decimal("10.00")
    assert parsed.value == 10
    assert parsed.source_type == "string"


def test_trailing_zeros_are_preserved_exactly():
    assert str(parse_fixed_point("0.6500", "yes_price_dollars").value) == "0.6500"


def test_subpenny_tick_is_representable():
    assert parse_fixed_point("0.0010", "yes_price_dollars").value == Decimal("0.0010")


def test_no_binary_float_rounding_is_introduced():
    """0.1 + 0.2 == 0.3 must hold for these values, which floats cannot give."""
    a = parse_fixed_point("0.10", "yes_price_dollars").value
    b = parse_fixed_point("0.20", "yes_price_dollars").value
    assert a + b == parse_fixed_point("0.30", "yes_price_dollars").value


def test_fractional_contracts_are_exact():
    assert parse_fixed_point("0.25", "count_fp").value == Decimal("0.25")


def test_large_quantity_is_exact():
    assert parse_fixed_point("1234567.89", "count_fp").value == Decimal("1234567.89")


def test_negative_values_are_accepted():
    assert parse_fixed_point("-5.00", "count_fp").value == Decimal("-5.00")


@pytest.mark.parametrize("absent", [None, ""])
def test_absent_values_return_none(absent):
    assert parse_fixed_point(absent, "count_fp") is None


@pytest.mark.parametrize(
    "bad", ["1e5", "abc", "1.2.3", "1,000.00", "0x10", " ", "--1", "+1.0", "Infinity", "NaN"]
)
def test_malformed_strings_fail_closed(bad):
    with pytest.raises(SchemaError, match="count_fp"):
        parse_fixed_point(bad, "count_fp")


def test_boolean_fails_closed():
    with pytest.raises(SchemaError):
        parse_fixed_point(True, "count_fp")


@pytest.mark.parametrize("bad", [{"a": 1}, ["1.0"]])
def test_wrong_types_fail_closed(bad):
    with pytest.raises(SchemaError):
        parse_fixed_point(bad, "count_fp")


def test_json_integer_is_tolerated_and_marked():
    parsed = parse_fixed_point(10, "count_fp")
    assert parsed.value == Decimal(10) and parsed.source_type == "number"


def test_json_float_is_tolerated_without_binary_rounding_and_marked():
    parsed = parse_fixed_point(0.65, "yes_price_dollars")
    assert parsed.value == Decimal("0.65")
    assert parsed.source_type == "number"
