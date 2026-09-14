"""Current Kalshi fill schema: fee_cost, canonical direction, subaccounts."""

from __future__ import annotations

from decimal import Decimal

import pytest

from kalshi_router.accounting import AccountingEngine, HistoryCompleteness
from kalshi_router.accounting.position import project_fill
from kalshi_router.errors import SchemaError
from kalshi_router.models import Action, BookSide, OutcomeSide, Side, normalize_fill

COMPLETE = HistoryCompleteness.COMPLETE


def official_fill(**overrides):
    """Shaped exactly like the published Get Fills example response."""
    raw = {
        "fill_id": "SYNTHFILL-0001",
        "exchange_index": 1,
        "trade_id": "SYNTHTRADE-0001",
        "order_id": "SYNTHORDER-0001",
        "ticker": "KXSYNTH-ACCT01-AAA",
        "market_ticker": "KXSYNTH-ACCT01-AAA",
        "outcome_side": "yes",
        "book_side": "bid",
        "count_fp": "100.00",
        "yes_price_dollars": "0.5600",
        "no_price_dollars": "0.4400",
        "is_taker": True,
        "fee_cost": "0.5600",
        "side": "yes",
        "action": "buy",
        "created_time": "2026-09-01T12:00:00Z",
        "subaccount_number": 3,
        "ts": 1788000000,
    }
    raw.update(overrides)
    return raw


# ============================== fee_cost (Blocker 1) =========================

def test_official_example_fill_parses_completely():
    fill = normalize_fill(official_fill())
    assert fill.count == Decimal("100.00")
    assert fill.price_dollars == Decimal("0.5600")
    assert fill.fee_dollars == Decimal("0.5600")
    assert fill.fee_source == "fee_cost"
    assert fill.outcome_side is OutcomeSide.YES
    assert fill.book_side is BookSide.BID
    assert fill.subaccount_number == 3


def test_fee_cost_string_is_read_as_dollars_losslessly():
    fill = normalize_fill(official_fill(fee_cost="0.5600"))
    assert fill.fee_dollars == Decimal("0.5600")
    assert str(fill.fee_dollars) == "0.5600"


def test_fee_cost_keeps_sub_cent_precision():
    """Kalshi's fee rounding math runs to six decimal places."""
    fill = normalize_fill(official_fill(fee_cost="0.010000"))
    assert fill.fee_dollars == Decimal("0.010000")
    assert str(fill.fee_dollars) == "0.010000"


def test_zero_fee_is_valid():
    assert normalize_fill(official_fill(fee_cost="0.0000")).fee_dollars == Decimal("0.0000")


def test_negative_fee_fails_closed():
    with pytest.raises(SchemaError, match="negative fee"):
        normalize_fill(official_fill(fee_cost="-0.0100"))


def test_integer_fee_cost_is_the_legacy_cents_schema():
    """Unit is disambiguated by JSON type, with documented provenance."""
    fill = normalize_fill(official_fill(fee_cost=56))
    assert fill.fee_dollars == Decimal("0.56")
    assert fill.fee_source == "fee_cost_legacy_cents"


def test_a_string_fee_is_never_read_as_cents():
    """"0.5600" is 56 cents, not 0.56 cents."""
    assert normalize_fill(official_fill(fee_cost="0.5600")).fee_dollars == Decimal("0.56")


def test_absent_fee_is_none_and_never_reconstructed():
    raw = official_fill()
    del raw["fee_cost"]
    fill = normalize_fill(raw)
    assert fill.fee_dollars is None and fill.has_fee is False


def test_malformed_fee_string_fails_closed():
    with pytest.raises(SchemaError):
        normalize_fill(official_fill(fee_cost="1e-2"))


# ========================= canonical direction (Blocker 2) ===================

@pytest.mark.parametrize(
    "action,side,expected",
    [
        ("buy", "yes", OutcomeSide.YES),
        ("sell", "no", OutcomeSide.YES),
        ("buy", "no", OutcomeSide.NO),
        ("sell", "yes", OutcomeSide.NO),
    ],
)
def test_documented_legacy_equivalences(action, side, expected):
    raw = official_fill(action=action, side=side)
    raw["outcome_side"] = expected.value
    raw["book_side"] = "bid" if expected is OutcomeSide.YES else "ask"
    assert normalize_fill(raw).outcome_side is expected


def test_canonical_only_fill_needs_no_action_or_side():
    raw = official_fill()
    del raw["action"], raw["side"]
    fill = normalize_fill(raw)
    assert fill.outcome_side is OutcomeSide.YES
    assert fill.legacy_action is None and fill.legacy_side is None
    assert fill.direction_sources == ("outcome_side", "book_side")


def test_outcome_side_alone_is_sufficient():
    raw = official_fill()
    for key in ("book_side", "action", "side"):
        del raw[key]
    assert normalize_fill(raw).outcome_side is OutcomeSide.YES


def test_book_side_alone_is_sufficient():
    raw = official_fill(book_side="ask")
    for key in ("outcome_side", "action", "side"):
        del raw[key]
    fill = normalize_fill(raw)
    assert fill.outcome_side is OutcomeSide.NO
    assert fill.direction_sources == ("book_side",)


