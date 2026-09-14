"""End-to-end audit over a synthetic account, asserted on aggregates only."""

from __future__ import annotations

from kalshi_router.audit import run_audit
from kalshi_router.classify import EvidenceLevel
from kalshi_router.client import KalshiReadOnlyClient
from kalshi_router.config import AuditConfig
from kalshi_router.sports import Sport

from .synthetic import (
    FakeTransport,
    make_event,
    make_event_metadata,
    make_fill,
    make_market,
    make_series,
    make_taxonomy,
    paged_fills_handler,
)

# One market per outcome we want to exercise.  ``competition`` is what the live
# API returns from GET /events/{ticker}/metadata.
SCENARIO = {
    #  key      series ticker    competition            series fields
    "MLB": ("KXMLBGAME", "Pro Baseball", {"category": "Sports", "tags": ["Baseball"]}),
    "NFL": ("KXNFLGAME", "Pro Football", {"category": "Sports", "tags": ["Football"]}),
    "CFB": ("KXNCAAFGAME", "College Football", {"category": "Sports", "tags": ["Football"]}),
    "TEN": ("KXATPMATCH", "ATP Madrid", {"category": "Sports", "tags": ["Tennis"]}),
    # A tournament name with no tour token: only the live taxonomy maps it.
    "TN2": ("KXUSOPENTENNIS", "US Open Men Singles", {"category": "Sports"}),
    "NBA": ("KXNBAGAME", "Pro Basketball (M)", {"category": "Sports", "tags": ["Basketball"]}),
    # No competition at all, and only an ambiguous family tag: stays UNRESOLVED.
    "AMB": ("KXFOOTBALLX", None, {"category": "Sports", "tags": ["Football"]}),
}

TAXONOMY = make_taxonomy({
    "Baseball": ["Pro Baseball", "College Baseball"],
    "Football": ["Pro Football", "College Football"],
    "Tennis": ["ATP Madrid", "WTA Indian Wells", "US Open Men Singles"],
    "Basketball": ["Pro Basketball (M)", "College Basketball (M)"],
})


def event_for(key: str) -> str:
    return f"{SCENARIO[key][0]}-SYNTH01"


def market_for(key: str) -> str:
    return f"{event_for(key)}-{key}"


def build_metadata() -> dict:
    metadata: dict = {}
    for key, (series_ticker, competition, series_fields) in SCENARIO.items():
        event = event_for(key)
        market = market_for(key)
        metadata[market] = make_market(market, event)
        metadata[event] = make_event(event, series_ticker)
        metadata[f"{event}/metadata"] = make_event_metadata(
            competition, "Game" if competition else None
        )
        metadata[series_ticker] = make_series(series_ticker, **series_fields)
    return metadata


def build_client(pages, signer, metadata=None, taxonomy=TAXONOMY, milestones=None):
    handler = paged_fills_handler(
        pages,
        metadata if metadata is not None else build_metadata(),
        taxonomy=taxonomy,
        milestones=milestones,
    )
    return KalshiReadOnlyClient(
        signer=signer,
        config=AuditConfig(max_fills=500, page_limit=100, max_retries=0),
        transport=FakeTransport(handler),
        sleep=lambda _: None,
    )


def test_audit_classifies_every_sport_from_competition_metadata(signer):
    pages = [
        [
            make_fill(1, ticker=market_for("MLB")),
            make_fill(2, ticker=market_for("MLB")),
            make_fill(3, ticker=market_for("NFL"), action="sell", side="no"),
        ],
        [
            make_fill(4, ticker=market_for("CFB")),
            make_fill(5, ticker=market_for("TEN"), side="no"),
            make_fill(6, ticker=market_for("NBA")),
            make_fill(7, ticker=market_for("AMB")),
        ],
    ]
    report = run_audit(build_client(pages, signer)).report

    assert report.fills_fetched == 7
    assert report.unique_fills == 7
    assert report.classification_counts == {
        Sport.MLB: 2,
        Sport.NFL: 1,
        Sport.CFB: 1,
        Sport.TENNIS: 1,
        Sport.OTHER: 1,
        Sport.UNRESOLVED: 1,
    }
    assert sum(report.classification_counts.values()) == report.unique_fills
    assert report.supported_sport_count == 5
    assert report.classification_failures == 0
    assert report.buy_fills == 6 and report.sell_fills == 1
    # Canonical outcome_side: selling NO leaves the account positioned for YES,
    # so the one sell/no fill counts as YES rather than NO.
    assert report.yes_side_fills == 6 and report.no_side_fills == 1


