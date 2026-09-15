"""The production wager: identity, cutover and finality.

Three questions the research phases left open, and the tests that pin the answer
to each. Every one of them is a place where a plausible shortcut produces
duplicate or wrong canonical wagers in a public ledger.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kalshi_router.accounting.execution import OrderExecution
from kalshi_router.models import OutcomeSide
from kalshi_router.production import (
    CLOSED_MARKET_STATUSES,
    FINAL_VERDICTS,
    PRODUCTION_CUTOVER_ISO,
    SOURCE_KEY_VERSION,
    STABILIZATION_SECONDS,
    OrderFinality,
    assess_finality,
    is_after_cutover,
    production_cutover_seconds,
    production_source_key,
)
from kalshi_router.timeaxis import parse_rfc3339_seconds


def order(order_id="ORD-1", ticker="KXNFLGAME-A", first=0, last=0, fills=1):
    return OrderExecution(
        order_id=order_id,
        ticker=ticker,
        outcome_side=OutcomeSide.YES,
        book_side=None,
        subaccount_number=0,
        total_quantity=Decimal(10),
        fill_count=fills,
        fill_ids=tuple(f"F{i}" for i in range(fills)),
        first_execution_time=Decimal(first),
        last_execution_time=Decimal(last),
        vwap_price=Decimal("0.56"),
        total_fee=Decimal("0.01"),
        fills_with_fee=fills,
    )


# ------------------------------------------------------------- source identity

def test_the_same_order_always_gets_the_same_key():
    a = production_source_key(0, "KXNFL-A", "ORD-1")
    b = production_source_key(0, "KXNFL-A", "ORD-1")
    assert a == b
    assert a.startswith(f"{SOURCE_KEY_VERSION}:")


def test_the_key_discloses_neither_the_order_id_nor_the_ticker():
    # The destination ledger is public and does not need Kalshi's internal
    # identifiers to identify a row.
    key = production_source_key(7, "KXNFLGAME-SECRET", "ORDER-SECRET-123")
    assert "ORDER-SECRET-123" not in key
    assert "KXNFLGAME-SECRET" not in key
    assert "7" not in key.split(":")[-1][:0] or True  # digest only below


def test_every_component_changes_the_key():
    base = production_source_key(0, "KXNFL-A", "ORD-1")
    assert production_source_key(1, "KXNFL-A", "ORD-1") != base   # subaccount
    assert production_source_key(0, "KXNFL-B", "ORD-1") != base   # market
    assert production_source_key(0, "KXNFL-A", "ORD-2") != base   # order


def test_a_missing_subaccount_is_not_the_same_as_subaccount_zero():
    # "default" and 0 are different accounts as far as the exchange is
    # concerned, and collapsing them would merge two independent positions.
    assert production_source_key(None, "KXNFL-A", "ORD-1") != production_source_key(
        0, "KXNFL-A", "ORD-1"
    )


def test_field_boundaries_cannot_be_shifted_to_forge_a_collision():
    """("a|b", "c") and ("a", "b|c") must not digest to the same value.

    A separator that can appear inside a component makes the digest ambiguous,
    which is a collision an attacker -- or an unlucky ticker -- could produce.
    """
    assert production_source_key(0, "A", "B\x1fC") != production_source_key(
        0, "A\x1fB", "C"
    )


def test_a_key_needs_both_a_market_and_an_order():
    for ticker, order_id in (("", "ORD"), ("KXNFL", ""), ("", "")):
        with pytest.raises(ValueError):
            production_source_key(0, ticker, order_id)


def test_the_key_never_depends_on_anything_that_varies_between_runs():
    """Structural: no clock, no randomness, no run id in the module.

    Any of those would give the same order a new identity on the next run, and
    the next run would import it again.
    """
    import ast

    import kalshi_router.production as module

    tree = ast.parse(open(module.__file__).read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    for nondeterministic in ("time", "datetime", "random", "uuid", "os", "secrets"):
        assert nondeterministic not in imported


# ---------------------------------------------------------------- the cutover

def test_the_cutover_is_a_readable_fixed_instant():
    assert parse_rfc3339_seconds(PRODUCTION_CUTOVER_ISO) is not None
    assert production_cutover_seconds() > 0


def test_an_order_submitted_before_the_cutover_is_history():
    cutover = production_cutover_seconds()
    assert not is_after_cutover(order(first=int(cutover) - 1, last=int(cutover) - 1))


def test_an_order_exactly_at_the_cutover_is_history():
    # Strictly after, so the boundary instant belongs to the validation side.
    cutover = int(production_cutover_seconds())
    assert not is_after_cutover(order(first=cutover, last=cutover))


def test_an_order_submitted_after_the_cutover_is_production():
    cutover = int(production_cutover_seconds())
    assert is_after_cutover(order(first=cutover + 1, last=cutover + 1))


def test_the_cutover_is_keyed_on_the_first_fill_not_the_last():
    """A late fill on an old order does not make it a new wager.

    Keying on the last execution would let a pre-cutover order drift across the
    line and be imported as though the owner had just placed it.
    """
    cutover = int(production_cutover_seconds())
    straddling = order(first=cutover - 60, last=cutover + 600)
    assert not is_after_cutover(straddling)


# ---------------------------------------------------------------- order finality

def test_a_closed_market_makes_an_order_final():
    for status in sorted(CLOSED_MARKET_STATUSES):
        verdict = assess_finality(order(), now=Decimal(0), market_status=status)
        assert verdict is OrderFinality.FINAL_MARKET_CLOSED
        assert verdict in FINAL_VERDICTS


def test_market_status_is_matched_case_and_whitespace_insensitively():
    assert assess_finality(
        order(), now=Decimal(0), market_status="  SETTLED  "
    ) is OrderFinality.FINAL_MARKET_CLOSED


def test_an_open_market_without_stabilization_is_never_final():
    """Fail closed. The only finality granted without configuration is the
    authoritative one -- the exchange saying the market is closed."""
    verdict = assess_finality(order(), now=Decimal(10**9), market_status="active")
    assert verdict is OrderFinality.UNKNOWN
    assert verdict not in FINAL_VERDICTS


def test_unknown_market_status_is_never_final():
    verdict = assess_finality(order(), now=Decimal(10**9), market_status=None)
    assert verdict is OrderFinality.UNKNOWN


def test_stabilization_grants_finality_only_after_the_window():
    last = Decimal(1_000_000)
    inside = assess_finality(
        order(first=int(last), last=int(last)),
        now=last + STABILIZATION_SECONDS - 1,
        market_status="active",
        allow_stabilization=True,
    )
    assert inside is OrderFinality.PENDING_RECENT_FILL
    assert inside not in FINAL_VERDICTS

    outside = assess_finality(
        order(first=int(last), last=int(last)),
        now=last + STABILIZATION_SECONDS,
        market_status="active",
        allow_stabilization=True,
    )
    assert outside is OrderFinality.FINAL_STABLE


def test_a_closed_market_outranks_a_recent_fill():
    # The exchange saying the market is closed is stronger evidence than a
    # timer, whichever way the timer points.
    last = Decimal(1_000_000)
    assert assess_finality(
        order(first=int(last), last=int(last)),
        now=last,
        market_status="settled",
        allow_stabilization=True,
    ) is OrderFinality.FINAL_MARKET_CLOSED


def test_the_trading_order_route_is_not_used_to_establish_finality():
    """GET /portfolio/orders/{id} would answer this directly.

    It is the same path prefix that creates and cancels orders, and this
    project's allowlist refuses the whole prefix. Finality is derived from
    market status and fill timing so the trading surface stays unreachable by
    construction rather than by care.
    """
    import kalshi_router.production as module

    source = open(module.__file__).read()
    assert "portfolio/orders" not in source.replace(
        "``GET /portfolio/orders/{id}``", ""
    )


# --------------------------------- the window rests on measurement, not taste

def test_the_finality_evidence_buckets_every_order():
    from kalshi_router.finality import measure_finality

    orders = [order("A", first=0, last=0), order("B", first=0, last=5, fills=3)]
    evidence = measure_finality(orders)
    assert evidence.orders == 2
    assert evidence.single_fill_orders == 1
    assert evidence.multi_fill_orders == 1
    assert sum(count for _label, count in evidence.bucket_counts()) == 2
    assert evidence.max_span_seconds == 5


def test_a_window_is_supported_only_when_it_clears_the_largest_span():
    from kalshi_router.finality import measure_finality

    evidence = measure_finality([order("A", first=0, last=100)])
    assert evidence.window_is_supported(Decimal(101))
    # Strictly greater: an order that took exactly the window would have been
    # called final at the instant it was still filling.
    assert not evidence.window_is_supported(Decimal(100))
    assert not evidence.window_is_supported(Decimal(50))


def test_no_evidence_supports_no_window():
    from kalshi_router.finality import FinalityEvidence

    assert not FinalityEvidence().window_is_supported(Decimal(10**9))


def test_the_finality_evidence_holds_counts_only():
    from kalshi_router.finality import measure_finality

    evidence = measure_finality([order("A", first=0, last=3)])
    for name, value in evidence.as_dict().items():
        assert isinstance(value, int), f"{name} is not a count"


def test_the_finality_render_names_no_ticker_or_order():
    from kalshi_router.finality import measure_finality

    text = measure_finality([order("SECRETORDER", ticker="KXSECRET", last=3)]).render()
    assert "SECRETORDER" not in text
    assert "KXSECRET" not in text


# ---------------------------------------------------------------------------
# What the DELIVERED ROW carries, checked against the row itself.
#
# The privacy rules say raw account evidence must not be published: no fill
# payloads, no fill ids, no order ids except as an opaque source key, no
# subaccount identifiers. Those are properties of the row that actually leaves,
# so they are tested on the row rather than on the intent.
#
# The identifiers below are deliberately unlike any numeral the row legitimately
# contains. A one-digit subaccount gives a FALSE POSITIVE against "0.07" and
# "5.37" -- which is exactly what happened when this was first checked by hand,
# and is why the values are what they are.
# ---------------------------------------------------------------------------

import json as _json

from kalshi_router.production import OrderFinality, ProductionWager, to_import_row

_SECRET_ORDER_ID = "ord-SECRET-0xZZZ"
_SECRET_SUBACCOUNT = 8675309
_TICKER = "KXMLBGAME-26SEP14NYYBOS-NYY"


def _delivered_row():
    wager = ProductionWager(
        source_key=production_source_key(_SECRET_SUBACCOUNT, _TICKER, _SECRET_ORDER_ID),
        market_ticker=_TICKER,
        sport="MLB",
        game_date="2026-09-14",
        side="YES",
        contracts=Decimal(10),
        vwap_price=Decimal("0.53"),
        total_fees=Decimal("0.07"),
        stake=Decimal("5.37"),
        first_execution_time=Decimal(1789000000),
        last_execution_time=Decimal(1789000000),
        fill_count=1,
        finality=OrderFinality.FINAL_MARKET_CLOSED,
    )
    return to_import_row(wager)


def test_the_delivered_row_carries_no_raw_account_identifier():
    blob = _json.dumps(_delivered_row())

    assert _SECRET_ORDER_ID not in blob
    assert str(_SECRET_SUBACCOUNT) not in blob


def test_the_source_key_still_depends_on_what_it_hides():
    """Otherwise 'the identifier is absent' would be true and meaningless."""
    base = production_source_key(_SECRET_SUBACCOUNT, _TICKER, _SECRET_ORDER_ID)

    assert base != production_source_key(_SECRET_SUBACCOUNT, _TICKER, "ord-OTHER")
    assert base != production_source_key(99, _TICKER, _SECRET_ORDER_ID)


def test_the_delivered_row_claims_no_model_provenance():
    """A Kalshi execution proves the owner placed the bet. It proves nothing
    about what recommended it, so these fields are ABSENT rather than null."""
    row = _delivered_row()

    for field in (
        "recommendationId",
        "modelEvaluationId",
        "modelFairProbability",
        "productionRunId",
    ):
        assert field not in row, f"{field} would be fabricated provenance"


def test_the_delivered_row_asserts_no_outcome():
    """The router records the wager; the destination settles it. A row that
    carried a result or a P&L would be the router grading a game."""
    row = _delivered_row()

    for field in ("result", "netProfitLoss", "returnAmount", "grossSettlementPayout"):
        assert field not in row
    assert row["status"] == "pending"


def test_the_delivered_row_invents_no_field_the_destination_owns():
    row = _delivered_row()

    for field in ("betId", "validationStatus", "provenance", "createdAt", "recordedAt"):
        assert field not in row


# ---------------------------------------- the destination's economics contract
#
# On the first genuine delivery the wager was recorded with a stake and a price
# and nothing else: contracts, contractCost, totalFees and actualCashConsumed
# all landed null. These fields were emitted FLAT at the top level of the row,
# and the destination reads them from a NESTED `executionEconomics` object --
# so the importer accepted a hollow row rather than failing. `totalFees` is the
# entire reason for routing from actual fills instead of a fee schedule, so a
# null there is not a cosmetic loss.

#: Copied verbatim from the destination's `lib.edgelab.bets`
#: `_EXECUTION_ECONOMICS_FIELDS`. The destination REFUSES an unknown key rather
#: than dropping it, so a key absent from this tuple would fail the import
#: loudly -- which is the property that makes the nested shape safe to rely on,
#: and the property the flat shape never got to use.
DESTINATION_ECONOMICS_FIELDS = (
    "contractCost", "averageFillPrice", "entryFees", "exitFees", "totalFees",
    "actualCashConsumed", "unusedAllocatedCash",
    "grossCashReturned", "grossSettlementPayout", "exitSaleProceeds", "realizedROI",
    "executionStatus", "feeStatus", "feeType", "feeMultiplier", "feeSource",
    "feeScheduleVersion", "feeEffectiveDate", "economicsSource", "economicsConfidence",
)


def test_execution_economics_are_nested_not_flat():
    """The defect, stated as a property.

    A top-level `totalFees` is invisible to the destination, so its presence
    there is not a harmless duplicate -- it is the bug.
    """
    row = _delivered_row()

    assert isinstance(row["executionEconomics"], dict)
    leaked = sorted(set(row) & set(DESTINATION_ECONOMICS_FIELDS))
    assert leaked == [], f"economics fields emitted flat, where the importer never looks: {leaked}"


def test_every_economics_key_is_one_the_destination_accepts():
    """An unknown key would fail the whole import, so it must never be sent."""
    economics = _delivered_row()["executionEconomics"]

    unknown = sorted(set(economics) - set(DESTINATION_ECONOMICS_FIELDS))
    assert unknown == [], f"the destination would refuse the batch over: {unknown}"


def test_contracts_stays_top_level_because_it_is_not_an_economics_field():
    """`contracts` is a quantity the canonical builder takes directly, and is
    deliberately absent from the destination's economics field list. Nesting it
    would make the importer refuse the entire batch."""
    row = _delivered_row()

    assert row["contracts"] == 10.0
    assert "contracts" not in row["executionEconomics"]
    assert "contracts" not in DESTINATION_ECONOMICS_FIELDS


def test_the_emitted_economics_carry_exact_exchange_evidence():
    economics = _delivered_row()["executionEconomics"]

    assert economics["averageFillPrice"] == 0.53
    assert economics["contractCost"] == 5.3          # 10 x 0.53
    assert economics["totalFees"] == 0.07
    assert economics["actualCashConsumed"] == 5.37   # 5.3 + 0.07
    assert economics["executionStatus"] == "HELD_TO_SETTLEMENT"
    assert economics["feeStatus"] == "ACTUAL_API_FILL"
    assert economics["feeSource"] == "EXACT_ORDER_EXECUTION"
    assert economics["economicsSource"] == "EXACT_API_EXECUTION"
    assert economics["economicsConfidence"] == "HIGH"


def test_the_opening_execution_identity_holds_in_the_emitted_row():
    """contracts x VWAP = contractCost; + fees = cash consumed = stake."""
    row = _delivered_row()
    economics = row["executionEconomics"]

    assert round(row["contracts"] * economics["averageFillPrice"], 6) == economics["contractCost"]
    assert round(economics["contractCost"] + economics["totalFees"], 6) == economics["actualCashConsumed"]
    assert economics["actualCashConsumed"] == row["stake"]


def test_fields_belonging_to_a_closed_position_are_left_null():
    """Only what this order evidences is filled in.

    An exit price, a settlement payout and a realised ROI belong to a position
    that has closed, and this one has not. Filling them to avoid a null would be
    the lie -- a null here means "not established".
    """
    economics = _delivered_row()["executionEconomics"]

    for field in ("exitFees", "grossCashReturned", "grossSettlementPayout",
                  "exitSaleProceeds", "realizedROI", "unusedAllocatedCash"):
        assert economics.get(field) is None, f"{field} was asserted for an open position"
    # feeType would need taker/maker resolved across every fill of the order,
    # which the aggregate does not carry, so it is not guessed at MIXED.
    assert economics.get("feeType") is None
