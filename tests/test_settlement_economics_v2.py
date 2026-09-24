"""Settlement economics v2: a fee is subtracted once, and a test fails the moment it is subtracted twice.

v1 computed ``net = gross - stake - fee_cost``. ``stake`` already includes the entry fee from the order's own
fills, and Kalshi's settlement ``fee_cost`` is the market position's CUMULATIVE trading fee (every one of the
owner's orders on the market, both sides). On 2026 NFL week 2 that double-counted $112.24: $33.07 on the single
orders (each entry fee twice) and $79.17 on two YES+NO pairs (the market's combined fee subtracted again on EACH
leg). The identities below are cash flows, not formulas copied from the code:

    per market:  sum(net) == revenue received - sum(principal) - sum(entry fees)
    per order:   net == payout - contracts x price - entry fee
"""
from __future__ import annotations

from decimal import Decimal as D

import pytest

from kalshi_router.destination import _settlement_row
from kalshi_router.models import NormalizedSettlement
from kalshi_router.production import OrderFinality, ProductionWager
from kalshi_router.settlement import (
    ECONOMICS_V1,
    ECONOMICS_V2,
    ReturnRefusal,
    fee_evidence,
    settle_batch,
)

TICKER = "KXNFLSPREAD-26SEP20INDKC-KC3"
V2 = {"NFL": ECONOMICS_V2}


def order(key, side, contracts, price, fee, ticker=TICKER):
    contracts, price, fee = D(contracts), D(price), D(fee)
    return ProductionWager(
        source_key=key, market_ticker=ticker, sport="NFL", game_date="2026-09-20", side=side,
        contracts=contracts, vwap_price=price, total_fees=fee, stake=contracts * price + fee,
        first_execution_time=D(1), last_execution_time=D(1), fill_count=1, finality=OrderFinality.FINAL_MARKET_CLOSED)


def settled(result, fee_cost, ticker=TICKER, revenue=None):
    value = D("1.00") if result == "yes" else D("0.00")
    return NormalizedSettlement(ticker=ticker, settled_time="2026-09-21T03:00:00Z", market_result=result,
                                revenue_dollars=D(revenue) if revenue is not None else D("0"),
                                market_value_dollars=value,
                                fee_dollars=None if fee_cost is None else D(fee_cost))


def cash_net(w, won):
    payout = w.contracts if won else D(0)
    return payout - w.contracts * w.vwap_price - w.total_fees


def run(wagers, settlement, version=V2):
    return {s.source_bet_key: s for s in settle_batch(wagers, {settlement.ticker: settlement}, version)}


@pytest.mark.parametrize("side,result,won", [("YES", "yes", True), ("YES", "no", False),
                                              ("NO", "no", True), ("NO", "yes", False)])
def test_a_single_order_nets_exactly_its_cash_flow(side, result, won):
    w = order("k", side, "10", "0.60", "0.21")
    s = run([w], settled(result, "0.21"))["k"]
    assert s.economics_version == ECONOMICS_V2 and s.refusals == ()
    assert s.net_profit_loss == cash_net(w, won)
    assert s.result == ("WON" if won else "LOST")


def test_v1_is_the_defect_it_subtracts_the_same_fee_twice():
    w = order("k", "YES", "10", "0.60", "0.21")
    v1 = run([w], settled("yes", "0.21"), {"NFL": ECONOMICS_V1})["k"]
    v2 = run([w], settled("yes", "0.21"))["k"]
    assert v2.net_profit_loss == cash_net(w, True)
    assert v1.net_profit_loss == v2.net_profit_loss - D("0.21")      # the entry fee, a second time


def test_multiple_fills_use_the_quantity_weighted_price():
    # 7 @ 0.50 + 3 @ 0.70 -> vwap 0.56 over 10 contracts; the order is one wager with one fee total.
    w = order("k", "YES", "10", "0.56", "0.19")
    assert run([w], settled("no", "0.19"))["k"].net_profit_loss == D("-5.79")


def test_a_yes_no_pair_on_one_market_nets_each_leg_once_and_sums_to_the_market_cash_flow():
    """The IND@KC shape: fee_cost is the market's combined fee, and it is already inside both stakes."""
    yes = order("y", "YES", "100", "0.95", "31.1955")
    no = order("n", "NO", "40", "0.05", "2.2110")
    s = run([yes, no], settled("no", "33.4065"))
    assert s["y"].net_profit_loss == cash_net(yes, False)
    assert s["n"].net_profit_loss == cash_net(no, True)
    market_cash = no.contracts - (yes.contracts * yes.vwap_price + no.contracts * no.vwap_price) \
        - (yes.total_fees + no.total_fees)
    assert s["y"].net_profit_loss + s["n"].net_profit_loss == market_cash


