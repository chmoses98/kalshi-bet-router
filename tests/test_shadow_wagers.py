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
    """A sport with no destination profile is refused, never defaulted.

    Tennis has no profile: it is known to settle on a scalar this contract
    cannot express. (NFL was the other example until its activation on
    2026-09-24.)

    `SPORTS_WITH_AN_IMPORTER` is derived from the profiles rather than restated
    here, so this refusal and what production actually routes cannot drift
    apart in the direction where the shadow path refuses a sport the scheduled
    job is already delivering."""
    for sport in (Sport.TENNIS,):
        _, refusal = build_shadow_wager(settled_episode(), classified(sport), context())
        assert refusal is WagerRefusal.NO_DESTINATION_IMPORTER


def test_a_sport_with_a_profile_is_not_refused_for_want_of_a_destination():
    for sport in (Sport.MLB, Sport.CFB):
        _, refusal = build_shadow_wager(settled_episode(), classified(sport), context())
        assert refusal is not WagerRefusal.NO_DESTINATION_IMPORTER


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


# --------------------------------- spending the metadata budget where it counts

def test_routable_markets_are_classified_before_unroutable_ones():
    """A bounded sweep must look at the markets that could actually route.

    The first live shadow run spent its whole budget on an alphabetical prefix
    and never classified 549 of the 747 routable episodes -- so it could not say
    whether the zero wagers were a classification problem or a sampling one.
    """
    from kalshi_router.audit import _classification_order

    class FakeEpisode:
        def __init__(self, ticker, importable):
            self.ticker = ticker
            self.is_importable = importable

    class FakeReplay:
        episodes = [
            FakeEpisode("AAA", False),
            FakeEpisode("MMM", True),
            FakeEpisode("ZZZ", True),
        ]

    order = _classification_order(["AAA", "MMM", "ZZZ"], FakeReplay())
    assert order == ["MMM", "ZZZ", "AAA"]


def test_the_order_is_deterministic_within_each_group():
    from kalshi_router.audit import _classification_order

    class FakeEpisode:
        def __init__(self, ticker, importable):
            self.ticker = ticker
            self.is_importable = importable

    class FakeReplay:
        episodes = [FakeEpisode("BBB", True), FakeEpisode("AAA", True)]

    tickers = ["AAA", "BBB", "CCC"]
    first = _classification_order(tickers, FakeReplay())
    assert first == _classification_order(tickers, FakeReplay())
    assert first == ["AAA", "BBB", "CCC"]


def test_no_routable_market_leaves_the_order_untouched():
    from kalshi_router.audit import _classification_order

    class FakeReplay:
        episodes = []

    assert _classification_order(["B", "A"], FakeReplay()) == ["B", "A"]
    assert _classification_order(["B", "A"], None) == ["B", "A"]


def test_reordering_never_changes_how_many_markets_are_classified():
    from kalshi_router.audit import _classification_order

    class FakeEpisode:
        def __init__(self, ticker, importable):
            self.ticker = ticker
            self.is_importable = importable

    class FakeReplay:
        episodes = [FakeEpisode("C", True)]

    tickers = ["A", "B", "C", "D"]
    assert sorted(_classification_order(tickers, FakeReplay())) == sorted(tickers)


# ------------------------- what ARE the markets this account could route? ----

def test_routable_episodes_are_counted_by_what_they_classified_as():
    """A refusal reason says why; it does not say what the market WAS.

    "296 unresolved" reads as a classifier defect. It may instead be an account
    that trades markets this router is right to refuse -- and the two call for
    opposite responses, so the counts are kept apart.
    """
    from kalshi_router.accounting import AccountingEngine

    result = AccountingEngine().replay(
        [fill(fee="0.0100")], COMPLETE, settlements=[settlement()]
    )
    _, diagnostics = build_shadow_wagers(
        result.episodes, {TICKER: classified(Sport.UNRESOLVED)}, {TICKER: context()}
    )
    assert diagnostics.routable_classified_unresolved == 1
    assert diagnostics.routable_classified_mlb == 0
    # And it is still refused -- classifying it does not route it.
    assert diagnostics.refused_sport_unresolved == 1


