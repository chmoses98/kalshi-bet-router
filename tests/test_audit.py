"""End-to-end audit over a synthetic account, asserted on aggregates only."""

from __future__ import annotations

from kalshi_router.audit import run_audit
from kalshi_router.client import KalshiReadOnlyClient
from kalshi_router.config import AuditConfig
from kalshi_router.sports import Sport

from .synthetic import FakeTransport, make_event, make_market, make_series, make_fill, paged_fills_handler

# One market per sport, plus an out-of-scope market and an unresolvable one.
SCENARIO = {
    "MLB": ("KXMLBGAME", {"category": "Sports", "tags": ["Baseball", "MLB"]}),
    "NFL": ("KXNFLGAME", {"category": "Sports", "tags": ["Football", "NFL"]}),
    "CFB": ("KXNCAAFGAME", {"category": "Sports", "tags": ["Football", "College Football"]}),
    "TEN": ("KXATPMATCH", {"category": "Sports", "tags": ["Tennis", "ATP"]}),
    "NBA": ("KXNBAGAME", {"category": "Sports", "tags": ["Basketball", "NBA"]}),
    "AMB": ("KXFOOTBALLX", {"category": "Sports", "tags": ["Football"]}),
}


def build_metadata():
    metadata = {}
    for key, (series_ticker, series_fields) in SCENARIO.items():
        event = f"{series_ticker}-SYNTH01"
        market = f"{event}-{key}"
        metadata[market] = make_market(market, event)
        metadata[event] = make_event(event, series_ticker)
        metadata[series_ticker] = make_series(series_ticker, **series_fields)
    return metadata


def market_for(key):
    series_ticker = SCENARIO[key][0]
    return f"{series_ticker}-SYNTH01-{key}"


def build_client(pages, signer, metadata=None):
    transport = FakeTransport(paged_fills_handler(pages, metadata if metadata is not None else build_metadata()))
    return KalshiReadOnlyClient(
        signer=signer,
        config=AuditConfig(max_fills=500, page_limit=100, max_retries=0),
        transport=transport,
        sleep=lambda _: None,
    )


def test_audit_classifies_every_sport_and_counts_per_fill(signer):
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
    assert report.duplicate_fill_ids_observed == 0
    assert report.classification_counts == {
        Sport.MLB: 2,
        Sport.NFL: 1,
        Sport.CFB: 1,
        Sport.TENNIS: 1,
        Sport.OTHER: 1,
        Sport.UNRESOLVED: 1,
    }
    assert sum(report.classification_counts.values()) == report.unique_fills
    assert report.classification_failures == 0
    assert report.buy_fills == 6 and report.sell_fills == 1
    assert report.yes_side_fills == 5 and report.no_side_fills == 2
    assert report.unique_markets_observed == 6
    assert report.fills_requiring_metadata_lookup == 7


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
    assert report.fills_without_order_id == 1


def test_empty_account_is_a_successful_audit(signer):
    report = run_audit(build_client([[]], signer)).report
    assert report.fills_fetched == 0
    assert report.account_has_no_fills is True
    assert report.classification_failures == 0
    assert report.metadata_lookup_failures == 0
    assert "valid empty state" in report.render()


def test_metadata_outage_reports_unresolved_rather_than_failing(signer):
    pages = [[make_fill(1, ticker=market_for("MLB"))]]
    client = build_client(pages, signer, metadata={})
    report = run_audit(client).report
    assert report.unique_fills == 1
    assert report.classification_counts[Sport.UNRESOLVED] == 1
    assert report.metadata_lookup_failures == 1
    assert report.classification_failures == 0


def test_unverified_series_ticker_reliance_is_reported(signer):
    pages = [[make_fill(1, ticker=market_for("MLB"))]]
    report = run_audit(build_client(pages, signer)).report
    assert report.classifications_using_unverified_series_ticker == 1


def test_fixed_point_counts_are_reported_as_needing_verification(signer):
    raw = make_fill(1, ticker=market_for("MLB"), count=None)
    raw["count_fp"] = "10000000000"
    report = run_audit(build_client([[raw]], signer)).report
    assert report.fills_with_unverified_count == 1


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
    assert detailed.details[0].evidence  # explainable
