"""The production filter: which orders are delivered, and why the rest are not.

Every gate here is a place where letting one order through would put a wrong or
duplicate canonical wager into a public ledger. A refusal is the normal outcome
and never an error -- what it must never be is silent.
"""

from __future__ import annotations

from decimal import Decimal

from kalshi_router.accounting.execution import OrderExecution
from kalshi_router.models import OutcomeSide
from kalshi_router.production import (
    OrderFinality,
    ProductionRefusal,
    evaluate_order,
    evaluate_production,
    production_cutover_seconds,
    production_source_key,
)

MLB = frozenset({"MLB"})
AFTER = int(production_cutover_seconds()) + 3600
BEFORE = int(production_cutover_seconds()) - 3600
NOW = Decimal(AFTER + 10_000)


def order(
    order_id="ORD-1",
    ticker="KXMLBGAME-26SEP20SFLAD-SF",
    when=AFTER,
    price="0.56",
    fee="0.01",
    side=OutcomeSide.YES,
    quantity=10,
):
    return OrderExecution(
        order_id=order_id,
        ticker=ticker,
        outcome_side=side,
        book_side=None,
        subaccount_number=0,
        total_quantity=Decimal(quantity),
        fill_count=1,
        fill_ids=("F1",),
        first_execution_time=Decimal(when),
        last_execution_time=Decimal(when),
        vwap_price=None if price is None else Decimal(price),
        total_fee=None if fee is None else Decimal(fee),
        fills_with_fee=1,
    )


def run(o, sport="MLB", date="2026-09-20", status="settled", destinations=MLB):
    return evaluate_order(o, sport, date, status, NOW, destinations)


# ------------------------------------------------------------- the happy path

def test_an_eligible_order_becomes_a_deliverable_wager():
    wager, refusal, finality = run(order())
    assert refusal is None
    assert finality is OrderFinality.FINAL_MARKET_CLOSED
    assert wager.sport == "MLB"
    assert wager.side == "YES"
    assert wager.contracts == Decimal(10)
    assert wager.vwap_price == Decimal("0.56")
    # stake = contract cost + exchange fees, both exchange-stated.
    assert wager.stake == Decimal("0.56") * 10 + Decimal("0.01")
    assert wager.source_key == production_source_key(
        0, "KXMLBGAME-26SEP20SFLAD-SF", "ORD-1"
    )


def test_a_no_side_order_records_the_contract_price_not_the_yes_axis():
    # The ledger is denominated on the signed YES axis; the destination records
    # what was paid for the contract actually held.
    wager, refusal, _ = run(order(side=OutcomeSide.NO, price="0.56"))
    assert refusal is None
    assert wager.side == "NO"
    assert wager.vwap_price == Decimal("0.44")
    assert wager.stake == Decimal("0.44") * 10 + Decimal("0.01")


# ----------------------------------------------------------------- the gates

def test_a_pre_cutover_order_is_refused_first_and_reports_no_finality():
    """History is not a failure of any later gate.

    Checking the cutover first is free and it rejects almost everything. If a
    later gate reported on historical orders too, the history would look broken.
    """
    wager, refusal, finality = run(order(when=BEFORE), sport=None, date=None, status=None)
    assert refusal is ProductionRefusal.BEFORE_CUTOVER
    assert wager is None
    assert finality is None


def test_an_order_on_an_open_market_is_not_final_and_is_refused():
    _w, refusal, finality = run(order(), status="active")
    assert refusal is ProductionRefusal.ORDER_NOT_FINAL
    assert finality is OrderFinality.UNKNOWN


def test_an_order_with_no_price_is_refused_rather_than_averaged():
    _w, refusal, _f = run(order(price=None))
    assert refusal is ProductionRefusal.NO_EXECUTION_PRICE


def test_an_order_with_an_incomplete_fee_is_refused_rather_than_reconstructed():
    _w, refusal, _f = run(order(fee=None))
    assert refusal is ProductionRefusal.FEES_INCOMPLETE


def test_an_unclassified_market_is_distinct_from_an_unresolved_one():
    # "Never attempted" and "attempted and could not tell" are different
    # problems with different fixes.
    assert run(order(), sport=None)[1] is ProductionRefusal.MARKET_NOT_CLASSIFIED
    assert run(order(), sport="UNRESOLVED")[1] is ProductionRefusal.SPORT_UNRESOLVED


def test_other_is_refused_as_firmly_as_unresolved():
    # OTHER means positively not a sport this router carries. Never route it.
    assert run(order(), sport="OTHER")[1] is ProductionRefusal.SPORT_UNRESOLVED


def test_a_sport_with_no_destination_is_refused():
    for sport in ("NFL", "CFB", "TENNIS"):
        assert run(order(), sport=sport)[1] is ProductionRefusal.NO_DESTINATION_IMPORTER


def test_an_unestablished_game_date_is_refused():
    assert run(order(), date=None)[1] is ProductionRefusal.GAME_DATE_NOT_ESTABLISHED
    assert run(order(), date="")[1] is ProductionRefusal.GAME_DATE_NOT_ESTABLISHED


# ------------------------------------------------------------- the aggregate

def test_every_order_is_either_eligible_or_refused_exactly_once():
    orders = [
        order("A", when=AFTER),
        order("B", when=BEFORE),
        order("C", when=AFTER, price=None),
    ]
    wagers, d = evaluate_production(
        orders,
        {"KXMLBGAME-26SEP20SFLAD-SF": "MLB"},
        {"KXMLBGAME-26SEP20SFLAD-SF": "2026-09-20"},
        {"KXMLBGAME-26SEP20SFLAD-SF": "settled"},
        NOW,
        MLB,
    )
    assert d.orders_considered == 3
    assert d.eligible + d.refusals_total == 3
    assert len(wagers) == d.eligible == 1


def test_only_post_cutover_orders_are_counted_as_after_cutover():
    orders = [order("A", when=AFTER), order("B", when=BEFORE)]
    _w, d = evaluate_production(orders, {}, {}, {}, NOW, MLB)
    assert d.orders_after_cutover == 1
    assert d.refused_before_cutover == 1


def test_no_post_cutover_order_is_a_healthy_no_op():
    _w, d = evaluate_production([order("A", when=BEFORE)], {}, {}, {}, NOW, MLB)
    assert d.is_healthy_no_op
    assert d.eligible == 0


def test_a_deferred_post_cutover_order_is_not_a_healthy_no_op():
    """A gate holding something back is the system working -- but it is not
    nothing, and a health signal that called it nothing would hide it."""
    _w, d = evaluate_production(
        [order("A", when=AFTER)], {}, {}, {"KXMLBGAME-26SEP20SFLAD-SF": "active"}, NOW, MLB
    )
    assert not d.is_healthy_no_op
    assert d.refused_order_not_final == 1


def test_the_diagnostics_hold_counts_only():
    _w, d = evaluate_production([order("A")], {}, {}, {}, NOW, MLB)
    for name, value in d.as_dict().items():
        assert isinstance(value, int), f"{name} is not a count"


def test_the_rendered_output_names_no_ticker_key_amount_or_date():
    _w, d = evaluate_production(
        [order("SECRETORDER", ticker="KXSECRET")],
        {"KXSECRET": "MLB"},
        {"KXSECRET": "2026-09-20"},
        {"KXSECRET": "settled"},
        NOW,
        MLB,
    )
    text = d.render()
    for token in ("SECRETORDER", "KXSECRET", "2026-09-20", "0.56", "kalshi:v1:", "$"):
        assert token not in text
