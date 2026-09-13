"""Metadata resolution: caching, and lookup failures that degrade rather than crash."""

from __future__ import annotations

from kalshi_router.client import KalshiReadOnlyClient
from kalshi_router.config import AuditConfig
from kalshi_router.metadata import MetadataResolver
from kalshi_router.sports import Sport
from kalshi_router.classify import classify_market

from .synthetic import FakeTransport, make_event, make_market, make_series, paged_fills_handler

MARKET = "KXMLBGAME-SYNTH01-NYY"
EVENT = "KXMLBGAME-SYNTH01"
SERIES = "KXMLBGAME"

METADATA = {
    MARKET: make_market(MARKET, EVENT),
    EVENT: make_event(EVENT, SERIES),
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
    assert classify_market(context).sport is Sport.MLB
    assert len(transport.paths) == 3


def test_repeated_tickers_are_served_from_cache(signer):
    resolver, transport = build(paged_fills_handler([[]], METADATA), signer)
    for _ in range(4):
        resolver.resolve(MARKET)
    assert resolver.stats.markets_requested == 1
    assert resolver.stats.cache_hits == 3
    assert len(transport.paths) == 3


def test_market_lookup_failure_degrades_to_unresolved(signer):
    resolver, _ = build(paged_fills_handler([[]], {}), signer)
    context = resolver.resolve(MARKET)
    assert context.lookup_error is not None
    assert resolver.stats.market_lookup_failures == 1
    assert classify_market(context).sport is Sport.UNRESOLVED


def test_series_lookup_failure_keeps_market_and_counts_a_partial(signer):
    partial = {MARKET: METADATA[MARKET], EVENT: METADATA[EVENT]}
    resolver, _ = build(paged_fills_handler([[]], partial), signer)
    context = resolver.resolve(MARKET)
    assert context.market is not None and context.series is None
    assert resolver.stats.partial_lookup_failures == 1
    # The market ticker's series prefix is still an exact registry match.
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
    # 2 markets + 1 event + 1 series: the event and series are fetched once.
    assert len(transport.paths) == 4
