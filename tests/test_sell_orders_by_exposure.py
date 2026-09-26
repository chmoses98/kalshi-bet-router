"""The exchange-reported buy/sell verb travels to NFL as evidence; the side stays the contract as filed.

HISTORY. #94 briefly derived the NFL side from the legacy verb rule (sell-NO -> toward YES). The first production
delivery re-derived the owner's three filed cashouts as the opposite side (CONFLICT on side/actual_price/stake),
while the trade tape and the owner's account say the filed side is the true exposure. It was withdrawn; these
tests pin the withdrawal and the Love 275 cashout's filed shape.

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
    assert o.exposure_side is OutcomeSide.NO          # what the legacy verb rule says
    assert o.legacy_action is Action.SELL
    assert o.is_sell
    assert not _order("buy", "no", "0.3300").is_sell


def test_no_destination_derives_side_from_the_verb_rule():
    # Withdrawn after production: three filed NFL cashouts conflicted on side/price/stake.
    assert EXPOSURE_SIDE_DESTINATIONS == frozenset()


def test_filed_love_275_cashout_is_reproduced_byte_for_byte():
    """The TNF cashout as filed: NO, 0.67, stake 0.67 x q + fee. A sell verb must not flip it."""
    w, refusal, _ = _eval(_order("sell", "no", "0.3300"))
    assert refusal is None
    assert (w.side, w.vwap_price, w.execution_action) == ("NO", Decimal("0.6700"), "SELL")
    assert w.stake == Decimal("0.6700") * 91 + w.total_fees
    row = to_nfl_import_row(w, "kalshi-router-v1")
    assert row["side"] == "NO" and row["actual_price"] == 0.67 and row["execution_action"] == "SELL"


def test_nfl_sell_keeps_the_contract_side_and_reports_the_verb():
    w, _, _ = _eval(_order("sell", "yes", "0.3300"))
    assert (w.side, w.vwap_price, w.execution_action) == ("YES", Decimal("0.3300"), "SELL")


def test_buy_rows_are_unchanged_apart_from_the_verb():
    w, _, _ = _eval(_order("buy", "no", "0.3300"))
    assert (w.side, w.vwap_price, w.execution_action) == ("NO", Decimal("0.6700"), "BUY")


def test_sells_are_counted_as_recorded_by_contract():
    o = _order("sell", "no", "0.3300")
    _, diag = evaluate_production([o], {o.ticker: "NFL"}, {o.ticker: "2026-09-24"}, {o.ticker: "settled"}, NOW,
                                  frozenset({"NFL"}))
    assert diag.sell_orders_recorded_by_contract == 1 and diag.sell_orders_recorded_by_exposure == 0
    assert "by contract traded (destination semantics unchanged): 1" in diag.render()


def test_hand_built_execution_without_a_verb_sends_none():
    from tests.test_production_filter import order as hand_built
    o = hand_built(ticker=NFL_TICKER, side=OutcomeSide.NO)
    w, _, _ = _eval(o)
    assert w.side == "NO" and w.execution_action is None
    assert "execution_action" not in to_nfl_import_row(w, "kalshi-router-v1")
