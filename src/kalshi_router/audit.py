"""Audit orchestration: fetch -> normalize -> deduplicate -> classify -> count.

Sensitive material (fills, tickers, classifications) exists only as local
variables and, when explicitly requested, in :class:`AuditResult.details`.
Nothing here writes to disk, emits an artifact, or logs a record.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .aggregate import AuditReport
from .classify import Classification, classify_market
from .client import KalshiReadOnlyClient
from .errors import KalshiRouterError
from .metadata import MetadataResolver
from .models import (
    Action,
    NormalizedFill,
    Side,
    count_partial_order_groups,
    dedupe_fills,
    group_by_order,
    normalize_fill,
)
from .sports import REPORT_ORDER, Sport


@dataclass(frozen=True)
class SensitiveDetail:
    """Per-market classification detail.

    SENSITIVE: bound to the owner's fills, this reveals what they traded.  Only
    the local ``--show-sensitive-details`` mode ever renders it.
    """

    market_ticker: str
    series_ticker: str | None
    event_ticker: str | None
    sport: Sport
    reason: str
    fill_count: int
    evidence: tuple[str, ...]


@dataclass
class AuditResult:
    report: AuditReport
    details: tuple[SensitiveDetail, ...] = ()
    #: Retained only when details were requested; empty otherwise.
    _classifications: dict[str, Classification] = field(default_factory=dict, repr=False)


def _describe_evidence(classification: Classification) -> tuple[str, ...]:
    return tuple(
        f"{e.source}={e.matched_token!r}"
        f"[{e.strength.value}"
        + (f"->{e.sport.value}" if e.sport else "")
        + (f" ambiguous:{e.ambiguous_family}" if e.ambiguous_family else "")
        + "]"
        for e in classification.evidence
    )


def run_audit(
    client: KalshiReadOnlyClient,
    max_fills: int | None = None,
    collect_details: bool = False,
) -> AuditResult:
    """Run one complete Phase 0 audit.

    Fails closed: a malformed fill or a malformed API envelope raises rather than
    being skipped, because silently dropping a fill would understate the account.
    A *metadata* lookup failure is different -- it is expected under partial
    outage, and degrades that market to ``UNRESOLVED`` with a counter bumped.
    """
    report = AuditReport()
    resolver = MetadataResolver(client)

    normalized: list[NormalizedFill] = []
    for raw in client.iter_fills(max_fills=max_fills):
        normalized.append(normalize_fill(raw))
    report.fills_fetched = len(normalized)

    deduped = dedupe_fills(normalized)
    report.unique_fills = deduped.unique_count
    report.duplicate_fill_ids_observed = deduped.duplicate_count
    fills = deduped.fills

    for fill in fills:
        if fill.action is Action.BUY:
            report.buy_fills += 1
        else:
            report.sell_fills += 1
        if fill.side is Side.YES:
            report.yes_side_fills += 1
        else:
            report.no_side_fills += 1
        if not fill.order_id:
            report.fills_without_order_id += 1
        if fill.count_needs_verification:
            report.fills_with_unverified_count += 1

    report.orders_observed = len(group_by_order(fills))
    report.partial_order_groups = count_partial_order_groups(fills)

    tickers = sorted({fill.ticker for fill in fills})
    report.unique_markets_observed = len(tickers)
    report.fills_requiring_metadata_lookup = len(fills)

    classifications: dict[str, Classification] = {}
    for ticker in tickers:
        context = resolver.resolve(ticker)
        try:
            classifications[ticker] = classify_market(context)
        except KalshiRouterError:
            report.classification_failures += 1
            classifications[ticker] = Classification(
                sport=Sport.UNRESOLVED,
                reason="classifier_error",
                market_ticker=ticker,
            )

    report.classification_counts = {sport: 0 for sport in REPORT_ORDER}
    fills_per_ticker: dict[str, int] = {}
    for fill in fills:
        classification = classifications[fill.ticker]
        report.classification_counts[classification.sport] += 1
        fills_per_ticker[fill.ticker] = fills_per_ticker.get(fill.ticker, 0) + 1

    report.classifications_using_unverified_series_ticker = sum(
        1 for c in classifications.values() if c.used_unverified_series_ticker
    )
    report.metadata_lookup_failures = resolver.stats.market_lookup_failures
    report.metadata_partial_failures = resolver.stats.partial_lookup_failures
    report.metadata_cache_hits = resolver.stats.cache_hits
    report.api_requests = client.request_count

    details: tuple[SensitiveDetail, ...] = ()
    if collect_details:
        details = tuple(
            SensitiveDetail(
                market_ticker=ticker,
                series_ticker=classification.series_ticker,
                event_ticker=classification.event_ticker,
                sport=classification.sport,
                reason=classification.reason,
                fill_count=fills_per_ticker.get(ticker, 0),
                evidence=_describe_evidence(classification),
            )
            for ticker, classification in sorted(classifications.items())
        )

    return AuditResult(report=report, details=details, _classifications=classifications)
