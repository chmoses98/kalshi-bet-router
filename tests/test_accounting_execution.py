"""Order aggregation: quantity-weighted pricing and fail-closed grouping."""

from __future__ import annotations

from decimal import Decimal

import pytest

from kalshi_router.accounting.execution import (
    OrderAggregationStats,
    aggregate_orders,
    weighted_average_price,
)
from kalshi_router.errors import SchemaError
from kalshi_router.models import normalize_fill

from .synthetic import make_accounting_fill


def fills(*specs):
    return [normalize_fill(make_accounting_fill(**spec)) for spec in specs]


def test_single_fill_order_summarizes_to_that_fill():
    orders = aggregate_orders(fills({"index": 1, "quantity": "25.00", "order_id": "O1"}))
    order = orders["O1"]
    assert order.total_quantity == Decimal("25.00")
    assert order.fill_count == 1
    assert order.is_partially_filled_order is False
    assert order.vwap_price == Decimal("0.5700")


def test_multi_fill_order_uses_quantity_weighted_average_not_arithmetic_mean():
    """The CEO's worked example: 40@0.57, 65@0.58, 71@0.59."""
    group = fills(
        {"index": 1, "quantity": "40.00", "yes_price": "0.5700", "order_id": "O1"},
        {"index": 2, "quantity": "65.00", "yes_price": "0.5800", "order_id": "O1"},
        {"index": 3, "quantity": "71.00", "yes_price": "0.5900", "order_id": "O1"},
    )
    order = aggregate_orders(group)["O1"]
    expected = (
        Decimal("40") * Decimal("0.57")
        + Decimal("65") * Decimal("0.58")
        + Decimal("71") * Decimal("0.59")
    ) / Decimal("176")
    assert order.total_quantity == Decimal("176.00")
    assert order.fill_count == 3
    assert order.is_partially_filled_order is True
    assert order.vwap_price == expected
    # The arithmetic mean is 0.58 exactly; the weighted average is not.
    assert order.vwap_price != Decimal("0.58")


def test_weighted_average_is_exact_decimal_not_float():
    group = fills(
        {"index": 1, "quantity": "1.00", "yes_price": "0.1000", "order_id": "O1"},
        {"index": 2, "quantity": "2.00", "yes_price": "0.2000", "order_id": "O1"},
    )
    # (1*0.10 + 2*0.20) / 3 = 0.50/3
    assert weighted_average_price(group) == Decimal("0.50") / Decimal(3)


def test_order_records_first_and_last_execution_times():
    group = fills(
        {"index": 2, "quantity": "1.00", "order_id": "O1", "minute": 5},
        {"index": 1, "quantity": "1.00", "order_id": "O1", "minute": 2},
    )
    order = aggregate_orders(group)["O1"]
    assert order.first_execution_time < order.last_execution_time


def test_underlying_fill_ids_are_preserved_in_order():
    group = fills(
        {"index": 2, "quantity": "1.00", "order_id": "O1", "minute": 5},
        {"index": 1, "quantity": "1.00", "order_id": "O1", "minute": 2},
    )
    assert aggregate_orders(group)["O1"].fill_ids == ("SYNTHFILL-0001", "SYNTHFILL-0002")


def test_multiple_orders_are_grouped_separately():
    group = fills(
        {"index": 1, "quantity": "10.00", "order_id": "O1"},
        {"index": 2, "quantity": "20.00", "order_id": "O2"},
        {"index": 3, "quantity": "30.00", "order_id": "O2"},
    )
    stats = OrderAggregationStats()
    orders = aggregate_orders(group, stats)
    assert set(orders) == {"O1", "O2"}
    assert orders["O2"].total_quantity == Decimal("50.00")
    assert stats.orders == 2 and stats.partial_orders == 1


def test_fills_without_an_order_id_are_excluded_and_counted():
    group = fills(
        {"index": 1, "quantity": "10.00", "order_id": ""},
        {"index": 2, "quantity": "20.00", "order_id": "O2"},
    )
    stats = OrderAggregationStats()
    orders = aggregate_orders(group, stats)
    assert set(orders) == {"O2"}
    assert stats.fills_without_order_id == 1


# ------------------------------------------------------------------ fees

def test_order_fee_total_is_the_sum_when_every_fill_reports_one():
    group = fills(
        {"index": 1, "quantity": "10.00", "order_id": "O1", "fee": "0.0175"},
        {"index": 2, "quantity": "10.00", "order_id": "O1", "fee": "0.0100"},
    )
    order = aggregate_orders(group)["O1"]
    assert order.total_fee == Decimal("0.0275")
    assert order.fee_complete is True
    assert order.fills_with_fee == 2


def test_partial_fee_data_yields_no_total_rather_than_a_partial_one():
    group = fills(
        {"index": 1, "quantity": "10.00", "order_id": "O1", "fee": "0.0175"},
        {"index": 2, "quantity": "10.00", "order_id": "O1"},
    )
    stats = OrderAggregationStats()
    order = aggregate_orders(group, stats)["O1"]
    assert order.total_fee is None
    assert order.fee_complete is False
    assert order.fills_with_fee == 1
    assert stats.orders_missing_fees == 1


def test_absent_fees_are_never_reconstructed_from_the_fee_schedule():
    group = fills({"index": 1, "quantity": "10.00", "order_id": "O1"})
    assert aggregate_orders(group)["O1"].total_fee is None


# ------------------------------------------------------------ fail closed

def test_order_spanning_two_markets_fails_closed():
    group = fills(
        {"index": 1, "quantity": "1.00", "order_id": "O1", "ticker": "KXSYNTH-A-1"},
        {"index": 2, "quantity": "1.00", "order_id": "O1", "ticker": "KXSYNTH-B-1"},
    )
    with pytest.raises(SchemaError, match="market ticker"):
        aggregate_orders(group)


def test_order_spanning_two_actions_fails_closed():
    group = fills(
        {"index": 1, "quantity": "1.00", "order_id": "O1", "action": "buy"},
        {"index": 2, "quantity": "1.00", "order_id": "O1", "action": "sell"},
    )
    with pytest.raises(SchemaError, match="action"):
        aggregate_orders(group)


def test_order_spanning_two_sides_fails_closed():
    group = fills(
        {"index": 1, "quantity": "1.00", "order_id": "O1", "side": "yes"},
        {"index": 2, "quantity": "1.00", "order_id": "O1", "side": "no"},
    )
    with pytest.raises(SchemaError, match="side"):
        aggregate_orders(group)


def test_order_without_prices_reports_no_vwap_rather_than_a_partial_one():
    raw = make_accounting_fill(index=1, quantity="10.00", order_id="O1")
    del raw["yes_price_dollars"]
    stats = OrderAggregationStats()
    order = aggregate_orders([normalize_fill(raw)], stats)["O1"]
    assert order.vwap_price is None
    assert stats.orders_without_price == 1


# --------------------------------------------------------------- invariant

def test_invariant_sum_of_fill_quantities_equals_order_quantity():
    group = fills(
        {"index": 1, "quantity": "13.00", "order_id": "O1"},
        {"index": 2, "quantity": "7.50", "order_id": "O1"},
        {"index": 3, "quantity": "0.25", "order_id": "O1"},
    )
    order = aggregate_orders(group)["O1"]
    assert order.total_quantity == sum((f.count for f in group), Decimal(0))
    assert order.total_quantity == Decimal("20.75")
