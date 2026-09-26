"""A SELL is not a purchase: the NFL row records the EXPOSURE an order created, and says how it was executed.

Kalshi's `outcome_side` names the CONTRACT traded. The production path used it as the side, so a sell of YES at
0.33 would have been delivered as "YES at 0.33, stake 0.33 x q + fee" -- a sale written as a purchase. On the
netted book the same order is exactly NO at 0.67 (it costs 0.67 x q + fee and pays (1 - value) x q at
settlement against the YES it closed), which is what NFL now records, with `execution_action`.

The owner's TNF 2026-09-24 cashout (91.41 on the Love 275 rung) arrived as outcome_side=no, a buy of NO; that
already matched its exposure, and this change leaves it byte-identical apart from the verb.
"""
from __future__ import annotations

from decimal import Decimal

from kalshi_router.accounting.execution import aggregate_orders
from kalshi_router.models import Action, OutcomeSide, normalize_fill
from kalshi_router.production import (
    EXPOSURE_SIDE_DESTINATIONS,
    evaluate_order,
    evaluate_production,
    production_cutover_seconds,
    to_nfl_import_row,
)
from tests.synthetic import make_fill

AFTER = int(production_cutover_seconds()) + 3600
NOW = Decimal(AFTER + 10_000)
NFL_TICKER = "KXNFLPASSYDS-26SEP24ATLGB-GBJLOVE10-275"
WHEN = "2026-09-25T02:43:34Z"


def _order(action, side, yes_price, ticker=NFL_TICKER, count=91):
    from datetime import datetime, timezone
    created = datetime.fromtimestamp(AFTER, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = make_fill(1, ticker=ticker, action=action, side=side, count=count, created_time=created,
                    yes_price_dollars=yes_price, no_price_dollars=f"{Decimal(1) - Decimal(yes_price):.4f}",
                    fee_cost="1.41")
    orders = aggregate_orders([normalize_fill(raw)])
    return next(iter(orders.values()))


def _eval(o, sport="NFL"):
    return evaluate_order(o, sport, "2026-09-24", "settled", NOW, frozenset({sport}))


def test_order_carries_exposure_and_verb():
    o = _order("sell", "yes", "0.3300")
    assert o.outcome_side is OutcomeSide.YES          # the contract traded
    assert o.exposure_side is OutcomeSide.NO          # the direction it moved the position
    assert o.legacy_action is Action.SELL
    assert o.is_sell
    assert not _order("buy", "no", "0.3300").is_sell


def test_nfl_sell_yes_is_recorded_as_the_no_exposure_it_created():
    w, refusal, _ = _eval(_order("sell", "yes", "0.3300"))
    assert refusal is None
    assert (w.side, w.vwap_price, w.execution_action) == ("NO", Decimal("0.6700"), "SELL")
    assert w.stake == Decimal("0.6700") * 91 + w.total_fees
    row = to_nfl_import_row(w, "kalshi-router-v1")
    assert row["side"] == "NO" and row["execution_action"] == "SELL"


def test_nfl_buy_no_cashout_is_unchanged_apart_from_the_verb():
    w, _, _ = _eval(_order("buy", "no", "0.3300"))
    assert (w.side, w.vwap_price, w.execution_action) == ("NO", Decimal("0.6700"), "BUY")


def test_nfl_sell_no_is_the_yes_exposure():
    w, _, _ = _eval(_order("sell", "no", "0.2600"))
    assert (w.side, w.vwap_price, w.execution_action) == ("YES", Decimal("0.2600"), "SELL")


def test_other_destinations_keep_contract_semantics_and_are_counted():
    assert "MLB" not in EXPOSURE_SIDE_DESTINATIONS
    o = _order("sell", "yes", "0.3300", ticker="KXMLBGAME-26SEP20SFLAD-SF")
    w, _, _ = _eval(o, sport="MLB")
    assert w.side == "YES"  # unchanged until MLB's own historical sells are checked
    _, diag = evaluate_production([o], {o.ticker: "MLB"}, {o.ticker: "2026-09-20"}, {o.ticker: "settled"}, NOW,
                                  frozenset({"MLB"}))
    assert diag.sell_orders_recorded_by_contract == 1 and diag.sell_orders_recorded_by_exposure == 0


def test_nfl_sells_are_counted_by_exposure():
    o = _order("sell", "yes", "0.3300")
    _, diag = evaluate_production([o], {o.ticker: "NFL"}, {o.ticker: "2026-09-24"}, {o.ticker: "settled"}, NOW,
                                  frozenset({"NFL"}))
    assert diag.sell_orders_recorded_by_exposure == 1
    assert "by exposure, with execution_action: 1" in diag.render()


def test_hand_built_execution_without_exposure_keeps_old_behaviour():
    from tests.test_production_filter import order as hand_built
    o = hand_built(ticker=NFL_TICKER, side=OutcomeSide.NO)
    w, _, _ = _eval(o)
    assert w.side == "NO" and w.execution_action is None
    assert "execution_action" not in to_nfl_import_row(w, "kalshi-router-v1")
