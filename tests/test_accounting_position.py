"""Position state machine: every transition the exchange semantics allow."""

from __future__ import annotations

from decimal import Decimal

from kalshi_router.accounting import AccountingEngine, HistoryCompleteness, TransitionKind
from kalshi_router.accounting.position import Direction, project_fill
from kalshi_router.models import normalize_fill

from .synthetic import SYNTH_TICKER, make_accounting_fill

COMPLETE = HistoryCompleteness.COMPLETE


def replay(*specs):
    fills = [normalize_fill(make_accounting_fill(**spec)) for spec in specs]
    return AccountingEngine().replay(fills, COMPLETE)


def ledger(result):
    return result.ledgers[SYNTH_TICKER]


def kinds(result):
    return [t.kind.value for t in result.transitions]


# ------------------------------------------------- projection onto the axis

def test_buy_yes_is_positive_at_the_yes_price():
    signed, price = project_fill(normalize_fill(
        make_accounting_fill(index=1, quantity="10.00", action="buy", side="yes",
                             yes_price="0.6000")))
    assert signed == Decimal("10.00") and price == Decimal("0.6000")


def test_sell_yes_is_negative_at_the_yes_price():
    signed, price = project_fill(normalize_fill(
        make_accounting_fill(index=1, quantity="10.00", action="sell", side="yes",
                             yes_price="0.6000")))
    assert signed == Decimal("-10.00") and price == Decimal("0.6000")


def test_buy_no_is_negative_at_the_complementary_price():
    """Buying NO at $0.43 is economically selling YES at $0.57."""
    signed, price = project_fill(normalize_fill(
        make_accounting_fill(index=1, quantity="10.00", action="buy", side="no",
                             no_price="0.4300")))
    assert signed == Decimal("-10.00") and price == Decimal("0.5700")


def test_sell_no_is_positive_at_the_complementary_price():
    signed, price = project_fill(normalize_fill(
        make_accounting_fill(index=1, quantity="10.00", action="sell", side="no",
                             no_price="0.4300")))
    assert signed == Decimal("10.00") and price == Decimal("0.5700")


# ----------------------------------------------------- the ten requirements

def test_1_opening_a_position():
    result = replay({"index": 1, "quantity": "100.00", "yes_price": "0.6000"})
    assert kinds(result) == ["open"]
    assert ledger(result).position == Decimal("100.00")
    episode = ledger(result).episodes[0]
    assert episode.direction is Direction.LONG_YES
    assert episode.average_entry_price == Decimal("0.6000")
    assert episode.is_open is True


def test_2_adding_to_an_existing_position():
    result = replay(
        {"index": 1, "quantity": "100.00", "yes_price": "0.6000", "order_id": "O1"},
        {"index": 2, "quantity": "100.00", "yes_price": "0.7000", "order_id": "O1"},
    )
    assert kinds(result) == ["open", "increase"]
    assert ledger(result).position == Decimal("200.00")
    assert ledger(result).episodes[0].average_entry_price == Decimal("0.65")


def test_3_multiple_orders_adding_to_one_position():
    result = replay(
        {"index": 1, "quantity": "50.00", "yes_price": "0.5000", "order_id": "O1"},
        {"index": 2, "quantity": "50.00", "yes_price": "0.6000", "order_id": "O2"},
        {"index": 3, "quantity": "100.00", "yes_price": "0.7000", "order_id": "O3"},
    )
    episodes = ledger(result).episodes
    assert len(episodes) == 1, "one position, three orders"
    assert episodes[0].order_count == 3
    assert ledger(result).position == Decimal("200.00")
    assert episodes[0].average_entry_price == Decimal("0.625")


def test_4_partial_fills_of_one_order_form_one_position():
    result = replay(
        {"index": 1, "quantity": "40.00", "yes_price": "0.5700", "order_id": "O1"},
        {"index": 2, "quantity": "65.00", "yes_price": "0.5800", "order_id": "O1"},
        {"index": 3, "quantity": "71.00", "yes_price": "0.5900", "order_id": "O1"},
    )
    assert len(ledger(result).episodes) == 1
    assert ledger(result).position == Decimal("176.00")
    assert result.order_stats.partial_orders == 1


def test_5_partial_reduction():
    result = replay(
        {"index": 1, "quantity": "100.00", "yes_price": "0.6000"},
        {"index": 2, "quantity": "40.00", "yes_price": "0.7000", "action": "sell"},
    )
    assert kinds(result) == ["open", "reduce"]
    episode = ledger(result).episodes[0]
    assert ledger(result).position == Decimal("60.00")
    assert episode.total_closed_quantity == Decimal("40.00")
    assert episode.remaining_quantity == Decimal("60.00")
    assert episode.realized_pnl == Decimal("4.00")  # (0.70-0.60)*40
    assert episode.is_open is True
    # A reduction does not disturb the entry basis of what is still held.
    assert episode.average_entry_price == Decimal("0.6000")


def test_6_full_close():
    result = replay(
        {"index": 1, "quantity": "100.00", "yes_price": "0.6000"},
        {"index": 2, "quantity": "100.00", "yes_price": "0.7000", "action": "sell"},
    )
    assert kinds(result) == ["open", "close"]
    episode = ledger(result).episodes[0]
    assert ledger(result).position == Decimal(0)
    assert episode.is_open is False
    assert episode.remaining_quantity == Decimal(0)
    assert episode.realized_pnl == Decimal("10.00")


