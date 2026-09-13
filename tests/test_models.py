"""Normalization primitives: buy/sell, YES/NO, dedupe, order grouping."""

from __future__ import annotations

from decimal import Decimal

import pytest

from kalshi_router.errors import SchemaError
from kalshi_router.models import (
    Action,
    Side,
    count_partial_order_groups,
    dedupe_fills,
    group_by_order,
    normalize_fill,
)

from .synthetic import make_fill


def test_buy_fill_normalizes():
    fill = normalize_fill(make_fill(1, action="buy", side="yes"))
    assert fill.action is Action.BUY and fill.side is Side.YES
    assert fill.count == Decimal("10.00") and fill.count_source == "count_fp"
    assert fill.price_dollars == Decimal("0.5700") and fill.price_source == "price_dollars"


def test_sell_fill_normalizes():
    fill = normalize_fill(make_fill(2, action="sell", side="yes"))
    assert fill.action is Action.SELL


def test_no_side_fill_uses_the_no_price_leg():
    fill = normalize_fill(make_fill(3, side="no"))
    assert fill.side is Side.NO and fill.price_dollars == Decimal("0.4300")


def test_action_and_side_tokens_are_case_insensitive():
    fill = normalize_fill(make_fill(4, action="SELL", side="NO"))
    assert fill.action is Action.SELL and fill.side is Side.NO


def test_subpenny_price_survives_exactly():
    """A $0.001 tick cannot be represented in integer cents."""
    raw = make_fill(5)
    raw["yes_price_dollars"] = "0.6125"
    fill = normalize_fill(raw)
    assert fill.price_dollars == Decimal("0.6125")
    assert str(fill.price_dollars) == "0.6125"


def test_legacy_integer_cent_price_is_converted_exactly():
    raw = make_fill(5)
    del raw["yes_price_dollars"]
    raw["yes_price"] = 61
    fill = normalize_fill(raw)
    assert fill.price_dollars == Decimal("0.61") and fill.price_source == "price_cents"


def test_market_ticker_alias_is_accepted():
    raw = make_fill(6)
    raw["market_ticker"] = raw.pop("ticker")
    assert normalize_fill(raw).ticker.startswith("KXMLBGAME")


def test_outcome_side_alias_is_accepted():
    raw = make_fill(7)
    raw["outcome_side"] = raw.pop("side")
    assert normalize_fill(raw).side is Side.YES


@pytest.mark.parametrize("field", ["fill_id", "ticker", "action", "side"])
def test_missing_required_field_fails_closed(field):
    raw = make_fill(8)
    del raw[field]
    with pytest.raises(SchemaError):
        normalize_fill(raw)


@pytest.mark.parametrize("bad", [{"action": "hedge"}, {"side": "maybe"}])
def test_unrecognized_tokens_fail_closed_rather_than_defaulting(bad):
    raw = make_fill(9)
    raw.update(bad)
    with pytest.raises(SchemaError):
        normalize_fill(raw)


def test_missing_count_and_count_fp_fails_closed():
    raw = make_fill(10, count=None)
    with pytest.raises(SchemaError, match="count"):
        normalize_fill(raw)


def test_count_fp_ten_dot_zero_zero_is_ten_contracts():
    """Documented semantics: count_fp "10.00" is ten contracts, not 1000."""
    raw = make_fill(11, count=None)
    raw["count_fp"] = "10.00"
    fill = normalize_fill(raw)
    assert fill.count == Decimal("10.00")
    assert fill.count == 10
    assert fill.count_source == "count_fp"


def test_fractional_contract_counts_are_exact():
    raw = make_fill(11, count=None)
    raw["count_fp"] = "0.25"
    assert normalize_fill(raw).count == Decimal("0.25")


def test_legacy_integer_count_is_accepted_but_not_preferred():
    raw = make_fill(11, count=None)
    raw["count"] = 7
    raw["count_fp"] = "9.00"
    fill = normalize_fill(raw)
    assert fill.count == Decimal("9.00") and fill.count_source == "count_fp"


def test_price_is_optional_and_never_required():
    raw = make_fill(12)
    del raw["yes_price_dollars"]
    fill = normalize_fill(raw)
    assert fill.price_dollars is None and fill.price_source is None


# --------------------------------------------------------------- dedupe/group

def test_duplicate_fill_ids_are_recognized_and_counted():
    fills = [normalize_fill(make_fill(i)) for i in (1, 2, 1, 3, 2)]
    result = dedupe_fills(fills)
    assert result.unique_count == 3
    assert result.duplicate_count == 2
    assert [f.fill_id for f in result.fills] == [
        "SYNTHFILL-0001", "SYNTHFILL-0002", "SYNTHFILL-0003"
    ]


def test_dedupe_of_an_empty_stream_is_empty():
    result = dedupe_fills([])
    assert result.unique_count == 0 and result.duplicate_count == 0


def test_multiple_fills_share_one_order_id():
    fills = [
        normalize_fill(make_fill(1, order_id="SYNTHORDER-AAA", fill_id="SYNTHFILL-A")),
        normalize_fill(make_fill(2, order_id="SYNTHORDER-AAA", fill_id="SYNTHFILL-B")),
        normalize_fill(make_fill(3, order_id="SYNTHORDER-BBB", fill_id="SYNTHFILL-C")),
    ]
    groups = group_by_order(fills)
    assert len(groups) == 2
    assert len(groups["SYNTHORDER-AAA"]) == 2
    assert count_partial_order_groups(fills) == 1


def test_fills_without_an_order_id_are_not_grouped():
    fills = [normalize_fill(make_fill(1, order_id=""))]
    assert group_by_order(fills) == {}