def test_resolution_levels_are_attributed(signer):
    """MLB/NFL/CFB resolve at L1; a tennis tournament needs the L2 taxonomy."""
    pages = [[
        make_fill(1, ticker=market_for("MLB")),
        make_fill(2, ticker=market_for("NFL")),
        make_fill(3, ticker=market_for("CFB")),
        make_fill(4, ticker=market_for("TEN")),
        make_fill(5, ticker=market_for("TN2")),
    ]]
    report = run_audit(build_client(pages, signer)).report
    assert report.fills_resolved_by_level[EvidenceLevel.L1_EVENT_COMPETITION] == 4
    assert report.fills_resolved_by_level[EvidenceLevel.L2_SPORT_TAXONOMY] == 1
    assert report.fills_resolved_by_level[EvidenceLevel.L5_SERIES_REGISTRY] == 0
    assert report.markets_resolved_by_level[EvidenceLevel.L1_EVENT_COMPETITION] == 4
    assert report.classification_counts[Sport.TENNIS] == 2


def test_taxonomy_is_fetched_once_per_audit(signer):
    pages = [[make_fill(i, ticker=market_for("MLB")) for i in range(5)]]
    client = build_client(pages, signer)
    report = run_audit(client).report
    assert report.taxonomy_available is True
    assert report.taxonomy_sports == 4
    assert report.taxonomy_competitions == 9
    taxonomy_calls = [p for p in client._transport.paths if "filters_by_sport" in p]
    assert len(taxonomy_calls) == 1


def test_audit_survives_taxonomy_outage(signer):
    """Without the taxonomy the documented competitions still resolve."""
    pages = [[
        make_fill(1, ticker=market_for("MLB")),
        make_fill(2, ticker=market_for("TEN")),
        make_fill(3, ticker=market_for("TN2")),
    ]]
    report = run_audit(build_client(pages, signer, taxonomy=None)).report
    assert report.taxonomy_available is False
    assert report.classification_counts[Sport.MLB] == 1
    # ATP tour token still identifies tennis without the taxonomy...
    assert report.classification_counts[Sport.TENNIS] == 1
    # ...but a bare tournament name has nothing left to resolve it, and the
    # classifier refuses rather than guessing.
    assert report.classification_counts[Sport.UNRESOLVED] == 1
    assert report.unresolved_competition_unknown == 1


def test_unresolved_reasons_are_attributed(signer):
    pages = [[make_fill(1, ticker=market_for("AMB"))]]
    report = run_audit(build_client(pages, signer), collect_details=False).report
    assert report.classification_counts[Sport.UNRESOLVED] == 1
    assert report.unresolved_competition_absent + report.unresolved_ambiguous_family == 1


def test_unknown_competition_fails_closed_and_is_counted(signer):
    metadata = build_metadata()
    metadata[f"{event_for('NFL')}/metadata"] = make_event_metadata("Semi-Pro Football", "Game")
    pages = [[make_fill(1, ticker=market_for("NFL"))]]
    report = run_audit(build_client(pages, signer, metadata=metadata)).report
    assert report.classification_counts[Sport.UNRESOLVED] == 1
    assert report.unresolved_competition_unknown == 1


def test_milestone_backstop_resolves_events_without_competition(signer):
    from .synthetic import make_milestone

    metadata = build_metadata()
    metadata[f"{event_for('AMB')}/metadata"] = make_event_metadata(None, None)
    milestones = [
        make_milestone("m1", [event_for("AMB")], competition="College Football"),
    ]
    pages = [[make_fill(1, ticker=market_for("AMB"))]]
    report = run_audit(
        build_client(pages, signer, metadata=metadata, milestones=milestones)
    ).report
    assert report.milestone_index_built is True
    assert report.milestone_events_indexed >= 1
    assert report.classification_counts[Sport.CFB] == 1
    assert report.fills_resolved_by_level[EvidenceLevel.L3_MILESTONE] == 1


def test_milestone_sweep_is_skipped_when_nothing_needs_it(signer):
    pages = [[make_fill(1, ticker=market_for("MLB"))]]
    client = build_client(pages, signer, milestones=[])
    report = run_audit(client).report
    assert report.milestone_index_built is False
    assert report.milestone_requests_issued == 0
    assert not [p for p in client._transport.paths if "milestones" in p]


def test_milestones_can_be_disabled(signer):
    metadata = build_metadata()
    metadata[f"{event_for('AMB')}/metadata"] = make_event_metadata(None, None)
    pages = [[make_fill(1, ticker=market_for("AMB"))]]
    client = build_client(pages, signer, metadata=metadata, milestones=[])
    report = run_audit(client, use_milestones=False).report
    assert report.milestone_index_built is False
    assert not [p for p in client._transport.paths if "milestones" in p]


