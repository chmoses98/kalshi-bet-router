"""Settlement coverage: the second completeness dimension.

Phase C.14 proved a complete FILL history does not earn authority over
positions.  This is why.  A market settles without a fill, and the settlements
route does not reach as far back as the archive fill route does, so a market
bought, held and settled before that reach leaves a complete fill trail and no
settlement row.  The replay holds it open forever.

The distinction under test throughout: an open episode ABOVE the settlement
floor is genuinely open (the route had data there and said nothing), while one
BELOW the floor has an outcome the evidence cannot establish either way.  The
first is a defect to chase; the second is a limit to state.
"""

from __future__ import annotations

from decimal import Decimal

from kalshi_router.accounting import AccountingEngine, HistoryCompleteness
from kalshi_router.accounting.diagnostics import build_diagnostics
from kalshi_router.coverage import SettlementCoverage, build_settlement_coverage
from kalshi_router.models import normalize_fill, normalize_settlement
from kalshi_router.reconcile import probe_reconciliation
from kalshi_router.timeaxis import parse_rfc3339_seconds, seconds_to_days

from .synthetic import SYNTH_TICKER, make_accounting_fill

COMPLETE = HistoryCompleteness.COMPLETE
OLD_TICKER = "KXSYNTH-OLD"
RECENT_TICKER = "KXSYNTH-RECENT"


def settlement_row(**overrides):
    row = {
        "ticker": SYNTH_TICKER,
        "settled_time": "2026-06-01T00:00:00Z",
        "market_result": "yes",
        "revenue": "1000",
        "value": "100",
        "fee_cost": "0.0700",
        "yes_count_fp": "10.00",
        "no_count_fp": "0.00",
    }
    row.update(overrides)
    return row


def normalized(*rows):
    return [normalize_settlement(r) for r in rows]


def at(text):
    parsed = parse_rfc3339_seconds(text)
    assert parsed is not None
    return parsed


# --------------------------------------------------------------- the time axis

def test_fills_and_settlements_land_on_one_scale():
    # A settlement time and a fill time are compared against each other, so a
    # separate parser for each is a correctness risk, not a convenience.
    fill = normalize_fill(make_accounting_fill(1, "1", created_time="2026-06-01T00:00:00Z"))
    settlement = normalize_settlement(settlement_row(settled_time="2026-06-01T00:00:00Z"))
    from kalshi_router.accounting.ordering import parse_execution_time

    assert parse_execution_time(fill) == settlement.settled_at


def test_an_offsetless_timestamp_is_read_as_utc_not_local():
    assert parse_rfc3339_seconds("2026-06-01T00:00:00") == at("2026-06-01T00:00:00Z")


def test_an_unreadable_timestamp_is_none_never_a_default():
    for raw in ("", "   ", "yesterday", None, 17, "2026-13-01T00:00:00Z"):
        assert parse_rfc3339_seconds(raw) is None


def test_day_conversion_truncates_toward_zero():
    assert seconds_to_days(Decimal(86400 * 3 + 1)) == 3


# ------------------------------------------------------------ measuring reach

def test_coverage_records_the_earliest_and_latest_settlement():
    coverage = build_settlement_coverage(
        normalized(
            settlement_row(settled_time="2026-06-01T00:00:00Z"),
            settlement_row(settled_time="2026-03-01T00:00:00Z"),
            settlement_row(settled_time="2026-09-01T00:00:00Z"),
        ),
        exhausted=True,
    )
    assert coverage.rows == 3
    assert coverage.earliest_settled_at == at("2026-03-01T00:00:00Z")
    assert coverage.latest_settled_at == at("2026-09-01T00:00:00Z")
    assert coverage.floor == at("2026-03-01T00:00:00Z")
    assert coverage.span_days == 184


def test_a_truncated_walk_yields_no_floor():
    # The earliest row of a walk that stopped on a budget bounds nothing: the
    # rows it never fetched could be older than every row it did.
    coverage = build_settlement_coverage(
        normalized(settlement_row()), exhausted=True, truncated=True
    )
    assert coverage.earliest_settled_at is not None
    assert coverage.floor is None


def test_an_unexhausted_walk_yields_no_floor():
    coverage = build_settlement_coverage(normalized(settlement_row()), exhausted=False)
    assert coverage.floor is None


def test_one_unreadable_settled_time_withholds_the_whole_floor():
    # Fail-closed: the row whose clock will not parse might be the oldest one.
    coverage = build_settlement_coverage(
        normalized(
            settlement_row(settled_time="2026-06-01T00:00:00Z"),
            settlement_row(settled_time="not a timestamp"),
        ),
        exhausted=True,
    )
    assert coverage.rows_with_an_unreadable_time == 1
    assert coverage.rows_with_a_readable_time == 1
    assert coverage.floor is None