def test_an_unroutable_episode_is_not_counted_among_the_routable_ones():
    from kalshi_router.accounting import AccountingEngine

    # No settlement and no exchange view: the position story is unearned, so it
    # is not routable and says nothing about what this account could route.
    result = AccountingEngine().replay([fill()], COMPLETE)
    _, diagnostics = build_shadow_wagers(
        result.episodes, {TICKER: classified()}, {TICKER: context()}
    )
    assert diagnostics.routable_classified_mlb == 0
    assert diagnostics.routable_not_classified == 0
    assert diagnostics.refused_identity_not_importable == 1


def test_a_routable_market_outside_the_classification_bound_is_counted_as_such():
    from kalshi_router.accounting import AccountingEngine

    result = AccountingEngine().replay(
        [fill(fee="0.0100")], COMPLETE, settlements=[settlement()]
    )
    _, diagnostics = build_shadow_wagers(result.episodes, {}, {})
    assert diagnostics.routable_not_classified == 1
    assert diagnostics.refused_market_not_classified == 1


def test_the_routable_breakdown_accounts_for_every_routable_episode():
    from kalshi_router.accounting import AccountingEngine

    result = AccountingEngine().replay(
        [fill(fee="0.0100")], COMPLETE, settlements=[settlement()]
    )
    _, d = build_shadow_wagers(
        result.episodes, {TICKER: classified()}, {TICKER: context()}
    )
    routable = sum(1 for e in result.episodes if e.is_importable)
    total = (
        d.routable_classified_mlb + d.routable_classified_nfl
        + d.routable_classified_cfb + d.routable_classified_tennis
        + d.routable_classified_other + d.routable_classified_unresolved
        + d.routable_not_classified
    )
    assert total == routable == 1


# ------------------------------------ why the classifier could not tell ------

def unresolved_with(reason):
    from kalshi_router.classify import Classification

    return Classification(
        sport=Sport.UNRESOLVED,
        reason="test",
        market_ticker=TICKER,
        unresolved_reason=reason,
    )


def routable_result():
    from kalshi_router.accounting import AccountingEngine

    return AccountingEngine().replay(
        [fill(fee="0.0100")], COMPLETE, settlements=[settlement()]
    )


def test_each_unresolved_reason_is_counted_separately():
    """"Unresolved" is not a diagnosis.

    Absent metadata, malformed metadata and present-but-unrecognised metadata
    are three different repairs, and one number cannot tell them apart.
    """
    from kalshi_router.classify import UnresolvedReason

    cases = {
        UnresolvedReason.METADATA_LOOKUP_FAILED: "unresolved_metadata_lookup_failed",
        UnresolvedReason.COMPETITION_ABSENT: "unresolved_competition_absent",
        UnresolvedReason.MALFORMED_EVENT_METADATA:
            "unresolved_malformed_event_metadata",
        UnresolvedReason.INSUFFICIENT: "unresolved_insufficient",
    }
    for reason, counter in cases.items():
        _, d = build_shadow_wagers(
            routable_result().episodes,
            {TICKER: unresolved_with(reason)},
            {TICKER: context()},
        )
        assert getattr(d, counter) == 1, reason
        assert d.routable_classified_unresolved == 1


def test_an_unresolved_market_with_no_recorded_reason_is_still_counted():
    # Fail-closed on the diagnostic itself: a missing reason must not make the
    # breakdown silently under-count and look tidier than the total.
    _, d = build_shadow_wagers(
        routable_result().episodes,
        {TICKER: unresolved_with(None)},
        {TICKER: context()},
    )
    assert d.unresolved_reason_not_recorded == 1
    assert d.routable_classified_unresolved == 1


def test_the_reason_breakdown_sums_to_the_unresolved_total():
    from kalshi_router.classify import UnresolvedReason

    _, d = build_shadow_wagers(
        routable_result().episodes,
        {TICKER: unresolved_with(UnresolvedReason.COMPETITION_ABSENT)},
        {TICKER: context()},
    )
    total = sum(
        value for name, value in vars(d).items() if name.startswith("unresolved_")
    )
    assert total == d.routable_classified_unresolved == 1


def test_a_resolved_sport_contributes_no_unresolved_reason():
    _, d = build_shadow_wagers(
        routable_result().episodes, {TICKER: classified()}, {TICKER: context()}
    )
    assert d.routable_classified_mlb == 1
    assert all(
        value == 0 for name, value in vars(d).items() if name.startswith("unresolved_")
    )