def test_duplicate_fill_ids_across_pages_are_deduplicated(signer):
    repeated = make_fill(1, ticker=market_for("MLB"))
    pages = [[repeated, make_fill(2, ticker=market_for("MLB"))], [repeated]]
    report = run_audit(build_client(pages, signer)).report
    assert report.fills_fetched == 3
    assert report.unique_fills == 2
    assert report.duplicate_fill_ids_observed == 1
    assert sum(report.classification_counts.values()) == 2


def test_partial_order_groups_are_counted(signer):
    pages = [[
        make_fill(1, ticker=market_for("MLB"), order_id="SYNTHORDER-A"),
        make_fill(2, ticker=market_for("MLB"), order_id="SYNTHORDER-A"),
        make_fill(3, ticker=market_for("MLB"), order_id="SYNTHORDER-A"),
        make_fill(4, ticker=market_for("NFL"), order_id="SYNTHORDER-B"),
        make_fill(5, ticker=market_for("NFL"), order_id=""),
    ]]
    report = run_audit(build_client(pages, signer)).report
    assert report.orders_observed == 2
    assert report.partial_order_groups == 1
    assert report.fills_without_an_order_id == 1


def test_fixed_point_field_presence_is_reported(signer):
    pages = [[make_fill(1, ticker=market_for("MLB"))]]
    report = run_audit(build_client(pages, signer)).report
    assert report.fills_quantity_from_count_fp == 1
    assert report.fills_quantity_from_legacy_count == 0
    assert report.fills_price_from_dollars == 1
    assert report.fills_without_price == 0
    assert report.fills_with_number_typed_fixed_point == 0


def test_empty_account_is_a_successful_audit(signer):
    report = run_audit(build_client([[]], signer)).report
    assert report.fills_fetched == 0
    assert report.account_has_no_fills is True
    assert report.classification_failures == 0
    assert "valid empty state" in report.render()


def test_metadata_outage_reports_unresolved_rather_than_failing(signer):
    pages = [[make_fill(1, ticker=market_for("MLB"))]]
    report = run_audit(build_client(pages, signer, metadata={})).report
    assert report.unique_fills == 1
    assert report.classification_counts[Sport.UNRESOLVED] == 1
    assert report.metadata_lookup_failures == 1
    assert report.unresolved_metadata_lookup_failed == 1
    assert report.classification_failures == 0


def test_max_fills_bounds_the_sample(signer):
    pages = [[make_fill(i, ticker=market_for("MLB")) for i in range(50)]]
    report = run_audit(build_client(pages, signer), max_fills=10).report
    assert report.fills_fetched == 10


def test_details_are_withheld_unless_explicitly_requested(signer):
    pages = [[make_fill(1, ticker=market_for("MLB"))]]
    assert run_audit(build_client(pages, signer)).details == ()
    detailed = run_audit(build_client(pages, signer), collect_details=True)
    assert len(detailed.details) == 1
    assert detailed.details[0].market_ticker == market_for("MLB")
    assert detailed.details[0].sport is Sport.MLB
    assert detailed.details[0].competition == "Pro Baseball"
    assert detailed.details[0].resolved_by is EvidenceLevel.L1_EVENT_COMPETITION
    assert detailed.details[0].evidence


# =================== Phase 0.1 fail-closed paths, end to end =================

def test_taxonomy_collision_makes_the_audit_unresolved_and_counted(signer):
    from .synthetic import make_taxonomy

    colliding = make_taxonomy({
        "Baseball": ["Pro Baseball"],
        "Tennis": ["Pro Baseball"],  # same name claimed by two sports
    })
    pages = [[make_fill(1, ticker=market_for("MLB"))]]
    report = run_audit(build_client(pages, signer, taxonomy=colliding)).report
    assert report.classification_counts[Sport.MLB] == 0
    assert report.classification_counts[Sport.UNRESOLVED] == 1
    assert report.unresolved_competition_ambiguous == 1
    assert report.taxonomy_competition_collisions == 1


def test_milestone_conflict_makes_the_audit_unresolved_and_counted(signer):
    from .synthetic import make_milestone

    metadata = build_metadata()
    metadata[f"{event_for('AMB')}/metadata"] = make_event_metadata(None, None)
    # The same event surfaces under two competition sweeps.
    milestones = [
        make_milestone("m1", [event_for("AMB")], competition="Pro Football"),
        make_milestone("m2", [event_for("AMB")], competition="College Football"),
    ]
    pages = [[make_fill(1, ticker=market_for("AMB"))]]
    report = run_audit(
        build_client(pages, signer, metadata=metadata, milestones=milestones)
    ).report
    assert report.classification_counts[Sport.UNRESOLVED] == 1
    assert report.classification_counts[Sport.NFL] == 0
    assert report.classification_counts[Sport.CFB] == 0
    assert report.milestone_event_conflicts >= 1
    assert report.unresolved_milestone_conflict == 1