def test_no_rows_means_no_floor():
    assert build_settlement_coverage([], exhausted=True).floor is None


# ------------------------------------------------- applying the floor to state

def episodes_across_the_floor():
    """One episode well before the floor, one well after it."""
    fills = [
        normalize_fill(
            make_accounting_fill(
                1, "10", ticker=OLD_TICKER, created_time="2026-01-01T00:00:00Z"
            )
        ),
        normalize_fill(
            make_accounting_fill(
                2, "10", ticker=RECENT_TICKER, created_time="2026-08-01T00:00:00Z"
            )
        ),
    ]
    return fills


def replay_with_floor(floor):
    return AccountingEngine().replay(
        episodes_across_the_floor(), COMPLETE, settlement_floor=floor
    )


def by_ticker(result):
    return {e.ticker: e for e in result.episodes}


def test_an_episode_below_the_floor_has_an_unprovable_outcome():
    episodes = by_ticker(replay_with_floor(at("2026-03-01T00:00:00Z")))
    old = episodes[OLD_TICKER]
    assert old.is_open
    assert not old.outcome_provable
    assert old.outcome_bounded_by_settlement_coverage


def test_an_episode_above_the_floor_is_genuinely_open():
    episodes = by_ticker(replay_with_floor(at("2026-03-01T00:00:00Z")))
    recent = episodes[RECENT_TICKER]
    assert recent.is_open
    assert recent.outcome_provable
    assert not recent.outcome_bounded_by_settlement_coverage


def test_the_boundary_uses_last_activity_not_the_opening():
    # A position opened before the floor but added to after it sits inside the
    # evidence: if it had settled, the settlement would post after its last
    # fill, and so would be inside the route's reach.
    fills = [
        normalize_fill(
            make_accounting_fill(
                1, "10", ticker=OLD_TICKER, created_time="2026-01-01T00:00:00Z"
            )
        ),
        normalize_fill(
            make_accounting_fill(
                2, "5", ticker=OLD_TICKER, created_time="2026-08-01T00:00:00Z"
            )
        ),
    ]
    result = AccountingEngine().replay(
        fills, COMPLETE, settlement_floor=at("2026-03-01T00:00:00Z")
    )
    episode = by_ticker(result)[OLD_TICKER]
    assert episode.opened_at < at("2026-03-01T00:00:00Z")
    assert episode.last_activity_at > at("2026-03-01T00:00:00Z")
    assert episode.outcome_provable


def test_no_floor_reclassifies_nothing():
    # The fail-closed direction. Over-marking would convert real contradictions
    # into an explained boundary, which is the one failure this must not have.
    result = replay_with_floor(None)
    assert all(e.outcome_provable for e in result.episodes)
    assert result.episodes_with_an_unprovable_outcome == []


def test_a_closed_episode_is_never_reclassified():
    # Its outcome is already established; the floor has nothing to say about it.
    fills = [
        normalize_fill(
            make_accounting_fill(
                1, "10", ticker=OLD_TICKER, created_time="2026-01-01T00:00:00Z"
            )
        ),
        normalize_fill(
            make_accounting_fill(
                2, "10", action="sell", side="yes", ticker=OLD_TICKER,
                created_time="2026-01-02T00:00:00Z",
            )
        ),
    ]
    result = AccountingEngine().replay(
        fills, COMPLETE, settlement_floor=at("2026-03-01T00:00:00Z")
    )
    episode = by_ticker(result)[OLD_TICKER]
    assert not episode.is_open
    assert episode.outcome_provable


# -------------------------------------------------------- settlement closes it

def test_a_settled_episode_closes_at_the_settlement_clock():
    # Not at its opening fill's time. An episode opened in January and settled
    # in June did not close in January, and a holding period built from that
    # would be wrong by months.
    fills = [
        normalize_fill(
            make_accounting_fill(1, "10", created_time="2026-01-01T00:00:00Z")
        )
    ]
    result = AccountingEngine().replay(
        fills, COMPLETE, settlements=normalized(settlement_row())
    )
    episode = by_ticker(result)[SYNTH_TICKER]
    assert not episode.is_open
    assert episode.opened_at == at("2026-01-01T00:00:00Z")
    assert episode.closed_at == at("2026-06-01T00:00:00Z")


def test_an_unreadable_settlement_clock_leaves_closed_at_unset():
    fills = [
        normalize_fill(
            make_accounting_fill(1, "10", created_time="2026-01-01T00:00:00Z")
        )
    ]
    result = AccountingEngine().replay(
        fills,
        COMPLETE,
        settlements=normalized(settlement_row(settled_time="not a timestamp")),
    )
    episode = by_ticker(result)[SYNTH_TICKER]
    assert not episode.is_open
    assert episode.closed_at is None


