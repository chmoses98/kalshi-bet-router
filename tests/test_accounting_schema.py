"""Current Kalshi fill schema: fee_cost, canonical direction, subaccounts."""

from __future__ import annotations

from decimal import Decimal

import pytest

from kalshi_router.accounting.position import PositionAuthority
from kalshi_router.accounting import AccountingEngine, HistoryCompleteness
from kalshi_router.accounting.position import project_fill
from kalshi_router.errors import SchemaError
from kalshi_router.models import Action, BookSide, OutcomeSide, Side, normalize_fill

from .synthetic import complement

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
        # The two price fields are complementary legs of one trade and sum
        # to 1.00, as every observed live fill does.
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


# ========================= canonical direction ===============================
#
# CORRECTED against live data.  outcome_side reports WHICH CONTRACT was traded,
# not which way the position moved:
#
#     no|ask|buy|no:   31        no|ask|sell|no:  2        yes|bid|buy|yes: 167
#
# outcome_side equalled `side` on all 200 fills, sells included, and book_side
# tracked the contract too -- the 31 buy-NO and the 2 sell-NO fills all reported
# `ask`.  So the canonical pair cannot distinguish a buy from a sell, and only
# the deprecated `action` carries the verb.

@pytest.mark.parametrize(
    "action,side,exposure",
    [
        ("buy", "yes", OutcomeSide.YES),
        ("sell", "no", OutcomeSide.YES),
        ("buy", "no", OutcomeSide.NO),
        ("sell", "yes", OutcomeSide.NO),
    ],
)
def test_exposure_comes_from_the_verb_and_the_contract(action, side, exposure):
    raw = official_fill(action=action, side=side)
    raw["outcome_side"] = side                       # the contract, not the exposure
    raw["book_side"] = "bid" if side == "yes" else "ask"
    fill = normalize_fill(raw)
    assert fill.outcome_side is OutcomeSide(side)
    assert fill.exposure_side is exposure


def test_a_canonical_only_fill_is_refused_rather_than_assumed_to_be_a_buy():
    raw = official_fill()
    del raw["action"], raw["side"]
    with pytest.raises(SchemaError, match="buy/sell verb"):
        normalize_fill(raw)


def test_outcome_side_alone_identifies_the_contract_but_not_the_direction():
    raw = official_fill()
    del raw["book_side"]
    fill = normalize_fill(raw)
    assert fill.outcome_side is OutcomeSide.YES
    assert "outcome_side" in fill.direction_sources


def test_book_side_corroborates_the_contract():
    raw = official_fill(book_side="ask", outcome_side="no", side="no", action="buy")
    del raw["outcome_side"]
    fill = normalize_fill(raw)
    assert fill.outcome_side is OutcomeSide.NO
    assert "book_side" in fill.direction_sources


def test_legacy_only_payload_still_parses_for_historical_replay():
    raw = official_fill(action="sell", side="yes")
    del raw["outcome_side"], raw["book_side"]
    fill = normalize_fill(raw)
    assert fill.outcome_side is OutcomeSide.YES       # contract
    assert fill.exposure_side is OutcomeSide.NO       # sell-YES moves toward NO
    assert fill.direction_sources == ("legacy_side",)


def test_outcome_side_and_book_side_disagreement_fails_closed():
    with pytest.raises(SchemaError, match="disagree"):
        normalize_fill(official_fill(outcome_side="yes", book_side="ask"))


def test_canonical_and_legacy_disagreement_fails_closed():
    # outcome_side says YES, but buy/no says NO.
    with pytest.raises(SchemaError, match="disagree"):
        normalize_fill(official_fill(outcome_side="yes", book_side="bid",
                                     action="buy", side="no"))


def test_projection_uses_the_exposure_not_the_contract():
    raw = official_fill(outcome_side="no", book_side="ask", action="buy", side="no")
    signed, price = project_fill(normalize_fill(raw))
    assert signed == Decimal("-100.00")
    assert price == Decimal("0.5600")  # YES axis


def test_legacy_metadata_is_preserved_for_later_analysis():
    fill = normalize_fill(official_fill(action="buy", side="yes"))
    assert fill.legacy_action is Action.BUY and fill.legacy_side is Side.YES
    assert "legacy_side" in fill.direction_sources