def test_malformed_event_metadata_is_counted_and_never_rescued(signer):
    metadata = build_metadata()
    metadata[f"{event_for('MLB')}/metadata"] = {"competition": 123, "competition_scope": None}
    pages = [[make_fill(1, ticker=market_for("MLB"))]]
    report = run_audit(build_client(pages, signer, metadata=metadata)).report
    assert report.classification_counts[Sport.UNRESOLVED] == 1
    assert report.classification_counts[Sport.MLB] == 0
    assert report.unresolved_malformed_event_metadata == 1
    assert report.events_with_malformed_metadata == 1
    assert report.events_with_competition == 0


def test_a_bad_quantity_is_excluded_from_accounting_and_counted(signer):
    """Fail closed at the fill, not by aborting: the fill is excluded, never guessed."""
    bad = make_fill(1, ticker=market_for("MLB"))
    bad["count_fp"] = "0.00"
    good = make_fill(2, ticker=market_for("MLB"))
    result = run_audit(build_client([[bad, good]], signer))

    assert result.coverage.fills_seen == 2
    assert result.coverage.fills_normalized == 1
    assert result.coverage.fills_rejected == 1
    assert result.coverage.rejected_quantity == 1
    # The rejected fill never reaches accounting.
    assert result.accounting.fills_replayed == 1
    # And it is not silently counted as a classified fill.
    assert sum(result.report.classification_counts.values()) == 1


def test_empty_competition_is_counted_malformed_end_to_end(signer):
    """An empty competition must not become a routable MLB fill via L4/L5."""
    metadata = build_metadata()
    metadata[f"{event_for('MLB')}/metadata"] = {"competition": "", "competition_scope": None}
    pages = [[make_fill(1, ticker=market_for("MLB"))]]
    report = run_audit(build_client(pages, signer, metadata=metadata)).report
    assert report.classification_counts[Sport.MLB] == 0
    assert report.classification_counts[Sport.UNRESOLVED] == 1
    assert report.unresolved_malformed_event_metadata == 1
    assert report.events_with_malformed_metadata == 1
    assert report.events_with_competition == 0


# ============ classification scope is bounded separately (Phase C.11) ========
#
# Accounting needs every fill the account ever had; classification only needs
# enough markets to judge routing readiness, and it costs several metadata
# requests per market. Forcing one scope on both is what makes a full-history
# walk unaffordable.

def test_bounding_classification_leaves_accounting_untouched(signer):
    pages = [[make_fill(i, ticker=market_for("MLB")) for i in range(1, 4)]]
    result = run_audit(build_client(pages, signer), max_classify_markets=1)
    # Every fill still replayed...
    assert result.accounting.fills_replayed == 3
    # ...while only one market was classified.
    assert result.report.markets_classified == 1


def test_an_unclassified_market_is_never_counted_as_unresolved(signer):
    """Counting work never attempted as work that failed would understate
    classification quality, and would do so in the direction that looks like a
    defect in the classifier rather than a budget the caller chose."""
    tickers = [market_for("MLB"), market_for("NFL"), market_for("CFB")]
    pages = [[make_fill(i + 1, ticker=t) for i, t in enumerate(tickers)]]
    result = run_audit(build_client(pages, signer), max_classify_markets=1)
    report = result.report

    assert report.markets_not_classified == 2
    assert report.classification_counts[Sport.UNRESOLVED] == 0
    # The classified market resolved normally.
    assert sum(report.classification_counts.values()) == 1


def test_no_bound_classifies_everything(signer):
    tickers = [market_for("MLB"), market_for("NFL")]
    pages = [[make_fill(i + 1, ticker=t) for i, t in enumerate(tickers)]]
    result = run_audit(build_client(pages, signer))
    assert result.report.markets_not_classified == 0
    assert result.report.markets_classified == 2


def test_a_bound_larger_than_the_market_count_changes_nothing(signer):
    pages = [[make_fill(1, ticker=market_for("MLB"))]]
    result = run_audit(build_client(pages, signer), max_classify_markets=99)
    assert result.report.markets_not_classified == 0
    assert result.report.markets_classified == 1


# ================= Phase C.15: settlement coverage, end to end ===============

