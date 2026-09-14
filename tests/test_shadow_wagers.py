"""Shadow wagers: the rows a router would send, built and never sent.

Phase G. The interesting output is not the wagers -- it is the refusals. Every
one names a fact that could not be established, so it says what routing would
cost in accuracy today, before it can cost it.
"""

from __future__ import annotations

from decimal import Decimal

from kalshi_router.accounting import AccountingEngine, HistoryCompleteness
from kalshi_router.classify import Classification, MarketContext
from kalshi_router.models import normalize_fill, normalize_settlement
from kalshi_router.sports import Sport
from kalshi_router.wager import (
    GameDateSource,
    WagerRefusal,
    build_shadow_wager,
    build_shadow_wagers,
    resolve_game_date,
    row_is_valid,
)

from .synthetic import make_accounting_fill

COMPLETE = HistoryCompleteness.COMPLETE
TICKER = "KXMLBGAME-26AUG03SFLAD-SF"
EVENT = "KXMLBGAME-26AUG03SFLAD"


def fill(index=1, quantity="10.00", yes_price="0.5600", **kw):
    return normalize_fill(
        make_accounting_fill(index, quantity, ticker=TICKER, yes_price=yes_price, **kw)
    )


def settlement(revenue="1000", result="yes", yes_count="10.00", no_count="0.00"):
    return normalize_settlement({
        "ticker": TICKER,
        "settled_time": "2026-08-04T02:00:00Z",
        "market_result": result,
        "revenue": revenue,
        "value": "100",
        "fee_cost": "0.0000",
        "yes_count_fp": yes_count,
        "no_count_fp": no_count,
    })


def context(event=None, market=None):
    return MarketContext(
        market_ticker=TICKER,
        market=market if market is not None else {"event_ticker": EVENT},
        event=event if event is not None else {"event_ticker": EVENT},
    )


def classified(sport=Sport.MLB):
    return Classification(sport=sport, reason="test", market_ticker=TICKER)


def settled_episode(**settlement_kw):
    result = AccountingEngine().replay(
        [fill(fee="0.0100")], COMPLETE, settlements=[settlement(**settlement_kw)]
    )
    return result.ledger_for(TICKER, 0).episodes[0]


# ------------------------------------------------------------- the game date

def test_an_explicit_event_date_field_wins():
    date, source = resolve_game_date(context(event={"game_date": "2026-08-03T00:00:00Z"}))
    assert date == "2026-08-03"
    assert source is GameDateSource.EVENT_FIELD


def test_the_event_ticker_date_segment_is_parsed_when_no_field_exists():
    date, source = resolve_game_date(context())
    assert date == "2026-08-03"
    assert source is GameDateSource.EVENT_TICKER


def test_a_utc_close_time_is_never_used_as_a_game_date():
    # A night game ending at 03:00 UTC belongs to the previous local date, so
    # deriving the date from a close timestamp would be wrong for most evening
    # games -- and wrong silently. There is no such fallback.
    ctx = MarketContext(
        market_ticker=TICKER,
        market={"close_time": "2026-08-04T03:15:00Z"},
        event={"close_time": "2026-08-04T03:15:00Z"},
    )
    date, source = resolve_game_date(ctx)
    assert date is None
    assert source is GameDateSource.NONE


def test_an_unparseable_event_ticker_yields_no_date():
    ctx = context(event={"event_ticker": "KXSOMETHING-NODATE"},
                  market={"event_ticker": "KXSOMETHING-NODATE"})
    assert resolve_game_date(ctx) == (None, GameDateSource.NONE)


def test_a_bogus_month_in_the_ticker_is_refused_not_guessed():
    ctx = context(event={"event_ticker": "KXMLBGAME-26XXX03SFLAD"},
                  market={"event_ticker": "KXMLBGAME-26XXX03SFLAD"})
    assert resolve_game_date(ctx)[0] is None


def test_no_context_at_all_yields_no_date():
    assert resolve_game_date(None) == (None, GameDateSource.NONE)


# ------------------------------------------------------------ building a row