# ============================= subaccounts (Blocker 3) =======================

def acct_fill(index, subaccount, quantity="10.00", outcome="yes", ticker="KXSYNTH-A-1",
              price="0.5600"):
    return normalize_fill(official_fill(
        fill_id=f"SYNTHFILL-{index:04d}",
        order_id=f"SYNTHORDER-{index:04d}",
        ticker=ticker,
        market_ticker=ticker,
        subaccount_number=subaccount,
        count_fp=quantity,
        yes_price_dollars=price,
        no_price_dollars=complement(price),
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
    # Distinct tickers, so each position reconciles against the exchange on its
    # own and both earn an identity. The point under test is that the SUBACCOUNT
    # is part of the key.
    fills = [acct_fill(1, 0, ticker="KXSYNTH-A-1"), acct_fill(2, 7, ticker="KXSYNTH-A-2")]
    result = AccountingEngine().replay(
        fills,
        COMPLETE,
        exchange_positions={"KXSYNTH-A-1": Decimal("10.00"),
                            "KXSYNTH-A-2": Decimal("10.00")},
    )
    episodes = result.episodes
    assert len(episodes) == 2
    assert {e.subaccount_number for e in episodes} == {0, 7}
    assert episodes[0].source_key != episodes[1].source_key
    assert "kalshi:episode:0:" in episodes[0].source_key
    assert "kalshi:episode:7:" in episodes[1].source_key


def test_one_ticker_in_two_subaccounts_cannot_be_reconciled_and_fails_closed():
    """The positions response carries no subaccount, so it cannot be split.

    Attributing one reported quantity to one of two independent positions would
    merge them, which is the failure the subaccount key exists to prevent. So
    neither earns authority and neither exposes an identity.
    """
    fills = [acct_fill(1, 0), acct_fill(2, 7)]          # same ticker
    result = AccountingEngine().replay(
        fills, COMPLETE, exchange_positions={"KXSYNTH-A-1": Decimal("20.00")}
    )
    assert all(e.authority is PositionAuthority.CONFLICTED for e in result.episodes)
    assert all(e.source_key is None for e in result.episodes)
    assert result.claims_complete_position_state is False


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


# ================ canonical price semantics ==================================
#
# CORRECTED against live data (200 fills, read-only audit run 4).
#
# The earlier reading here -- that outcome_side changes direction but not the
# price, so both price fields carry one identical "unified" price -- is
# REFUTED.  The live account returned:
#
#     complementary (sum to exactly 1.00): 200
#     equal away from even odds (unified model only): 0
#
# yes_price_dollars and no_price_dollars are the two LEGS of one trade.  They
# coincide only at even odds, which is why the old model accepted exactly the
# 6 even-odds fills and rejected the other 194.
#
# Positions live on one signed YES axis, so the accounting price is the YES leg
# whichever contract was traded.  That is selection, not complementing: the
# sign of the quantity carries direction, and the price is not transformed
# again.

def acct(index, action, side, yes_price, quantity="100.00"):
    """One fill, specified the way the exchange actually reports it."""
    return normalize_fill(official_fill(
        fill_id=f"SYNTHFILL-{index:04d}",
        order_id=f"SYNTHORDER-{index:04d}",
        outcome_side=side,
        book_side="bid" if side == "yes" else "ask",
        action=action,
        side=side,
        yes_price_dollars=yes_price,
        no_price_dollars=complement(yes_price),
        count_fp=quantity,
        created_time=f"2026-09-01T12:{index:02d}:00Z",
    ))


def test_1_buying_yes_projects_positive_at_the_yes_leg():
    signed, price = project_fill(acct(1, "buy", "yes", "0.5600"))
    assert signed == Decimal("100.00")
    assert price == Decimal("0.5600")


def test_2_buying_no_projects_negative_at_the_same_yes_axis_price():
    signed, price = project_fill(acct(1, "buy", "no", "0.5600"))
    assert signed == Decimal("-100.00")
    # Same trade, opposite direction: one axis coordinate, two exposures.
    assert price == Decimal("0.5600")


def test_3_the_traded_leg_price_is_kept_alongside_the_axis_price():
    """Cash per contract is the leg actually traded, not the axis coordinate."""
    buy_no = acct(1, "buy", "no", "0.5600")
    assert buy_no.price_dollars == Decimal("0.5600")      # YES axis
    assert buy_no.leg_price_dollars == Decimal("0.4400")  # what was paid
    buy_yes = acct(2, "buy", "yes", "0.5600")
    assert buy_yes.price_dollars == buy_yes.leg_price_dollars == Decimal("0.5600")


def test_3b_selling_no_moves_toward_yes_even_though_the_contract_is_no():
    """The defect this replaces: outcome_side=no was read as a NO exposure.

    Live data carries 2 such fills; they were rejected as self-contradictory.
    """
    fill = acct(1, "sell", "no", "0.5600")
    assert fill.outcome_side is OutcomeSide.NO      # the contract
    assert fill.exposure_side is OutcomeSide.YES    # the direction
    signed, _ = project_fill(fill)
    assert signed == Decimal("100.00")


def test_3c_selling_yes_moves_toward_no():
    fill = acct(1, "sell", "yes", "0.5600")
    assert fill.outcome_side is OutcomeSide.YES
    assert fill.exposure_side is OutcomeSide.NO
    assert project_fill(fill)[0] == Decimal("-100.00")


def test_4_long_no_reduction_pnl_has_the_correct_sign():
    """Open NO at a 0.57 axis price, reduce at 0.50."""
    result = AccountingEngine().replay(
        [acct(1, "buy", "no", "0.5700"), acct(2, "buy", "yes", "0.5000", "40.00")],
        COMPLETE,
    )
    episode = result.ledger_for("KXSYNTH-ACCT01-AAA", 3).episodes[0]
    assert episode.average_entry_price == Decimal("0.5700")
    # (0.50 - 0.57) * 40 * sign(-1) = +2.80
    assert episode.realized_pnl == Decimal("2.80")
    assert episode.realized_pnl > 0


def test_4b_the_same_reduction_expressed_as_a_sell_agrees():
    """Reducing a long NO by BUYING yes and by SELLING no are the same trade."""
    as_buy = AccountingEngine().replay(
        [acct(1, "buy", "no", "0.5700"), acct(2, "buy", "yes", "0.5000", "40.00")],
        COMPLETE,
    ).ledger_for("KXSYNTH-ACCT01-AAA", 3).episodes[0]
    as_sell = AccountingEngine().replay(
        [acct(1, "buy", "no", "0.5700"), acct(2, "sell", "no", "0.5000", "40.00")],
        COMPLETE,
    ).ledger_for("KXSYNTH-ACCT01-AAA", 3).episodes[0]
    assert as_buy.realized_pnl == as_sell.realized_pnl == Decimal("2.80")


def test_5_long_no_losing_trade_has_the_correct_sign():
    result = AccountingEngine().replay(
        [acct(1, "buy", "no", "0.5000"), acct(2, "buy", "yes", "0.6000")], COMPLETE
    )
    episode = result.ledger_for("KXSYNTH-ACCT01-AAA", 3).episodes[0]
    # (0.60 - 0.50) * 100 * sign(-1) = -10.00
    assert episode.realized_pnl == Decimal("-10.00")


def test_6_cross_zero_reversal_uses_the_yes_price_axis():
    result = AccountingEngine().replay(
        [acct(1, "buy", "yes", "0.6000", "100.00"),
         acct(2, "buy", "no", "0.7000", "150.00")],
        COMPLETE,
    )
    ledger = result.ledger_for("KXSYNTH-ACCT01-AAA", 3)
    assert ledger.position == Decimal("-50.00")
    outgoing, incoming = ledger.episodes
    # Closed the long YES at 0.70 against a 0.60 basis.
    assert outgoing.realized_pnl == Decimal("10.00")
    # The new long-NO leg opens at the crossing price on the same axis.
    assert incoming.average_entry_price == Decimal("0.7000")
    assert incoming.remaining_quantity == Decimal("50.00")


def test_7_a_fill_with_no_buy_sell_verb_fails_closed():
    """outcome_side and book_side identify the CONTRACT, never the direction.

    Live data: 31 buy-NO and 2 sell-NO fills all reported book_side=ask, so the
    canonical pair cannot distinguish them.  Assuming "buy" would invert a sale,
    so a fill without the verb is refused.
    """
    raw = official_fill()
    raw.pop("action")
    raw.pop("side")
    with pytest.raises(SchemaError, match="buy/sell verb"):
        normalize_fill(raw)


def test_8_non_complementary_price_fields_fail_closed():
    with pytest.raises(SchemaError, match="sum to one"):
        normalize_fill(official_fill(yes_price_dollars="0.5600",
                                     no_price_dollars="0.5600"))


def test_8b_complementary_price_fields_are_accepted():
    fill = normalize_fill(official_fill(yes_price_dollars="0.5600",
                                        no_price_dollars="0.4400"))
    assert fill.price_dollars == Decimal("0.5600")
    assert fill.legacy_price_semantics_unproven is False


def test_8c_even_odds_is_the_one_case_both_models_agreed_on():
    """The 6 fills the refuted model accepted: a complementary pair at 0.50."""
    fill = normalize_fill(official_fill(yes_price_dollars="0.5000",
                                        no_price_dollars="0.5000"))
    assert fill.price_dollars == Decimal("0.5000")


def test_9_legacy_only_payload_still_resolves_a_verifiable_price():
    """Complementarity is checkable from the pair itself.

    It does not depend on which direction fields the payload carried, so a
    legacy-only fill with both legs is proven rather than flagged.
    """
    raw = official_fill(action="buy", side="no", yes_price_dollars="0.5600",
                        no_price_dollars="0.4400")
    del raw["outcome_side"], raw["book_side"]
    fill = normalize_fill(raw)
    assert fill.outcome_side is OutcomeSide.NO       # contract
    assert fill.exposure_side is OutcomeSide.NO      # buy-no -> toward NO
    assert fill.price_dollars == Decimal("0.5600")
    assert fill.leg_price_dollars == Decimal("0.4400")
    assert fill.legacy_price_semantics_unproven is False


def test_9b_an_uncheckable_single_legacy_price_is_flagged_not_guessed():
    raw = official_fill(action="buy", side="yes", yes_price=56)
    del raw["outcome_side"], raw["book_side"]
    del raw["yes_price_dollars"], raw["no_price_dollars"]
    fill = normalize_fill(raw)
    assert fill.legacy_price_semantics_unproven is True
    assert fill.price_dollars is None                 # never guessed
    assert fill.price_source is None


def test_9c_economics_are_incomplete_rather_than_invented_without_a_price():
    raw = official_fill(action="buy", side="yes")
    del raw["yes_price_dollars"], raw["no_price_dollars"]
    result = AccountingEngine().replay([normalize_fill(raw)], COMPLETE)
    episode = result.ledger_for("KXSYNTH-ACCT01-AAA", 3).episodes[0]
    assert episode.cost_basis_complete is False
    assert episode.average_entry_price is None


# ================== subaccount validation (Blocker 2) ========================

def test_absent_subaccount_is_the_unknown_bucket():
    raw = official_fill()
    del raw["subaccount_number"]
    assert normalize_fill(raw).subaccount_number is None


def test_explicit_null_subaccount_is_also_absent():
    assert normalize_fill(official_fill(subaccount_number=None)).subaccount_number is None


@pytest.mark.parametrize("value", [0, 1, 32, 62, 63])
def test_valid_subaccount_range_is_accepted(value):
    assert normalize_fill(official_fill(subaccount_number=value)).subaccount_number == value


@pytest.mark.parametrize("value", [-1, -63, 64, 100, 9999])
def test_out_of_range_subaccount_fails_closed(value):
    with pytest.raises(SchemaError, match="subaccount_number"):
        normalize_fill(official_fill(subaccount_number=value))


@pytest.mark.parametrize(
    "value", ["3", "", "primary", 3.0, 0.5, True, False, [3], {"n": 3}, ()],
)
def test_malformed_subaccount_types_fail_closed(value):
    with pytest.raises(SchemaError, match="subaccount_number"):
        normalize_fill(official_fill(subaccount_number=value))


def test_malformed_is_never_silently_treated_as_absent():
    """Otherwise two corrupted values would net together in the None ledger."""
    with pytest.raises(SchemaError):
        normalize_fill(official_fill(subaccount_number="not-a-number"))
    with pytest.raises(SchemaError):
        normalize_fill(official_fill(subaccount_number=999))
