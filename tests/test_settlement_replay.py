"""Settlement replay: the exchange closes the position, with no fill.

The live audit made the gap concrete -- 155 episodes open, 0 closed, in a window
of games that had all finished days earlier. Those positions did not stay open;
they settled, and a fills-only replay cannot see it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kalshi_router.accounting import AccountingEngine, HistoryCompleteness, TransitionKind
from kalshi_router.accounting.position import PositionAuthority
from kalshi_router.errors import SchemaError
from kalshi_router.models import normalize_fill, normalize_settlement

from .synthetic import SYNTH_TICKER, make_accounting_fill

COMPLETE = HistoryCompleteness.COMPLETE


def settlement_row(**overrides):
    """A settlement in the shape 755 live rows actually have."""
    row = {
        "ticker": SYNTH_TICKER,
        "event_ticker": "KXSYNTH-EVT",
        "settled_time": "2026-09-02T00:00:00Z",
        "market_result": "yes",
        "revenue": "1000",                 # CENTS -> $10.00
        "value": "100",                    # CENTS -> $1.00 per YES contract
        "fee_cost": "0.0700",              # dollars
        "yes_count_fp": "10.00",
        "no_count_fp": "0.00",
        "yes_total_cost_dollars": "5.60",
        "no_total_cost_dollars": "0.00",
    }
    row.update(overrides)
    return row


def replay(fills, settlements=(), completeness=COMPLETE):
    return AccountingEngine().replay(
        [normalize_fill(f) for f in fills],
        completeness,
        settlements=[normalize_settlement(s) for s in settlements],
    )


def ledger(result):
    return result.ledger_for(SYNTH_TICKER, 0)


# ------------------------------------------------------- units are not dollars

def test_revenue_and_value_are_read_as_cents():
    s = normalize_settlement(settlement_row())
    assert s.revenue_dollars == Decimal(10)          # not 1000
    assert s.market_value_dollars == Decimal(1)      # not 100


def test_fee_cost_is_read_as_dollars_in_the_same_row():
    """Units are mixed within one settlement; the conversion is not uniform."""
    assert normalize_settlement(settlement_row()).fee_dollars == Decimal("0.0700")


def test_per_leg_cost_is_read_as_dollars():
    s = normalize_settlement(settlement_row())
    assert s.yes_cost_dollars == Decimal("5.60")


# -------------------------------------------------------------- the close path

def test_a_settlement_closes_a_position_no_fill_could_close():
    result = replay(
        [make_accounting_fill(index=1, quantity="10.00", yes_price="0.5600")],
        [settlement_row()],
    )
    assert result.settlements_applied == 1
    assert ledger(result).position == Decimal(0)
    kinds = [t.kind for t in result.transitions]
    assert TransitionKind.SETTLE in kinds


def test_the_episode_closes_and_stops_counting_as_open():
    result = replay(
        [make_accounting_fill(index=1, quantity="10.00", yes_price="0.5600")],
        [settlement_row()],
    )
    episode = ledger(result).episodes[0]
    assert episode.remaining_quantity == Decimal(0)
    assert episode.total_closed_quantity == Decimal("10.00")
    assert not episode.is_open


def test_realized_pnl_uses_the_exchange_payout_not_a_guessed_close_price():
    """Bought 10 YES at 0.56 (=$5.60), settled for $10.00: +$4.40."""
    result = replay(
        [make_accounting_fill(index=1, quantity="10.00", yes_price="0.5600")],
        [settlement_row()],
    )
    assert ledger(result).episodes[0].realized_pnl == Decimal("4.40")


def test_a_losing_settlement_realizes_the_whole_cost_basis():
    result = replay(
        [make_accounting_fill(index=1, quantity="10.00", yes_price="0.5600")],
        [settlement_row(market_result="no", revenue="0", value="0",
                        yes_count_fp="10.00")],
    )
    assert ledger(result).episodes[0].realized_pnl == Decimal("-5.60")


def test_a_long_no_position_settles_against_its_own_leg_cost():
    """Bought 10 NO at 0.44 (=$4.40); market settled NO, paying $10.00."""
    result = replay(
        [make_accounting_fill(index=1, quantity="10.00", side="no",
                              no_price="0.4400", action="buy")],
        [settlement_row(market_result="no", revenue="1000", value="0",
                        yes_count_fp="0.00", no_count_fp="10.00")],
    )
    assert ledger(result).position == Decimal(0)
    assert ledger(result).episodes[0].realized_pnl == Decimal("5.60")


def test_the_settlement_fee_is_the_exchange_fee_never_reconstructed():
    result = replay(
        [make_accounting_fill(index=1, quantity="10.00", yes_price="0.5600",
                              fee="0.0100")],
        [settlement_row()],
    )
    episode = ledger(result).episodes[0]
    assert episode.fees_paid == Decimal("0.0800")   # 0.01 entry + 0.07 settlement
    assert episode.fee_complete


# ------------------------------------------------------------- the refusals

def test_a_size_disagreement_is_refused_rather_than_forced():
    """Settled 25 contracts but only 10 are in the window: history is short.

    Applying it anyway would credit a $25 payout against a 10-contract cost
    basis and overstate profit, silently.
    """
    result = replay(
        [make_accounting_fill(index=1, quantity="10.00", yes_price="0.5600")],
        [settlement_row(yes_count_fp="25.00", revenue="2500")],
    )
    assert result.settlements_refused_unreconciled == 1
    assert result.settlements_applied == 0
    assert ledger(result).position == Decimal("10.00")   # left open, not zeroed


def test_a_settlement_for_an_unseen_market_is_counted_not_an_error():
    """Expected constantly: the settlement walk is unbounded, fills are not."""
    result = replay(
        [make_accounting_fill(index=1, quantity="10.00", yes_price="0.5600")],
        [settlement_row(ticker="KXSOMETHING-ELSE")],
    )
    assert result.settlements_without_a_position == 1
    assert result.settlements_applied == 0


def test_a_ticker_held_in_two_subaccounts_refuses_the_settlement():
    """Settlements carry NO subaccount field, so attribution is impossible.

    Guessing would merge two independent positions into one.
    """
    result = replay(
        [make_accounting_fill(index=1, quantity="10.00", yes_price="0.5600",
                              subaccount=0, fill_id="F1"),
         make_accounting_fill(index=2, quantity="10.00", yes_price="0.5600",
                              subaccount=1, fill_id="F2")],
        [settlement_row()],
    )
    assert result.settlements_refused_ambiguous_subaccount == 1
    assert result.settlements_applied == 0


def test_a_settlement_with_no_quantity_still_applies():
    """Only a STATED size that disagrees is a refusal; an absent one is not."""
    row = settlement_row()
    del row["yes_count_fp"], row["no_count_fp"]
    result = replay(
        [make_accounting_fill(index=1, quantity="10.00", yes_price="0.5600")], [row]
    )
    assert result.settlements_applied == 1


# ------------------------------------------------------ fail-closed parsing

@pytest.mark.parametrize("field", ["ticker", "settled_time", "market_result", "revenue"])
def test_a_settlement_missing_a_required_field_fails_closed(field):
    row = settlement_row()
    del row[field]
    with pytest.raises(SchemaError):
        normalize_settlement(row)


def test_a_missing_revenue_is_never_reconstructed_from_result_and_count():
    row = settlement_row()
    del row["revenue"]
    with pytest.raises(SchemaError, match="never reconstructed"):
        normalize_settlement(row)


def test_settlements_are_applied_in_settled_time_order():
    result = replay(
        [make_accounting_fill(index=1, quantity="10.00", yes_price="0.5600")],
        [settlement_row(settled_time="2026-09-03T00:00:00Z"),
         settlement_row(ticker="KXOTHER", settled_time="2026-09-01T00:00:00Z")],
    )
    assert result.settlements_applied == 1
    assert result.settlements_without_a_position == 1


# ============ a settlement proves the END, never the BEGINNING ==============

def test_a_settled_episode_under_a_bounded_window_stays_provisional():
    """Closing is not the same as being importable, and conflating them is the
    defect this pins.

    A settlement proves where an episode ENDED. Identity is keyed on where it
    BEGAN, and a bounded window still cannot prove that -- back-filling older
    fills could merge this episode into an older one and change its opening
    fill. An importer that treated "settled" as "safe to import" would create a
    wager whose identity later moves underneath it.
    """
    from kalshi_router.accounting.identity import ProvisionalIdentity

    result = AccountingEngine().replay(
        [normalize_fill(make_accounting_fill(index=1, quantity="10.00",
                                             yes_price="0.5600"))],
        HistoryCompleteness.BOUNDED_WINDOW,
        settlements=[normalize_settlement(settlement_row())],
    )
    episode = result.ledger_for(SYNTH_TICKER, 0).episodes[0]
    assert not episode.is_open                       # the settlement closed it
    assert isinstance(episode.identity, ProvisionalIdentity)   # still not importable


def test_the_same_episode_is_importable_once_history_is_complete():
    from kalshi_router.accounting.identity import StableIdentity

    result = AccountingEngine().replay(
        [normalize_fill(make_accounting_fill(index=1, quantity="10.00",
                                             yes_price="0.5600"))],
        HistoryCompleteness.COMPLETE,
        settlements=[normalize_settlement(settlement_row())],
    )
    episode = result.ledger_for(SYNTH_TICKER, 0).episodes[0]
    assert not episode.is_open
    # A settlement is an authoritative closure, so this episode's position story
    # is earned without any exchange position view at all.
    assert episode.authority is PositionAuthority.EXPLAINED_SETTLED
    assert isinstance(episode.identity, StableIdentity)


def test_replaying_settlements_does_not_upgrade_history_completeness():
    """The replay must not start claiming authority it has not earned."""
    result = AccountingEngine().replay(
        [normalize_fill(make_accounting_fill(index=1, quantity="10.00",
                                             yes_price="0.5600"))],
        HistoryCompleteness.BOUNDED_WINDOW,
        settlements=[normalize_settlement(settlement_row())],
    )
    assert result.settlements_applied == 1
    assert not result.claims_complete_position_state


def test_the_fee_counter_counts_settlements_as_well_as_fills():
    """A settlement carries its own fee, so it is a fee-bearing event too.

    The counter used to be named for fills. Once settlements started producing
    transitions it was reporting 352 against 200 fills -- a correct total under
    a wrong label, which is the kind of number that gets mistrusted or, worse,
    trusted for the wrong thing.
    """
    from kalshi_router.accounting.diagnostics import build_diagnostics

    result = replay(
        [make_accounting_fill(index=1, quantity="10.00", yes_price="0.5600",
                              fee="0.0100")],
        [settlement_row()],
    )
    diagnostics = build_diagnostics(result)
    assert diagnostics.events_with_fee_field == 2      # one fill + one settlement
    assert diagnostics.events_missing_fee_field == 0
    assert "fee-bearing events (fills + settlements): 2" in diagnostics.render()


def test_a_settlement_without_a_fee_is_counted_as_missing_not_zero():
    from kalshi_router.accounting.diagnostics import build_diagnostics

    row = settlement_row()
    del row["fee_cost"]
    result = replay(
        [make_accounting_fill(index=1, quantity="10.00", yes_price="0.5600",
                              fee="0.0100")],
        [row],
    )
    diagnostics = build_diagnostics(result)
    assert diagnostics.events_missing_fee_field == 1


# ============ scalar settlements: the case this account does not have =======
#
# All 755 live settlements are yes/no. Tennis verified 1,836 that settle
# `scalar`, strictly between 0 and 1. Code that is correct on every row an
# account has and wrong on the first row of a new sport is the failure shape
# this project keeps finding, so the scalar path is exercised deliberately
# rather than left to be discovered when tennis is enabled.

def test_a_scalar_settlement_replays_without_a_binary_assumption():
    """Nothing derives the payout from the RESULT, so scalar needs no new path.

    Realized P&L is the exchange's stated revenue against the episode's cost
    basis. A walkover paying $0.42 a contract is arithmetic, not a special case.
    """
    result = replay(
        [make_accounting_fill(index=1, quantity="10.00", yes_price="0.5600")],
        [settlement_row(market_result="scalar", revenue="420", value="42")],
    )
    assert result.settlements_applied == 1
    episode = ledger(result).episodes[0]
    assert not episode.is_open
    # paid $5.60, received $4.20 -> -1.40
    assert episode.realized_pnl == Decimal("-1.40")


def test_a_scalar_result_is_preserved_verbatim_not_coerced_to_yes_or_no():
    s = normalize_settlement(settlement_row(market_result="scalar", value="42"))
    assert s.market_result == "scalar"
    assert s.market_value_dollars == Decimal("0.42")


def test_an_unfamiliar_result_still_parses_rather_than_failing_closed():
    """A result we have never seen is not a reason to refuse a stated payout.

    The payout comes from `revenue`, which does not depend on recognising the
    result, so refusing here would discard exchange truth over a vocabulary gap.
    """
    s = normalize_settlement(settlement_row(market_result="void", revenue="0"))
    assert s.market_result == "void"
    assert s.revenue_dollars == Decimal(0)


# ============ a complete fill history does not earn authority ===============
#
# The first exhaustive run walked both fill routes to exhaustion -- 883 live and
# 1050 archived, 0 rejected -- and legitimately reported HISTORY IS COMPLETE.
# It then claimed authority over position state while holding 943 markets the
# exchange does not report at all. The settlement route only reaches 755 of the
# 1698 traded markets, so the rest settled beyond its window and the replay has
# no event that could ever close them.

def test_a_contradicted_position_state_is_not_claimed_as_authoritative():
    """One value, gated. Not a true object beside a false presentation.

    This test used to assert exactly that contradiction -- the object reporting
    True while the rendered line said False, under the same name. A machine
    consumer reading as_dict() got the wrong answer, and only a human reading
    the report got the right one.
    """
    from kalshi_router.accounting.diagnostics import build_diagnostics

    result = AccountingEngine().replay(
        [normalize_fill(make_accounting_fill(index=1, quantity="10.00",
                                             yes_price="0.5600"))],
        HistoryCompleteness.COMPLETE,
        # The exchange reports nothing on this market and no settlement explains
        # it: the replay's open position is contradicted.
        exchange_positions={},
    )
    diagnostics = build_diagnostics(result)

    assert diagnostics.fill_history_complete is True
    assert diagnostics.claims_complete_position_state is False
    assert diagnostics.as_dict()["claims_complete_position_state"] is False
    assert "position state claimed as authoritative: False" in diagnostics.render()
    assert "CONTRADICTED BY THE EXCHANGE" in diagnostics.render()


def test_an_uncontradicted_complete_history_still_claims_authority():
    from kalshi_router.accounting.diagnostics import build_diagnostics

    result = replay(
        [make_accounting_fill(index=1, quantity="10.00", yes_price="0.5600")],
        [settlement_row()],
        completeness=HistoryCompleteness.COMPLETE,
    )
    diagnostics = build_diagnostics(result)
    rendered = diagnostics.render()
    assert "position state claimed as authoritative: True" in rendered
    assert "CONTRADICTED" not in rendered