def test_a_settled_reconciled_episode_becomes_a_routable_wager():
    wager, refusal = build_shadow_wager(settled_episode(), classified(), context())
    assert refusal is None
    assert wager is not None
    assert wager.sport is Sport.MLB
    assert wager.side == "YES"
    assert wager.game_date == "2026-08-03"
    assert wager.result == "WIN"
    assert wager.contracts == Decimal("10.00")
    assert wager.entry_price == Decimal("0.5600")
    # stake = contract cost + fees; nothing estimated.
    assert wager.stake == Decimal("0.5600") * Decimal("10.00") + Decimal("0.0100")
    assert wager.net_profit_loss == Decimal(10) - wager.stake


def test_the_row_carries_only_fields_the_caller_owns():
    wager, _ = build_shadow_wager(settled_episode(), classified(), context())
    row = wager.to_import_row()
    # These belong to the importer. A router emitting them would be inventing
    # values the destination produces.
    for owned_by_the_importer in ("betId", "validationStatus", "provenance", "createdAt"):
        assert owned_by_the_importer not in row
    assert row["sourceBetKey"] == wager.source_bet_key
    assert row["marketTicker"] == TICKER
    assert row["gameDate"] == "2026-08-03"
    assert row_is_valid(row)


def test_the_row_supplies_exactly_one_of_entry_price_and_entry_odds():
    wager, _ = build_shadow_wager(settled_episode(), classified(), context())
    row = wager.to_import_row()
    assert row["entryPrice"] is not None
    assert "entryOdds" not in row
    assert row_is_valid(row)


def test_a_row_missing_a_required_field_is_invalid():
    wager, _ = build_shadow_wager(settled_episode(), classified(), context())
    for field in ("gameDate", "stake", "sourceBetKey"):
        row = wager.to_import_row()
        row[field] = None
        assert not row_is_valid(row)


def test_a_row_with_both_prices_is_invalid():
    wager, _ = build_shadow_wager(settled_episode(), classified(), context())
    row = wager.to_import_row()
    row["entryOdds"] = 128
    assert not row_is_valid(row)


# --------------------------------------------------- the NO side is a complement

def test_a_long_no_episode_records_the_contract_price_not_the_yes_axis():
    # The ledger is denominated on the signed YES axis; the destination records
    # what was paid for the contract actually held. Emitting the axis
    # coordinate for a NO position would overstate or understate every one.
    result = AccountingEngine().replay(
        [fill(action="buy", side="no", yes_price="0.5600", fee="0.0100")],
        COMPLETE,
        settlements=[settlement(revenue="0", result="yes",
                                yes_count="0.00", no_count="10.00")],
    )
    episode = result.ledger_for(TICKER, 0).episodes[0]
    wager, refusal = build_shadow_wager(episode, classified(), context())
    assert refusal is None
    assert wager.side == "NO"
    assert wager.entry_price == Decimal("0.4400")      # 1.00 - 0.5600
    assert wager.result == "LOSS"                      # the exchange paid nothing


# --------------------------------------------------------------- the refusals

def test_an_unearned_episode_is_refused_for_its_identity_first():
    # No exchange view and no settlement: the position story is unearned, and
    # that is the thing that would have to be fixed first.
    result = AccountingEngine().replay([fill()], COMPLETE)
    episode = result.ledger_for(TICKER, 0).episodes[0]
    _, refusal = build_shadow_wager(episode, classified(), context())
    assert refusal is WagerRefusal.IDENTITY_NOT_IMPORTABLE


def test_an_open_reconciled_episode_is_refused_as_not_closed():
    result = AccountingEngine().replay(
        [fill()], COMPLETE, exchange_positions={TICKER: Decimal("10.00")}
    )
    episode = result.ledger_for(TICKER, 0).episodes[0]
    _, refusal = build_shadow_wager(episode, classified(), context())
    assert refusal is WagerRefusal.NOT_CLOSED


def test_an_unclassified_market_is_refused_as_not_classified():
    _, refusal = build_shadow_wager(settled_episode(), None, context())
    assert refusal is WagerRefusal.MARKET_NOT_CLASSIFIED


def test_an_unresolved_sport_is_refused_rather_than_guessed():
    _, refusal = build_shadow_wager(
        settled_episode(), classified(Sport.UNRESOLVED), context()
    )
    assert refusal is WagerRefusal.SPORT_UNRESOLVED