def test_legacy_only_payload_still_parses_for_historical_replay():
    raw = official_fill(action="sell", side="yes")
    del raw["outcome_side"], raw["book_side"]
    fill = normalize_fill(raw)
    assert fill.outcome_side is OutcomeSide.NO
    assert fill.direction_sources == ("legacy_action_side",)


def test_outcome_side_and_book_side_disagreement_fails_closed():
    with pytest.raises(SchemaError, match="disagree"):
        normalize_fill(official_fill(outcome_side="yes", book_side="ask"))


def test_canonical_and_legacy_disagreement_fails_closed():
    # outcome_side says YES, but buy/no says NO.
    with pytest.raises(SchemaError, match="disagree"):
        normalize_fill(official_fill(outcome_side="yes", book_side="bid",
                                     action="buy", side="no"))


def test_projection_does_not_require_action_to_exist():
    raw = official_fill(outcome_side="no", book_side="ask", no_price_dollars="0.4400")
    del raw["action"], raw["side"]
    signed, price = project_fill(normalize_fill(raw))
    assert signed == Decimal("-100.00")
    assert price == Decimal("0.5600")  # YES-equivalent of a $0.44 NO


def test_legacy_metadata_is_preserved_for_later_analysis():
    fill = normalize_fill(official_fill(action="buy", side="yes"))
    assert fill.legacy_action is Action.BUY and fill.legacy_side is Side.YES
    assert "legacy_action_side" in fill.direction_sources


# ============================= subaccounts (Blocker 3) =======================

def acct_fill(index, subaccount, quantity="10.00", outcome="yes", ticker="KXSYNTH-A-1"):
    return normalize_fill(official_fill(
        fill_id=f"SYNTHFILL-{index:04d}",
        order_id=f"SYNTHORDER-{index:04d}",
        ticker=ticker,
        market_ticker=ticker,
        subaccount_number=subaccount,
        count_fp=quantity,
        outcome_side=outcome,
        book_side="bid" if outcome == "yes" else "ask",
        action="buy",
        side=outcome,
        created_time=f"2026-09-01T12:{index:02d}:00Z",
    ))


def test_same_ticker_in_two_subaccounts_never_nets_together():
    result = AccountingEngine().replay(
        [acct_fill(1, 0, "100.00", "yes"), acct_fill(2, 1, "100.00", "no")], COMPLETE
    )
    assert result.ledger_for("KXSYNTH-A-1", 0).position == Decimal("100.00")
    assert result.ledger_for("KXSYNTH-A-1", 1).position == Decimal("-100.00")
    assert len(result.ledgers) == 2


def test_two_subaccounts_produce_separate_episodes_with_distinct_identities():
    result = AccountingEngine().replay(
        [acct_fill(1, 0), acct_fill(2, 7)], COMPLETE
    )
    episodes = result.episodes
    assert len(episodes) == 2
    assert {e.subaccount_number for e in episodes} == {0, 7}
    assert episodes[0].source_key != episodes[1].source_key
    assert "kalshi:episode:0:" in episodes[0].source_key
    assert "kalshi:episode:7:" in episodes[1].source_key


def test_a_close_in_one_subaccount_does_not_close_the_other():
    result = AccountingEngine().replay([
        acct_fill(1, 0, "50.00", "yes"),
        acct_fill(2, 1, "50.00", "yes"),
        acct_fill(3, 0, "50.00", "no"),  # closes subaccount 0 only
    ], COMPLETE)
    assert result.ledger_for("KXSYNTH-A-1", 0).position == Decimal("0.00")
    assert result.ledger_for("KXSYNTH-A-1", 1).position == Decimal("50.00")


def test_absent_subaccount_is_kept_distinct_rather_than_assumed_primary():
    """Absent is not silently mapped onto 0; that would merge real subaccounts."""
    raw = official_fill(fill_id="SYNTHFILL-0009", order_id="SYNTHORDER-0009",
                        ticker="KXSYNTH-A-1", market_ticker="KXSYNTH-A-1")
    del raw["subaccount_number"]
    unnumbered = normalize_fill(raw)
    assert unnumbered.subaccount_number is None
    result = AccountingEngine().replay([unnumbered, acct_fill(1, 0)], COMPLETE)
    assert len(result.ledgers) == 2
    assert result.ledger_for("KXSYNTH-A-1", None) is not None
    assert result.ledger_for("KXSYNTH-A-1", 0) is not None


def test_negative_subaccount_number_fails_closed():
    with pytest.raises(SchemaError, match="subaccount_number"):
        normalize_fill(official_fill(subaccount_number=-1))


def test_subaccount_counts_reach_diagnostics_as_counts_only():
    from kalshi_router.accounting.diagnostics import build_diagnostics

    result = AccountingEngine().replay([acct_fill(1, 0), acct_fill(2, 4)], COMPLETE)
    diagnostics = build_diagnostics(result)
    assert diagnostics.subaccounts_observed == 2
    rendered = diagnostics.render()
    assert "distinct subaccounts observed: 2" in rendered
    assert "KXSYNTH" not in rendered
