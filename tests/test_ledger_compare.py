"""Phase 6: the historical shadow comparison.

These tests exist because a comparison that is wrong in the FLATTERING
direction is worse than no comparison at all -- it would report agreement the
system has not earned, and that number is what decides whether the router's
view of history can be trusted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

import pytest

from kalshi_router.ledger_compare import (
    LedgerComparison,
    compare_to_ledger,
    read_ledger,
)


@dataclass
class FakeWager:
    """The attribute surface compare_to_ledger reads, and nothing else."""

    market_ticker: str
    side: str = "YES"
    stake: Decimal = Decimal("5.00")
    entry_price: Decimal = Decimal("0.50")
    result: str = "WIN"


def ledger_row(ticker, **overrides):
    row = {
        "marketTicker": ticker,
        "side": "YES",
        "stake": 5.0,
        "entryPrice": 0.50,
        "result": "WIN",
        "sport": "MLB",
        "platform": "KALSHI",
        "recordStatus": "ACTIVE",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# The buckets must partition the keys. Nothing counted twice, nothing dropped.
# ---------------------------------------------------------------------------


def test_key_buckets_partition_every_distinct_key():
    wagers = [FakeWager("A"), FakeWager("B"), FakeWager("C"), FakeWager("C", side="NO")]
    rows = [ledger_row("B"), ledger_row("D"), ledger_row("C", side="NO")]

    result = compare_to_ledger(wagers, rows)

    distinct = {("A", "YES"), ("B", "YES"), ("C", "YES"), ("C", "NO"), ("D", "YES")}
    assert result.keys_accounted == len(distinct)
    assert result.matched_keys == 2          # B/YES and C/NO
    assert result.router_only_keys == 2      # A/YES and C/YES
    assert result.ledger_only_keys == 1      # D/YES


def test_a_wager_without_a_ticker_is_not_counted_as_a_key():
    """It forms no key, so it must not land in a key bucket."""
    result = compare_to_ledger([FakeWager("")], [])

    assert result.router_wagers_built == 1
    assert result.router_wagers_without_ticker == 1
    assert result.keys_accounted == 0


# ---------------------------------------------------------------------------
# Multiplicity must not be flattened into a match.
# ---------------------------------------------------------------------------


def test_two_ledger_rows_for_one_key_are_ambiguous_not_matched():
    result = compare_to_ledger([FakeWager("A")], [ledger_row("A"), ledger_row("A")])

    assert result.ambiguous_multiplicity_keys == 1
    assert result.matched_keys == 0
    # And nothing was scored: an unscored key must not contribute agreement.
    assert result.stake_agrees == 0
    assert result.stake_disagrees == 0


def test_two_ledger_rows_are_not_summed_to_manufacture_agreement():
    """Half-and-half rows summing to the router's stake must NOT read as agreeing."""
    rows = [ledger_row("A", stake=2.5), ledger_row("A", stake=2.5)]

    result = compare_to_ledger([FakeWager("A", stake=Decimal("5.00"))], rows)

    assert result.stake_agrees == 0
    assert result.ambiguous_multiplicity_keys == 1


# ---------------------------------------------------------------------------
# Disagreement must surface as disagreement, never as a non-match.
# ---------------------------------------------------------------------------


def test_a_stake_disagreement_is_a_matched_key_that_disagrees():
    """Stake is not part of the key. If it were, this would hide in router_only."""
    result = compare_to_ledger(
        [FakeWager("A", stake=Decimal("5.00"))], [ledger_row("A", stake=9.0)]
    )

    assert result.matched_keys == 1
    assert result.router_only_keys == 0
    assert result.stake_disagrees == 1


def test_price_tolerance_admits_a_tick_rounding_but_not_a_cent_error():
    near = compare_to_ledger(
        [FakeWager("A", entry_price=Decimal("0.505"))], [ledger_row("A", entryPrice=0.50)]
    )
    far = compare_to_ledger(
        [FakeWager("A", entry_price=Decimal("0.52"))], [ledger_row("A", entryPrice=0.50)]
    )

    assert near.price_agrees == 1 and near.price_disagrees == 0
    assert far.price_disagrees == 1 and far.price_agrees == 0