# ------------------------------------------------------------- what is claimed

def test_diagnostics_split_open_from_unknowable():
    result = replay_with_floor(at("2026-03-01T00:00:00Z"))
    diagnostics = build_diagnostics(result)
    assert diagnostics.episodes_open_at_end == 2
    assert diagnostics.episodes_open_within_settlement_evidence == 1
    assert diagnostics.episodes_with_an_unprovable_outcome == 1
    assert diagnostics.episodes_bounded_by_settlement_coverage == 1
    assert diagnostics.settlement_floor_applied


def test_without_a_floor_nothing_is_claimed_to_be_inside_the_evidence():
    diagnostics = build_diagnostics(replay_with_floor(None))
    assert diagnostics.episodes_open_at_end == 2
    assert diagnostics.episodes_open_within_settlement_evidence == 0
    assert diagnostics.episodes_with_an_unprovable_outcome == 0
    assert not diagnostics.settlement_floor_applied


def test_bounded_coverage_denies_authority_without_alleging_a_defect():
    diagnostics = build_diagnostics(replay_with_floor(at("2026-03-01T00:00:00Z")))
    diagnostics.position_state_bounded_by_settlement_coverage = True
    assert diagnostics.claims_complete_position_state
    assert not diagnostics.position_state_contradicted_by_exchange
    assert not diagnostics.position_state_is_authoritative
    rendered = diagnostics.render()
    assert "POSITION STATE IS BOUNDED BY SETTLEMENT COVERAGE" in rendered
    assert "POSITION STATE IS CONTRADICTED" not in rendered


def test_authority_needs_all_three_conditions():
    diagnostics = build_diagnostics(replay_with_floor(at("2026-03-01T00:00:00Z")))
    assert not diagnostics.position_state_is_authoritative  # bounded episodes
    clean = build_diagnostics(replay_with_floor(at("2025-01-01T00:00:00Z")))
    assert clean.position_state_is_authoritative
    clean.position_state_contradicted_by_exchange = True
    assert not clean.position_state_is_authoritative


# --------------------------------------------------- the reconciliation split

def test_a_bounded_market_is_not_counted_as_unexplained():
    replayed = {OLD_TICKER: Decimal(10), RECENT_TICKER: Decimal(10)}
    report = probe_reconciliation([], [], replayed, frozenset({OLD_TICKER}))
    assert report.markets_absent_but_outside_settlement_evidence == 1
    assert report.markets_absent_and_unexplained == 1


def test_the_default_keeps_everything_unexplained():
    # Fail-closed: a caller that does not supply the bounded set gets the old,
    # louder answer rather than a quietly explained one.
    replayed = {OLD_TICKER: Decimal(10)}
    report = probe_reconciliation([], [], replayed)
    assert report.markets_absent_but_outside_settlement_evidence == 0
    assert report.markets_absent_and_unexplained == 1


def test_a_settlement_still_outranks_the_bounded_bucket():
    replayed = {SYNTH_TICKER: Decimal(10)}
    report = probe_reconciliation(
        [], [settlement_row()], replayed, frozenset({SYNTH_TICKER})
    )
    assert report.markets_absent_but_settled == 1
    assert report.markets_absent_but_outside_settlement_evidence == 0


# ------------------------------------------------------------------- rendering

def test_coverage_renders_counts_and_a_day_span_only():
    coverage = build_settlement_coverage(
        normalized(
            settlement_row(settled_time="2026-03-01T00:00:00Z"),
            settlement_row(settled_time="2026-09-01T00:00:00Z"),
        ),
        exhausted=True,
    )
    rendered = coverage.render()
    assert "settlement rows walked: 2" in rendered
    assert "evidence spans (days): 184" in rendered
    # No absolute timestamp reaches the log: the reach describes the route, the
    # dates would describe the member.
    assert "2026-03-01" not in rendered
    assert "2026-09-01" not in rendered


def test_coverage_as_dict_is_counts_and_booleans_only():
    for coverage in (
        SettlementCoverage(),
        build_settlement_coverage(normalized(settlement_row()), exhausted=True),
    ):
        for key, value in coverage.as_dict().items():
            assert isinstance(value, (int, bool)), f"{key} is not a count"


# ------------------------------ a refusal is evidence, not a coverage gap ----

def refusable_fills():
    """A position the settlement row will disagree with on size."""
    return [
        normalize_fill(
            make_accounting_fill(1, "10", created_time="2026-01-01T00:00:00Z")
        )
    ]