def test_a_sport_with_no_importer_is_refused():
    # Phase F read all four repositories: only MLB has an importer. The others
    # would need their wager contract designed first.
    for sport in (Sport.NFL, Sport.CFB, Sport.TENNIS):
        _, refusal = build_shadow_wager(settled_episode(), classified(sport), context())
        assert refusal is WagerRefusal.NO_DESTINATION_IMPORTER


def test_an_unestablished_game_date_is_refused():
    ctx = context(event={"event_ticker": "KXMLBGAME-NODATE"},
                  market={"event_ticker": "KXMLBGAME-NODATE"})
    _, refusal = build_shadow_wager(settled_episode(), classified(), ctx)
    assert refusal is WagerRefusal.GAME_DATE_NOT_ESTABLISHED


def test_a_non_binary_settlement_is_refused():
    # MLB's ledger has WIN/LOSS/PUSH/VOID and no partial. Tennis is known to
    # settle scalar, so this is a live hazard, not a hypothetical one.
    _, refusal = build_shadow_wager(
        settled_episode(result="scalar"), classified(), context()
    )
    assert refusal is WagerRefusal.NON_BINARY_SETTLEMENT


def test_an_incomplete_fee_is_refused_rather_than_reconstructed():
    result = AccountingEngine().replay(
        [fill(fee=None)], COMPLETE, settlements=[settlement()]
    )
    episode = result.ledger_for(TICKER, 0).episodes[0]
    _, refusal = build_shadow_wager(episode, classified(), context())
    assert refusal is WagerRefusal.FEES_INCOMPLETE


def test_an_episode_closed_by_trading_has_no_settlement_economics():
    # Closed by fills, so authoritative -- but there is no exchange-stated
    # payout, and inventing a result would be a guess.
    result = AccountingEngine().replay(
        [fill(1, "10.00", fee="0.0100"),
         fill(2, "10.00", action="sell", side="yes", fee="0.0100")],
        COMPLETE,
    )
    episode = result.ledger_for(TICKER, 0).episodes[0]
    assert not episode.is_open
    _, refusal = build_shadow_wager(episode, classified(), context())
    assert refusal is WagerRefusal.NO_SETTLEMENT_ECONOMICS


# ------------------------------------------------------------- the aggregate

def test_every_episode_is_either_built_or_refused_exactly_once():
    result = AccountingEngine().replay(
        [fill(fee="0.0100")], COMPLETE, settlements=[settlement()]
    )
    _, diagnostics = build_shadow_wagers(
        result.episodes, {TICKER: classified()}, {TICKER: context()}
    )
    assert diagnostics.episodes_considered == 1
    assert diagnostics.wagers_built + diagnostics.refusals_total == 1


def test_the_diagnostics_hold_counts_only():
    result = AccountingEngine().replay(
        [fill(fee="0.0100")], COMPLETE, settlements=[settlement()]
    )
    _, diagnostics = build_shadow_wagers(
        result.episodes, {TICKER: classified()}, {TICKER: context()}
    )
    for name, value in vars(diagnostics).items():
        assert isinstance(value, int), f"{name} is not a count"


def test_the_rendered_output_names_no_ticker_date_or_amount():
    result = AccountingEngine().replay(
        [fill(fee="0.0100")], COMPLETE, settlements=[settlement()]
    )
    _, diagnostics = build_shadow_wagers(
        result.episodes, {TICKER: classified()}, {TICKER: context()}
    )
    text = diagnostics.render()
    for token in (TICKER, EVENT, "2026-08-03", "0.56", "$"):
        assert token not in text


def test_the_game_date_source_split_is_reported():
    result = AccountingEngine().replay(
        [fill(fee="0.0100")], COMPLETE, settlements=[settlement()]
    )
    _, diagnostics = build_shadow_wagers(
        result.episodes, {TICKER: classified()}, {TICKER: context()}
    )
    assert diagnostics.wagers_built == 1
    # Knowing whether the date came from a field or from a ticker convention is
    # what says whether that convention is load-bearing.
    assert diagnostics.game_date_from_event_ticker == 1
    assert diagnostics.game_date_from_event_field == 0