def test_a_null_ledger_stake_is_not_comparable_rather_than_disagreeing():
    """Saying nothing is not the same as saying something different."""
    result = compare_to_ledger([FakeWager("A")], [ledger_row("A", stake=None)])

    assert result.stake_not_comparable == 1
    assert result.stake_disagrees == 0
    assert result.stake_agrees == 0


def test_a_boolean_is_not_read_as_a_dollar_amount():
    """isinstance(True, int) is true in Python; Decimal(True) is 1."""
    result = compare_to_ledger(
        [FakeWager("A", stake=Decimal("1.00"))], [ledger_row("A", stake=True)]
    )

    assert result.stake_agrees == 0
    assert result.stake_not_comparable == 1


def test_a_result_disagreement_is_reported():
    result = compare_to_ledger([FakeWager("A", result="WIN")], [ledger_row("A", result="LOSS")])

    assert result.result_disagrees == 1
    assert result.result_agrees == 0


def test_side_is_part_of_the_key_so_an_opposite_leg_is_not_a_match():
    """YES and NO on one market are different wagers, not one wager recorded twice."""
    result = compare_to_ledger([FakeWager("A", side="YES")], [ledger_row("A", side="NO")])

    assert result.matched_keys == 0
    assert result.router_only_keys == 1
    assert result.ledger_only_keys == 1


# ---------------------------------------------------------------------------
# Ledger filtering.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides,counter",
    [
        ({"recordStatus": "SUPERSEDED"}, "ledger_rows_not_active"),
        ({"platform": "DRAFTKINGS"}, "ledger_rows_other_platform"),
        ({"sport": "NFL"}, "ledger_rows_other_sport"),
        ({"marketTicker": None}, "ledger_rows_without_ticker"),
    ],
)
def test_excluded_ledger_rows_are_counted_under_the_reason(overrides, counter):
    result = compare_to_ledger([], [ledger_row("A", **overrides)])

    assert getattr(result, counter) == 1
    assert result.ledger_rows_compared == 0
    assert result.ledger_rows_read == 1


def test_every_ledger_row_read_is_accounted_for():
    rows = [
        ledger_row("A"),
        ledger_row("B", recordStatus="SUPERSEDED"),
        ledger_row("C", platform="DRAFTKINGS"),
        ledger_row("D", sport="NFL"),
        ledger_row("E", marketTicker=None),
    ]

    result = compare_to_ledger([], rows)

    assert result.ledger_rows_read == 5
    assert (
        result.ledger_rows_not_active
        + result.ledger_rows_other_platform
        + result.ledger_rows_other_sport
        + result.ledger_rows_without_ticker
        + result.ledger_rows_compared
    ) == result.ledger_rows_read


# ---------------------------------------------------------------------------
# Reading the file.
# ---------------------------------------------------------------------------


def test_a_malformed_ledger_line_raises_rather_than_being_skipped(tmp_path):
    """Skipping would understate the ledger and flatter the agreement rate."""
    path = tmp_path / "bets.jsonl"
    path.write_text(json.dumps(ledger_row("A")) + "\n{not json\n", encoding="utf-8")

    with pytest.raises(ValueError) as caught:
        read_ledger(str(path))

    assert "line 2" in str(caught.value)


def test_blank_lines_are_tolerated(tmp_path):
    path = tmp_path / "bets.jsonl"
    path.write_text(json.dumps(ledger_row("A")) + "\n\n", encoding="utf-8")

    assert len(read_ledger(str(path))) == 1


# ---------------------------------------------------------------------------
# PRIVACY: the comparison is the only thing that leaves the process.
# ---------------------------------------------------------------------------


def test_the_comparison_is_structurally_incapable_of_holding_a_ticker():
    """Every field is an int. Not a convention -- a checked property."""
    wagers = [FakeWager("KXMLBGAME-26AUG03SFLAD-SF")]
    rows = [ledger_row("KXMLBGAME-26AUG03SFLAD-LAD")]

    result = compare_to_ledger(wagers, rows)

    for name, value in result.as_dict().items():
        assert isinstance(value, int), f"{name} is {type(value).__name__}, not int"
    assert "KXMLB" not in result.render()
    assert "KXMLB" not in json.dumps(result.as_dict())