def coverage_client(signer, pages, settlements, archived_settlements=None, positions=None):
    handler = paged_fills_handler(
        pages,
        build_metadata(),
        taxonomy=TAXONOMY,
        positions=positions,
        settlements=settlements,
        archived_settlements=archived_settlements,
    )
    return KalshiReadOnlyClient(
        signer=signer,
        config=AuditConfig(max_fills=500, page_limit=100, max_retries=0),
        transport=FakeTransport(handler),
        sleep=lambda _: None,
    )


def settlement(ticker, settled_time):
    return {
        "ticker": ticker,
        "settled_time": settled_time,
        "market_result": "yes",
        "revenue": "1000",
        "value": "100",
        "fee_cost": "0.0700",
        "yes_count_fp": "1.00",
        "no_count_fp": "0.00",
    }


def test_audit_measures_settlement_reach_and_bounds_what_it_cannot_prove(signer):
    # One position opened well before any settlement the route will serve, one
    # opened well after. The first is unprovable; the second is really open.
    pages = [[
        make_fill(1, ticker=market_for("MLB"), created_time="2026-01-01T00:00:00Z"),
        make_fill(2, ticker=market_for("NFL"), created_time="2026-08-01T00:00:00Z"),
    ]]
    settlements = [settlement(market_for("CFB"), "2026-06-01T00:00:00Z")]
    result = run_audit(
        coverage_client(signer, pages, settlements), reconcile=True, full_history=True
    )

    assert result.settlement_coverage.rows == 1
    assert result.settlement_coverage.floor_is_usable
    assert result.accounting.settlement_floor_applied
    assert result.accounting.episodes_with_an_unprovable_outcome == 1
    assert result.accounting.episodes_bounded_by_settlement_coverage == 1
    assert result.accounting.episodes_open_within_settlement_evidence == 1
    # Bounded, not contradicted: a limit to state, not a defect to chase.
    assert result.accounting.position_state_bounded_by_settlement_coverage
    assert not result.accounting.position_state_is_authoritative
    assert result.reconciliation.markets_absent_but_outside_settlement_evidence == 1
    assert result.reconciliation.markets_absent_and_unexplained == 1


def test_an_absent_archive_settlements_route_is_recorded_not_fatal(signer):
    pages = [[make_fill(1, ticker=market_for("MLB"))]]
    result = run_audit(
        coverage_client(signer, pages, []), reconcile=True, full_history=True
    )
    coverage = result.settlement_coverage
    assert coverage.archive_route_probed
    assert not coverage.archive_route_available
    assert coverage.archive_route_status == 404


def test_an_available_archive_settlements_route_is_reported(signer):
    # If the route ever appears, the gap closes outright rather than being
    # bounded, so the audit must notice the day that changes.
    pages = [[make_fill(1, ticker=market_for("MLB"))]]
    result = run_audit(
        coverage_client(
            signer,
            pages,
            [],
            archived_settlements=[settlement(market_for("CFB"), "2020-01-01T00:00:00Z")],
        ),
        reconcile=True,
        full_history=True,
    )
    coverage = result.settlement_coverage
    assert coverage.archive_route_available
    assert coverage.archive_route_status == 200
    assert coverage.archive_first_page_rows == 1


def test_the_settlements_route_is_walked_once_per_audit(signer):
    # It used to be walked twice -- once for the replay and once inside the
    # reconciliation probe -- which doubled the cost and let the two views
    # disagree if a settlement landed between them.
    pages = [[make_fill(1, ticker=market_for("MLB"))]]
    handler = paged_fills_handler(
        pages, build_metadata(), taxonomy=TAXONOMY,
        settlements=[settlement(market_for("MLB"), "2026-06-01T00:00:00Z")],
    )
    transport = FakeTransport(handler)
    client = KalshiReadOnlyClient(
        signer=signer,
        config=AuditConfig(max_fills=500, page_limit=100, max_retries=0),
        transport=transport,
        sleep=lambda _: None,
    )
    run_audit(client, reconcile=True, full_history=True)
    walks = [p for p in transport.paths if "/portfolio/settlements" in p]
    assert len(walks) == 1


def test_without_reconcile_no_settlement_route_is_touched_at_all(signer):
    pages = [[make_fill(1, ticker=market_for("MLB"))]]
    handler = paged_fills_handler(pages, build_metadata(), taxonomy=TAXONOMY)
    transport = FakeTransport(handler)
    client = KalshiReadOnlyClient(
        signer=signer,
        config=AuditConfig(max_fills=500, page_limit=100, max_retries=0),
        transport=transport,
        sleep=lambda _: None,
    )
    result = run_audit(client)
    assert not any("settlements" in p for p in transport.paths)
    assert not result.settlement_coverage.archive_route_probed
    assert not result.accounting.settlement_floor_applied