def test_7_reopening_after_close_is_a_second_episode():
    result = replay(
        {"index": 1, "quantity": "100.00", "yes_price": "0.6000"},
        {"index": 2, "quantity": "100.00", "yes_price": "0.7000", "action": "sell"},
        {"index": 3, "quantity": "50.00", "yes_price": "0.4000"},
    )
    assert kinds(result) == ["open", "close", "open"]
    episodes = ledger(result).episodes
    assert len(episodes) == 2
    assert episodes[0].is_open is False and episodes[1].is_open is True
    assert episodes[0].source_id != episodes[1].source_id
    assert episodes[1].average_entry_price == Decimal("0.4000")


def test_8_reversing_exposure_crosses_zero_into_the_other_side():
    """Selling more YES than held is long NO on Kalshi's signed axis."""
    result = replay(
        {"index": 1, "quantity": "100.00", "yes_price": "0.6000"},
        {"index": 2, "quantity": "150.00", "yes_price": "0.7000", "action": "sell"},
    )
    assert kinds(result) == ["open", "reverse"]
    assert ledger(result).position == Decimal("-50.00")
    outgoing, incoming = ledger(result).episodes
    assert outgoing.direction is Direction.LONG_YES and outgoing.is_open is False
    assert outgoing.realized_pnl == Decimal("10.00")  # (0.70-0.60)*100
    assert incoming.direction is Direction.LONG_NO and incoming.is_open is True
    assert incoming.remaining_quantity == Decimal("50.00")
    assert incoming.average_entry_price == Decimal("0.7000")


def test_9_interleaved_buys_and_sells():
    result = replay(
        {"index": 1, "quantity": "100.00", "yes_price": "0.5000"},
        {"index": 2, "quantity": "30.00", "yes_price": "0.6000", "action": "sell"},
        {"index": 3, "quantity": "50.00", "yes_price": "0.5500"},
        {"index": 4, "quantity": "20.00", "yes_price": "0.6500", "action": "sell"},
    )
    assert kinds(result) == ["open", "reduce", "increase", "reduce"]
    assert ledger(result).position == Decimal("100.00")
    assert len(ledger(result).episodes) == 1


def test_10_fills_from_more_than_one_page_replay_as_one_stream():
    page_one = [{"index": 3, "quantity": "20.00", "yes_price": "0.6000", "minute": 3}]
    page_two = [
        {"index": 1, "quantity": "50.00", "yes_price": "0.5000", "minute": 1},
        {"index": 2, "quantity": "30.00", "yes_price": "0.5500", "minute": 2},
    ]
    result = replay(*(page_one + page_two))
    assert kinds(result) == ["open", "increase", "increase"]
    assert ledger(result).position == Decimal("100.00")


# ------------------------------------------------------------ NO-side flows

def test_no_side_open_and_close_realizes_correctly():
    result = replay(
        {"index": 1, "quantity": "30.00", "side": "no", "no_price": "0.4000"},
        {"index": 2, "quantity": "30.00", "side": "no", "no_price": "0.3000",
         "action": "sell"},
    )
    assert kinds(result) == ["open", "close"]
    episode = ledger(result).episodes[0]
    assert episode.direction is Direction.LONG_NO
    # Bought NO at $0.40, sold at $0.30: a $0.10 loss on 30 contracts.
    assert episode.realized_pnl == Decimal("-3.00")


def test_buying_no_reduces_a_long_yes_position():
    """Buy NO is the complement of sell YES, so it reduces YES inventory."""
    result = replay(
        {"index": 1, "quantity": "100.00", "yes_price": "0.6000"},
        {"index": 2, "quantity": "40.00", "side": "no", "no_price": "0.3000"},
    )
    assert kinds(result) == ["open", "reduce"]
    assert ledger(result).position == Decimal("60.00")
    # Exit YES-equivalent price is 1 - 0.30 = 0.70.
    assert ledger(result).episodes[0].realized_pnl == Decimal("4.00")


# ---------------------------------------------------------------- economics

def test_missing_price_marks_cost_basis_incomplete_rather_than_estimating():
    raw = make_accounting_fill(index=1, quantity="100.00")
    del raw["yes_price_dollars"]
    second = make_accounting_fill(index=2, quantity="50.00", yes_price="0.7000",
                                  action="sell")
    result = AccountingEngine().replay(
        [normalize_fill(raw), normalize_fill(second)], COMPLETE
    )
    episode = ledger(result).episodes[0]
    assert episode.cost_basis_complete is False
    assert episode.average_entry_price is None


def test_fees_accumulate_only_when_every_fill_reports_one():
    result = replay(
        {"index": 1, "quantity": "10.00", "fee": "0.0175"},
        {"index": 2, "quantity": "10.00", "fee": "0.0100"},
    )
    episode = ledger(result).episodes[0]
    assert episode.fee_complete is True
    assert episode.fees_paid == Decimal("0.0275")


def test_a_single_missing_fee_marks_the_episode_incomplete():
    result = replay(
        {"index": 1, "quantity": "10.00", "fee": "0.0175"},
        {"index": 2, "quantity": "10.00"},
    )
    assert ledger(result).episodes[0].fee_complete is False


def test_reversal_charges_the_execution_fee_once():
    result = replay(
        {"index": 1, "quantity": "100.00", "yes_price": "0.6000", "fee": "0.1000"},
        {"index": 2, "quantity": "150.00", "yes_price": "0.7000", "action": "sell",
         "fee": "0.2000"},
    )
    outgoing, incoming = ledger(result).episodes
    assert outgoing.fees_paid == Decimal("0.3000")
    assert incoming.fees_paid == Decimal("0")


def test_positions_on_different_markets_are_independent():
    result = replay(
        {"index": 1, "quantity": "10.00", "ticker": "KXSYNTH-A-1"},
        {"index": 2, "quantity": "20.00", "ticker": "KXSYNTH-B-1"},
    )
    assert result.ledgers["KXSYNTH-A-1"].position == Decimal("10.00")
    assert result.ledgers["KXSYNTH-B-1"].position == Decimal("20.00")
