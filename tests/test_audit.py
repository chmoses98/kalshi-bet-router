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
    assert report.yes_side_fills == 5 and report.no_side_fills == 2


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
