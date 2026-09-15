"""Attributing a POSITION's settlement to an ORDER, and refusing where it cannot.

A Kalshi settlement is per market and per position. A wager is one order. When
two orders sit on one market and side, one settlement covers both and nothing
in it says which contracts came from which order.

The tempting move -- split `revenue` in proportion to contracts -- produces a
number indistinguishable from an exchange-stated one for a quantity the
exchange never stated per order. These tests pin what may be stated anyway and
what must be refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from kalshi_router.models import NormalizedSettlement
from kalshi_router.settlement import (
    LOST,
    PENDING,
    SETTLED,
    WON,
    ReturnRefusal,
    orders_by_position,
    settle_batch,
    settle_wager,
)

TICKER = "KXMLBGAME-26SEP12NYYBOS-NYY"


@dataclass
class FakeWager:
    source_key: str = "kalshi:v1:a"
    market_ticker: str = TICKER
    side: str = "YES"
    contracts: Decimal = Decimal(10)
    stake: Decimal = Decimal("5.61")


def settlement(**overrides) -> NormalizedSettlement:
    fields = {
        "ticker": TICKER,
        "settled_time": "2026-09-12T23:10:00Z",
        "market_result": "yes",
        "revenue_dollars": Decimal("10.00"),
        "market_value_dollars": Decimal("1.00"),
        "fee_dollars": Decimal("0"),
    }
    fields.update(overrides)
    return NormalizedSettlement(**fields)


# ------------------------------------------------------- what CAN be stated

def test_a_winning_wagers_return_is_the_published_price_times_its_contracts():
    """Not a share of a lump sum. `value` is the market's settlement price for
    a YES contract -- a per-contract price the exchange published."""
    result = settle_wager(FakeWager(), settlement())

    assert result.settlement_status == SETTLED
    assert result.result == WON
    assert result.gross_return == Decimal("10.00")
    assert result.net_profit_loss == Decimal("4.39")   # 10.00 - 5.61
    assert result.is_established


def test_a_losing_wager_returns_nothing_and_loses_its_stake():
    result = settle_wager(FakeWager(), settlement(market_result="no",
                                                  market_value_dollars=Decimal("0")))

    assert result.result == LOST
    assert result.gross_return == Decimal("0")
    assert result.net_profit_loss == Decimal("-5.61")


def test_a_no_side_wager_is_paid_the_complement_of_the_yes_price():
    """The same published price, read from the other side of the contract."""
    result = settle_wager(
        FakeWager(side="NO"),
        settlement(market_result="no", market_value_dollars=Decimal("0")),
    )

    assert result.result == WON
    assert result.gross_return == Decimal("10.00")


def test_a_market_settling_between_zero_and_one_is_paid_at_that_price():
    """Binary is the usual case, not the only one. A market that settles at
    0.40 pays 0.40 a contract, and assuming 1.00 would invent 60 cents."""
    result = settle_wager(FakeWager(), settlement(market_value_dollars=Decimal("0.40")))

    assert result.gross_return == Decimal("4.00")


def test_two_orders_on_one_position_each_get_an_exact_return():
    """THE CASE THE MODULE EXISTS FOR.

    Both returns rest on the same published per-contract price, so neither is
    a share of anything -- and they still sum to the position's revenue.
    """
    wagers = [
        FakeWager(source_key="kalshi:v1:a", contracts=Decimal(10), stake=Decimal("5.61")),
        FakeWager(source_key="kalshi:v1:b", contracts=Decimal(30), stake=Decimal("17.00")),
    ]
    results = settle_batch(wagers, {TICKER: settlement(revenue_dollars=Decimal("40.00"))})

    assert [r.gross_return for r in results] == [Decimal("10.00"), Decimal("30.00")]
    assert sum(r.gross_return for r in results) == Decimal("40.00")
    assert all(r.is_established for r in results)


# ------------------------------------------------------ what must be REFUSED

def test_a_settlement_with_no_price_establishes_no_return():
    """"The YES side won" does not establish what a contract paid. A market can
    settle anywhere between 0 and 1, so the return is never reconstructed from
    the result and a count."""
    result = settle_wager(FakeWager(), settlement(market_value_dollars=None))

    assert result.settlement_status == SETTLED
    assert result.result == WON
    assert result.gross_return is None
    assert result.net_profit_loss is None
    assert result.refusals == (ReturnRefusal.NO_SETTLEMENT_PRICE.value,)


def test_a_fee_on_a_shared_position_refuses_the_net_but_keeps_the_gross():
    """A fee is charged on the POSITION and is not a per-contract price, so
    splitting it IS a split of a lump sum. The gross return is unaffected --
    it rests on a published price -- so it is kept rather than discarded with
    the part that could not be established."""
    wagers = [FakeWager(source_key="kalshi:v1:a"), FakeWager(source_key="kalshi:v1:b")]
    results = settle_batch(wagers, {TICKER: settlement(fee_dollars=Decimal("0.25"))})

    for result in results:
        assert result.gross_return == Decimal("10.00")
        assert result.net_profit_loss is None
        assert result.refusals == (ReturnRefusal.SHARED_POSITION_FEE.value,)
        assert not result.is_established


def test_a_fee_on_a_position_of_one_order_needs_no_splitting():
    """The whole fee belongs to the only order there is."""
    result = settle_wager(FakeWager(), settlement(fee_dollars=Decimal("0.25")),
                          orders_on_this_position=1)

    assert result.net_profit_loss == Decimal("4.14")   # 10.00 - 5.61 - 0.25
    assert result.refusals == ()


def test_a_shared_position_with_no_fee_needs_no_splitting_either():
    """There is nothing to apportion, so nothing is refused."""
    wagers = [FakeWager(source_key="kalshi:v1:a"), FakeWager(source_key="kalshi:v1:b")]
    results = settle_batch(wagers, {TICKER: settlement(fee_dollars=Decimal("0"))})

    assert all(r.is_established for r in results)


def test_an_unsettled_market_is_pending_and_never_a_loss():
    """An unsettled wager and a wager that lost are different facts, and
    defaulting one to the other understates a bankroll that has not moved."""
    result = settle_wager(FakeWager(), None)

    assert result.settlement_status == PENDING
    assert result.result is None
    assert result.net_profit_loss is None
    assert result.refusals == (ReturnRefusal.NOT_SETTLED.value,)


def test_an_outcome_outside_yes_and_no_is_left_unmapped():
    """A void or a scalar settlement is not a binary win or loss, and forcing
    it into one would misstate it."""
    result = settle_wager(FakeWager(), settlement(market_result="void"))

    assert result.result is None


def test_the_position_count_is_supplied_not_inferred():
    """`settle_wager` sees one wager and cannot know what stands beside it.

    Inferring 1 silently would turn the one case that needs refusing into the
    one case that looks cleanest, so the count comes from the caller -- and
    `settle_batch` computes it over the whole batch.
    """
    wagers = [
        FakeWager(source_key="kalshi:v1:a"),
        FakeWager(source_key="kalshi:v1:b"),
        FakeWager(source_key="kalshi:v1:c", side="NO"),
    ]

    assert orders_by_position(wagers) == {(TICKER, "YES"): 2, (TICKER, "NO"): 1}


def test_a_refusal_and_a_figure_are_never_both_absent_without_a_reason():
    """Every missing money field carries the reason it is missing. A null with
    no explanation is the shape a reader fills in with a zero."""
    for result in (
        settle_wager(FakeWager(), None),
        settle_wager(FakeWager(), settlement(market_value_dollars=None)),
        settle_batch([FakeWager("a"), FakeWager("b")],
                     {TICKER: settlement(fee_dollars=Decimal("0.25"))})[0],
    ):
        assert result.net_profit_loss is None
        assert result.refusals, result
