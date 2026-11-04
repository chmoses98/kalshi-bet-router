"""Price and fee stay separate: a price-only comparison never contains a fee, and a fee reaches net P&L exactly once.

Price-only CLV compares the fill PRICE with the close PRICE (``closes.ResolvedClose``); fee-adjusted economics live in
``stake`` / ``net_profit_loss``. The router computes no CLV itself; these tests pin that the inputs a CLV consumer
reads (``vwap_price``, the close price) carry no fee, and that the fee enters net P&L once.
"""
from __future__ import annotations

from decimal import Decimal as D

from kalshi_router.accounting.execution import weighted_average_price
from kalshi_router.execution_evidence import build_execution_evidence
from kalshi_router.models import NormalizedSettlement
from kalshi_router.settlement import ECONOMICS_V2, settle_batch

from kalshi_router.accounting.execution import OrderExecution
from kalshi_router.models import Action, OutcomeSide
from kalshi_router.production import OrderFinality, ProductionWager

TICKER = "KXNFLSPREAD-26SEP20INDKC-KC3"


def order(**kw) -> OrderExecution:
    base = dict(
        order_id="o", ticker=TICKER, outcome_side=OutcomeSide.YES, book_side=None, subaccount_number=0,
        total_quantity=D(10), fill_count=2, fill_ids=("a", "b"), first_execution_time=D(1788264060),
        last_execution_time=D(1788264075), vwap_price=D("0.56"), total_fee=D("0.01"), fills_with_fee=2,
        all_taker=True, legacy_action=Action.BUY)
    base.update(kw)
    return OrderExecution(**base)


def wager(key, side, contracts, price, fee):
    contracts, price, fee = D(contracts), D(price), D(fee)
    return ProductionWager(
        source_key=key, market_ticker=TICKER, sport="NFL", game_date="2026-09-20", side=side, contracts=contracts,
        vwap_price=price, total_fees=fee, stake=contracts * price + fee, first_execution_time=D(1),
        last_execution_time=D(1), fill_count=1, finality=OrderFinality.FINAL_MARKET_CLOSED)


def test_sidecar_price_is_fee_free_and_fee_is_a_separate_field():
    cheap = build_execution_evidence(order(total_fee=D("0.01"))).to_sidecar_row()
    dear = build_execution_evidence(order(total_fee=D("0.90"))).to_sidecar_row()
    assert cheap["vwap_price"] == dear["vwap_price"] == 0.56
    assert (cheap["total_fee"], dear["total_fee"]) == (0.01, 0.9)


def test_vwap_ignores_fee_fields():
    class Fill:
        def __init__(self, price, count, fee):
            self.price_dollars, self.count, self.fee_dollars = D(price), D(count), D(fee)

    assert weighted_average_price([Fill("0.50", 1, "0.05"), Fill("0.60", 3, "0.50")]) == D("0.575")


def test_entry_fee_reaches_net_pnl_exactly_once():
    results = {}
    for fee in ("0.00", "0.21"):
        w = wager("k", "YES", "10", "0.60", fee)
        s = NormalizedSettlement(ticker=TICKER, settled_time="2026-09-21T03:00:00Z", market_result="yes",
                                 revenue_dollars=D("10"), market_value_dollars=D("1.00"), fee_dollars=D(fee))
        results[fee] = settle_batch([w], {TICKER: s}, {"NFL": ECONOMICS_V2})[0]
    assert results["0.00"].net_profit_loss == D("4.00")
    assert results["0.00"].net_profit_loss - results["0.21"].net_profit_loss == D("0.21")  # once, not twice
