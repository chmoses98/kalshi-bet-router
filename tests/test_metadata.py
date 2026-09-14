"""Metadata resolution: caching, and lookup failures that degrade rather than crash."""

from __future__ import annotations

from kalshi_router.client import KalshiReadOnlyClient
from kalshi_router.config import AuditConfig
from kalshi_router.metadata import MetadataResolver
from kalshi_router.sports import Sport
from kalshi_router.classify import classify_market

from .synthetic import (
    FakeTransport,
    make_event,
    make_event_metadata,
    make_market,
    make_series,
    paged_fills_handler,
)

MARKET = "KXMLBGAME-SYNTH01-NYY"
EVENT = "KXMLBGAME-SYNTH01"
SERIES = "KXMLBGAME"

METADATA = {
    MARKET: make_market(MARKET, EVENT),
    EVENT: make_event(EVENT, SERIES),
    f"{EVENT}/metadata": make_event_metadata("Pro Baseball", "Game"),
    SERIES: make_series(SERIES, category="Sports", tags=["Baseball", "MLB"]),
}


def build(handler, signer):
    transport = FakeTransport(handler)
    client = KalshiReadOnlyClient(
        signer=signer, config=AuditConfig(max_retries=0), transport=transport, sleep=lambda _: None
    )
    return MetadataResolver(client), transport


def test_resolves_market_event_and_series(signer):
    resolver, transport = build(paged_fills_handler([[]], METADATA), signer)
    context = resolver.resolve(MARKET)
    assert context.market["event_ticker"] == EVENT
    assert context.event["series_ticker"] == SERIES
    assert context.series["tags"] == ["Baseball", "MLB"]
    assert context.competition == "Pro Baseball"
    assert context.competition_scope == "Game"
    assert classify_market(context).sport is Sport.MLB
    # market + event + event metadata + series
    assert len(transport.paths) == 4


def test_repeated_tickers_are_served_from_cache(signer):
    resolver, transport = build(paged_fills_handler([[]], METADATA), signer)
    for _ in range(4):
        resolver.resolve(MARKET)
    assert resolver.stats.markets_requested == 1
    assert resolver.stats.cache_hits == 3
    assert len(transport.paths) == 4


def test_market_lookup_failure_degrades_to_unresolved(signer):
    resolver, _ = build(paged_fills_handler([[]], {}), signer)
    context = resolver.resolve(MARKET)
    assert context.lookup_error is not None
    assert resolver.stats.market_lookup_failures == 1
    assert classify_market(context).sport is Sport.UNRESOLVED


def test_series_lookup_failure_keeps_market_and_counts_a_partial(signer):
    partial = {
        MARKET: METADATA[MARKET],
        EVENT: METADATA[EVENT],
        f"{EVENT}/metadata": METADATA[f"{EVENT}/metadata"],
    }
    resolver, _ = build(paged_fills_handler([[]], partial), signer)
    context = resolver.resolve(MARKET)
    assert context.market is not None and context.series is None
    assert resolver.stats.partial_lookup_failures == 1
    # The market ticker's series prefix is still an exact registry match.
    assert classify_market(context).sport is Sport.MLB


def test_event_metadata_counters_track_competition_presence(signer):
    resolver, _ = build(paged_fills_handler([[]], METADATA), signer)
    resolver.resolve(MARKET)
    assert resolver.stats.events_observed == 1
    assert resolver.stats.event_metadata_retrieved == 1
    assert resolver.stats.events_with_competition == 1
    assert resolver.stats.events_with_competition_scope == 1
    assert resolver.stats.event_metadata_failures == 0


def test_null_competition_is_a_valid_shape_not_a_failure(signer):
    metadata = dict(METADATA)
    metadata[f"{EVENT}/metadata"] = make_event_metadata(None, None)
    resolver, _ = build(paged_fills_handler([[]], metadata), signer)
    context = resolver.resolve(MARKET)
    assert context.competition is None
    assert resolver.stats.event_metadata_retrieved == 1
    assert resolver.stats.events_with_competition == 0
    assert resolver.stats.event_metadata_failures == 0
    # Falls through to series metadata rather than failing.
    assert classify_market(context).sport is Sport.MLB


def test_event_metadata_failure_degrades_without_breaking_the_walk(signer):
    metadata = {k: v for k, v in METADATA.items() if not k.endswith("/metadata")}
    resolver, _ = build(paged_fills_handler([[]], metadata), signer)
    context = resolver.resolve(MARKET)
    assert context.event_metadata is None
    assert context.event_metadata_error == "HttpStatusError(status=404)"
    assert resolver.stats.event_metadata_failures == 1
    assert classify_market(context).sport is Sport.MLB


def test_failure_labels_never_carry_response_bodies_or_tickers(signer):
    resolver, _ = build(paged_fills_handler([[]], {}), signer)
    context = resolver.resolve(MARKET)
    assert MARKET not in (context.lookup_error or "")
    assert "not found" not in (context.lookup_error or "")
    assert context.lookup_error == "HttpStatusError(status=404)"


def test_events_and_series_are_cached_across_markets(signer):
    second = "KXMLBGAME-SYNTH01-BOS"
    metadata = dict(METADATA)
    metadata[second] = make_market(second, EVENT)
    resolver, transport = build(paged_fills_handler([[]], metadata), signer)
    resolver.resolve(MARKET)
    resolver.resolve(second)
    # 2 markets + 1 event + 1 event metadata + 1 series: shared hops fetched once.
    assert len(transport.paths) == 5