# ------------- what a fall-through WOULD have decided (measurement only) -----

def test_a_terminal_refusal_records_which_level_would_have_decided():
    """The verdict does not change. The cost of the terminal rule is counted.

    L4 is Kalshi's OWN series metadata and L5 is this project's registry, so
    they are counted apart: falling back on the first would be consulting the
    exchange, falling back on the second would be overruling it.
    """
    from kalshi_router.classify import (
        EvidenceLevel,
        MarketContext,
        UnresolvedReason,
        classify_market,
    )
    from kalshi_router.taxonomy import parse_filters_by_sport

    # One competition string claimed by two sports -- Kalshi's own taxonomy is
    # ambiguous about the NAME, while the series says plainly what this market is.
    from .synthetic import make_taxonomy

    taxonomy = parse_filters_by_sport(make_taxonomy({
        "Baseball": ["Championship"],
        "Football": ["Championship"],
    }))
    context = MarketContext(
        market_ticker="KXNFLGAME-SYNTH01-AAA",
        market={"event_ticker": "KXNFLGAME-SYNTH01", "series_ticker": "KXNFLGAME"},
        event={"event_ticker": "KXNFLGAME-SYNTH01", "series_ticker": "KXNFLGAME"},
        event_metadata={"competition": "Championship", "competition_scope": "Game"},
        series={"category": "Sports", "tags": ["Football"]},
    )
    result = classify_market(context, taxonomy=taxonomy)

    # Unchanged: still refused, still for the same reason.
    assert result.sport is Sport.UNRESOLVED
    assert result.unresolved_reason is UnresolvedReason.COMPETITION_AMBIGUOUS

    # And now we know what it cost -- and the answer argues AGAINST relaxing the
    # rule rather than for it. A series tagged "Football" is an ambiguous family
    # (pro or college), so Kalshi's own L4 metadata does NOT decide; only this
    # project's own L5 registry does, by reading the ticker prefix. Falling
    # through here would be our registry overruling Kalshi's ambiguity, which is
    # exactly what the terminal rule exists to prevent.
    assert result.terminal_rescuable_by is EvidenceLevel.L5_SERIES_REGISTRY


def test_a_terminal_refusal_with_no_lower_evidence_records_nothing():
    from kalshi_router.classify import MarketContext, classify_market
    from kalshi_router.taxonomy import parse_filters_by_sport

    from .synthetic import make_taxonomy

    taxonomy = parse_filters_by_sport(make_taxonomy({
        "Baseball": ["Championship"],
        "Football": ["Championship"],
    }))
    context = MarketContext(
        market_ticker="KXUNKNOWN-SYNTH01-AAA",
        market={"event_ticker": "KXUNKNOWN-SYNTH01"},
        event={"event_ticker": "KXUNKNOWN-SYNTH01"},
        event_metadata={"competition": "Championship"},
    )
    result = classify_market(context, taxonomy=taxonomy)
    assert result.sport is Sport.UNRESOLVED
    assert result.terminal_rescuable_by is None


def test_a_resolved_market_records_no_rescue_level():
    # The field is only meaningful for a terminal refusal.
    from kalshi_router.classify import MarketContext, classify_market

    context = MarketContext(
        market_ticker="KXMLBGAME-SYNTH01-AAA",
        market={"event_ticker": "KXMLBGAME-SYNTH01"},
        event={"event_ticker": "KXMLBGAME-SYNTH01"},
        event_metadata={"competition": "Pro Baseball"},
    )
    result = classify_market(context)
    assert result.sport is Sport.MLB
    assert result.terminal_rescuable_by is None


def test_the_rescue_axis_does_not_disturb_the_reason_breakdown():
    """Two axes over the same markets; neither may contaminate the other."""
    from kalshi_router.classify import UnresolvedReason

    _, d = build_shadow_wagers(
        routable_result().episodes,
        {TICKER: unresolved_with(UnresolvedReason.COMPETITION_AMBIGUOUS)},
        {TICKER: context()},
    )
    reasons = sum(v for k, v in vars(d).items() if k.startswith("unresolved_"))
    rescues = sum(v for k, v in vars(d).items() if k.startswith("rescue_"))
    assert reasons == d.routable_classified_unresolved == 1
    assert rescues == d.routable_classified_unresolved == 1