def test_a_refused_settlement_is_never_blamed_on_coverage():
    # The route DID speak about this market. That the sizes disagree is a
    # reconciliation problem; calling it a coverage gap would file a real
    # defect under an expected limit.
    result = AccountingEngine().replay(
        refusable_fills(),
        COMPLETE,
        settlements=normalized(settlement_row(yes_count_fp="7.00")),
        settlement_floor=at("2026-03-01T00:00:00Z"),
    )
    assert result.settlements_refused_unreconciled == 1
    episode = by_ticker(result)[SYNTH_TICKER]
    assert episode.is_open
    assert not episode.outcome_bounded_by_settlement_coverage


def test_a_refused_settlement_leaves_the_outcome_unprovable_not_open():
    # The exchange settled the market. The replay cannot reconcile the size, so
    # the outcome is unresolved -- reporting it as a live position would
    # overstate inventory by the whole episode.
    result = AccountingEngine().replay(
        refusable_fills(),
        COMPLETE,
        settlements=normalized(settlement_row(yes_count_fp="7.00")),
    )
    episode = by_ticker(result)[SYNTH_TICKER]
    assert episode.is_open
    assert not episode.outcome_provable
    diagnostics = build_diagnostics(result)
    assert diagnostics.episodes_with_an_unprovable_outcome == 1
    assert diagnostics.episodes_bounded_by_settlement_coverage == 0


def test_a_settlement_naming_an_unheld_market_marks_nothing():
    result = AccountingEngine().replay(
        refusable_fills(),
        COMPLETE,
        settlements=normalized(settlement_row(ticker="KXSYNTH-ELSEWHERE")),
        settlement_floor=at("2026-03-01T00:00:00Z"),
    )
    assert result.settlements_without_a_position == 1
    assert SYNTH_TICKER not in result.tickers_with_settlement_evidence
    # Its own market saw no settlement row at all, so coverage still applies.
    assert by_ticker(result)[SYNTH_TICKER].outcome_bounded_by_settlement_coverage


# ------------------------------------ which way did the disagreement run? ---

def replay_against(stated_yes, held="10"):
    """Replay a held position against a settlement stating a different size."""
    fills = [normalize_fill(make_accounting_fill(1, held, created_time="2026-01-01T00:00:00Z"))]
    return AccountingEngine().replay(
        fills, COMPLETE, settlements=normalized(settlement_row(yes_count_fp=stated_yes))
    )


def shapes(result):
    return build_diagnostics(result)


def test_a_settlement_larger_than_the_replay_is_recorded_as_larger():
    # Suggests fills the replay never saw, or a gross rather than net count.
    d = shapes(replay_against("15.00"))
    assert d.settlements_refused_settlement_larger == 1
    assert d.settlements_refused_settlement_smaller == 0


def test_a_settlement_smaller_than_the_replay_is_recorded_as_smaller():
    # The opposite diagnosis, and the opposite repair -- which is exactly why
    # one "refused" counter was not enough.
    d = shapes(replay_against("7.00"))
    assert d.settlements_refused_settlement_smaller == 1
    assert d.settlements_refused_settlement_larger == 0


def test_a_settlement_on_the_other_side_is_recorded_as_opposite():
    # Held YES, settled as a NO position: a different kind of wrong from a size
    # disagreement, and folding them together would hide it.
    result = AccountingEngine().replay(
        [normalize_fill(make_accounting_fill(1, "10", created_time="2026-01-01T00:00:00Z"))],
        COMPLETE,
        settlements=normalized(settlement_row(yes_count_fp="0.00", no_count_fp="10.00")),
    )
    d = build_diagnostics(result)
    assert d.settlements_refused_opposite_direction == 1
    assert result.settlements_refused_unreconciled == 1


def test_every_refusal_lands_in_exactly_one_shape():
    # The shapes must partition the refusals, or the diagnostic quietly loses
    # cases and reads as though fewer things went wrong.
    for stated in ("15.00", "7.00"):
        result = replay_against(stated)
        d = build_diagnostics(result)
        total = (
            d.settlements_refused_replay_flat
            + d.settlements_refused_settlement_larger
            + d.settlements_refused_settlement_smaller
            + d.settlements_refused_opposite_direction
        )
        assert total == result.settlements_refused_unreconciled == 1


def test_the_shapes_render_even_when_every_count_is_zero():
    # A shape that only appears once it happens cannot be read as "none seen".
    rendered = build_diagnostics(replay_with_floor(None)).render()
    assert "refusal shapes (which way the disagreement ran):" in rendered
    assert "settlement larger than the replay: 0" in rendered
    assert "opposite direction: 0" in rendered
