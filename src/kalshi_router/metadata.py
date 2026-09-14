"""Resolve public Kalshi market metadata, with caching and non-fatal failures.

Fills reference only a market ticker.  Deciding a sport needs the competition
behind that market, so each unique ticker is walked
``market -> event -> event metadata -> series`` and cached.  The event metadata
hop is the one that carries ``competition`` / ``competition_scope``, which is the
strongest classification evidence Kalshi publishes.  Caching matters for privacy as well as
rate limits: the number of metadata requests is reported as an aggregate, and it
tracks unique markets rather than fill volume.

A lookup failure is never fatal.  It degrades the classification to
``UNRESOLVED`` and increments a counter, so a partial API outage produces an
honest audit instead of a crash or a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .classify import MarketContext, validate_metadata_string_field
from .client import KalshiReadOnlyClient
from .errors import KalshiRouterError


@dataclass
class ResolverStats:
    """Privacy-safe counters describing metadata resolution."""

    markets_requested: int = 0
    cache_hits: int = 0
    market_lookup_failures: int = 0
    partial_lookup_failures: int = 0
    events_observed: int = 0
    event_metadata_retrieved: int = 0
    event_metadata_failures: int = 0
    events_with_competition: int = 0
    events_with_competition_scope: int = 0
    events_with_malformed_metadata: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "markets_requested": self.markets_requested,
            "cache_hits": self.cache_hits,
            "market_lookup_failures": self.market_lookup_failures,
            "partial_lookup_failures": self.partial_lookup_failures,
            "events_observed": self.events_observed,
            "event_metadata_retrieved": self.event_metadata_retrieved,
            "event_metadata_failures": self.event_metadata_failures,
            "events_with_competition": self.events_with_competition,
            "events_with_competition_scope": self.events_with_competition_scope,
            "events_with_malformed_metadata": self.events_with_malformed_metadata,
        }


def _failure_label(exc: Exception) -> str:
    """A non-secret description of a failure.

    Only the exception type and, for HTTP errors, the status code are kept.
    Tickers, bodies and messages that could carry account state are dropped.
    """
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return f"{type(exc).__name__}(status={status})"
    return type(exc).__name__


@dataclass
class MetadataResolver:
    """Caching ``market -> event -> series`` resolver."""

    client: KalshiReadOnlyClient
    stats: ResolverStats = field(default_factory=ResolverStats)
    _market_cache: dict[str, MarketContext] = field(default_factory=dict, repr=False)
    _event_cache: dict[str, dict[str, Any] | None] = field(default_factory=dict, repr=False)
    _event_metadata_cache: dict[str, dict[str, Any] | None] = field(default_factory=dict, repr=False)
    _series_cache: dict[str, dict[str, Any] | None] = field(default_factory=dict, repr=False)

    def resolve(self, market_ticker: str) -> MarketContext:
        cached = self._market_cache.get(market_ticker)
        if cached is not None:
            self.stats.cache_hits += 1
            return cached

        self.stats.markets_requested += 1
        try:
            market = self.client.get_market(market_ticker)
        except KalshiRouterError as exc:
            self.stats.market_lookup_failures += 1
            context = MarketContext(
                market_ticker=market_ticker, lookup_error=_failure_label(exc)
            )
            self._market_cache[market_ticker] = context
            return context

        event_ticker = market.get("event_ticker") or (market.get("event") or {}).get("event_ticker")
        event = self._resolve_event(event_ticker)
        event_metadata, event_metadata_error = self._resolve_event_metadata(event_ticker)
        series_ticker = (
            (event or {}).get("series_ticker") or market.get("series_ticker")
        )
        series = self._resolve_series(series_ticker)

        context = MarketContext(
            market_ticker=market_ticker,
            market=market,
            event=event,
            series=series,
            event_metadata=event_metadata,
            event_metadata_error=event_metadata_error,
        )
        self._market_cache[market_ticker] = context
        return context

    def _resolve_event_metadata(
        self, event_ticker: Any
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Fetch ``competition`` / ``competition_scope`` for one event.

        A failure here is never fatal: it degrades the market to the weaker
        evidence levels and increments a counter.  A *successful* response with a
        null competition is a valid shape, not a failure -- plenty of events are
        not sports.
        """
        if not isinstance(event_ticker, str) or not event_ticker.strip():
            return None, None
        key = event_ticker.strip()
        if key in self._event_metadata_cache:
            return self._event_metadata_cache[key], None

        self.stats.events_observed += 1
        try:
            metadata = self.client.get_event_metadata(key)
        except KalshiRouterError as exc:
            self.stats.event_metadata_failures += 1
            self._event_metadata_cache[key] = None
            return None, _failure_label(exc)

        self.stats.event_metadata_retrieved += 1
        # Presence is only counted for a *valid* value; a wrong-typed field is
        # counted as malformed and will fail the classification closed.
        competition, competition_error = validate_metadata_string_field(metadata, "competition")
        scope, scope_error = validate_metadata_string_field(metadata, "competition_scope")
        if competition_error or scope_error:
            self.stats.events_with_malformed_metadata += 1
        else:
            if competition:
                self.stats.events_with_competition += 1
            if scope:
                self.stats.events_with_competition_scope += 1

        self._event_metadata_cache[key] = metadata
        return metadata, None

    def _resolve_event(self, event_ticker: Any) -> dict[str, Any] | None:
        if not isinstance(event_ticker, str) or not event_ticker.strip():
            return None
        key = event_ticker.strip()
        if key in self._event_cache:
            return self._event_cache[key]
        try:
            event = self.client.get_event(key)
        except KalshiRouterError:
            self.stats.partial_lookup_failures += 1
            event = None
        self._event_cache[key] = event
        return event

    def _resolve_series(self, series_ticker: Any) -> dict[str, Any] | None:
        if not isinstance(series_ticker, str) or not series_ticker.strip():
            return None
        key = series_ticker.strip()
        if key in self._series_cache:
            return self._series_cache[key]
        try:
            series = self.client.get_series(key)
        except KalshiRouterError:
            self.stats.partial_lookup_failures += 1
            series = None
        self._series_cache[key] = series
        return series