def test_v1_charges_a_pairs_combined_fee_to_each_leg():
    yes = order("y", "YES", "100", "0.95", "31.1955")
    no = order("n", "NO", "40", "0.05", "2.2110")
    v1 = run([yes, no], settled("no", "33.4065"), {"NFL": ECONOMICS_V1})
    v2 = run([yes, no], settled("no", "33.4065"))
    overcharge = sum(v2[k].net_profit_loss - v1[k].net_profit_loss for k in ("y", "n"))
    assert overcharge == 2 * D("33.4065")                            # the week-2 shape of the $112.24


def test_two_orders_on_one_side_are_established_when_the_fee_reconciles():
    """v1 refused these as a shared-position fee (week 1's two unestablished wagers)."""
    a, b = order("a", "YES", "30", "0.40", "2.9138"), order("b", "YES", "30", "0.40", "2.9138")
    v2 = run([a, b], settled("no", "5.8276"))
    assert v2["a"].net_profit_loss == cash_net(a, False) and v2["b"].net_profit_loss == cash_net(b, False)
    v1 = run([a, b], settled("no", "5.8276"), {"NFL": ECONOMICS_V1})
    assert v1["a"].refusals == (ReturnRefusal.SHARED_POSITION_FEE.value,)


@pytest.mark.parametrize("fee_cost", ["0.30", "0.10"])
def test_a_fee_the_fills_do_not_explain_is_refused_not_charged(fee_cost):
    w = order("k", "YES", "10", "0.60", "0.21")
    s = run([w], settled("yes", fee_cost))["k"]
    assert s.net_profit_loss is None and s.gross_return == D("10")
    assert s.refusals == (ReturnRefusal.FEE_NOT_RECONCILED.value,)


def test_an_absent_fee_cost_is_zero_only_when_no_entry_fee_was_charged():
    free = order("f", "YES", "10", "0.60", "0")
    assert run([free], settled("yes", None))["f"].net_profit_loss == D("4.00")
    paid = order("p", "YES", "10", "0.60", "0.21")
    assert run([paid], settled("yes", None))["p"].refusals == (ReturnRefusal.FEE_NOT_RECONCILED.value,)


def test_an_order_with_an_unknown_fee_cannot_reconcile_its_market():
    class NoFee:
        source_key, market_ticker, sport, side = "u", TICKER, "NFL", "YES"
        contracts, stake = D("10"), D("6.21")
    s = settle_batch([NoFee()], {TICKER: settled("yes", "0.21")}, V2)[0]
    assert s.refusals == (ReturnRefusal.FEE_NOT_RECONCILED.value,)


def test_only_a_v2_row_carries_its_economics_version_so_v1_destinations_see_no_new_field():
    w = order("k", "YES", "10", "0.60", "0.21")
    v2 = run([w], settled("yes", "0.21"))["k"]
    v1 = run([w], settled("yes", "0.21"), {"NFL": ECONOMICS_V1})["k"]
    assert _settlement_row(v2)["economics_version"] == ECONOMICS_V2
    assert "economics_version" not in _settlement_row(v1)


def test_a_sport_without_a_declared_contract_stays_on_v1():
    w = order("k", "YES", "10", "0.60", "0.21")
    assert run([w], settled("yes", "0.21"), {})["k"].economics_version == ECONOMICS_V1


def test_fee_evidence_is_counts_only_and_names_the_shape():
    yes = order("y", "YES", "100", "0.95", "31.1955")
    no = order("n", "NO", "40", "0.05", "2.2110")
    single = order("s", "YES", "10", "0.60", "0.21", ticker="KXNFLGAME-26SEP20CARATL-CAR")
    other = order("o", "YES", "10", "0.60", "0.21", ticker="KXNFLTOTAL-26SEP20CARATL-44")
    ev = fee_evidence([yes, no, single, other], {
        TICKER: settled("no", "33.4065"),
        "KXNFLGAME-26SEP20CARATL-CAR": settled("yes", "0.21", ticker="KXNFLGAME-26SEP20CARATL-CAR"),
        "KXNFLTOTAL-26SEP20CARATL-44": settled("yes", "0.42", ticker="KXNFLTOTAL-26SEP20CARATL-44")})
    assert ev == {"NFL|ONE_ORDER|FEE_COST_EQUALS_ENTRY_FEES": 1, "NFL|ONE_ORDER|FEE_COST_GREATER": 1,
                  "NFL|YES_NO_PAIR|FEE_COST_EQUALS_ENTRY_FEES": 1}
    assert all(isinstance(v, int) for v in ev.values())
