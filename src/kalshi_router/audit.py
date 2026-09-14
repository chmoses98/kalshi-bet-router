"""Audit orchestration.

Flow: fetch fills -> normalize -> deduplicate -> resolve public metadata ->
fetch the public sport taxonomy -> classify -> (only if needed) sweep public
milestones and re-classify what is still unresolved -> count.

Sensitive material (fills, tickers, competitions, classifications) exists only as
local variables and, when explicitly requested, in :class:`AuditResult.details`.
Nothing here writes to disk, emits an artifact, or logs a record.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .accounting.diagnostics import AccountingDiagnostics, build_diagnostics
from .accounting.engine import AccountingEngine, HistoryCompleteness
from .aggregate import AuditReport
from .classify import (
    Classification,
    EvidenceLevel,
    UnresolvedReason,
    classify_market,
)
from .client import KalshiReadOnlyClient, WalkStats
from .errors import KalshiRouterError, SchemaError
from .history import HistoryEvidence
from .metadata import MetadataResolver
from .milestones import MilestoneIndex, build_milestone_index
from .models import (
    normalize_settlement,
    Action,
    NormalizedFill,
    OutcomeSide,
    count_partial_order_groups,
    dedupe_fills,
    group_by_order,
    normalize_fill,
)
from .reconcile import ReconciliationReport, probe_reconciliation
from .safety import safe_schema_name
from .schema_probe import SchemaCoverage, probe_fills
from .sports import REPORT_ORDER, Sport
from .taxonomy import SportTaxonomy, parse_filters_by_sport

#: Fail-closed reasons a public milestone sweep could plausibly repair.
MILESTONE_REPAIRABLE = frozenset({
    UnresolvedReason.COMPETITION_ABSENT,
    UnresolvedReason.INSUFFICIENT,
    # A "Football" series with no competition is precisely the case a public
    # milestone can settle into Pro vs College football.
    UnresolvedReason.AMBIGUOUS_FAMILY,
})


@dataclass(frozen=True)
class SensitiveDetail:
    """Per-market classification detail.

    SENSITIVE: bound to the owner's fills, this reveals what they traded.  Only
    the local ``--show-sensitive-details`` mode ever renders it.
    """

    market_ticker: str
    series_ticker: str | None
    event_ticker: str | None
    competition: str | None
    competition_scope: str | None
    sport: Sport
    resolved_by: EvidenceLevel | None
    reason: str
    fill_count: int
    evidence: tuple[str, ...]


@dataclass
class AuditResult:
    report: AuditReport
    #: Shadow-only accounting counts.  Never routed, never persisted.
    accounting: AccountingDiagnostics = field(default_factory=AccountingDiagnostics)
    #: Live schema coverage.  Counts only.
    coverage: SchemaCoverage = field(default_factory=SchemaCoverage)
    #: Replay-versus-exchange measurement.  Empty unless explicitly requested.
    reconciliation: ReconciliationReport | None = None
    #: What the fill walks earned the right to claim about the history.
    history: HistoryEvidence = field(default_factory=HistoryEvidence)
    details: tuple[SensitiveDetail, ...] = ()
    _classifications: dict[str, Classification] = field(default_factory=dict, repr=False)


def _describe_evidence(classification: Classification) -> tuple[str, ...]:
    return tuple(
        f"{e.source}={e.matched_token!r}"
        f"[{(e.level.value + '/') if e.level else ''}{e.strength.value}"
        + (f"->{e.sport.value}" if e.sport else "")
        + (f" ambiguous:{e.ambiguous_family}" if e.ambiguous_family else "")
        + "]"
        for e in classification.evidence
    )


def _fetch_taxonomy(client: KalshiReadOnlyClient, report: AuditReport) -> SportTaxonomy | None:
    """Fetch the public sport taxonomy once, degrading rather than failing.

    The taxonomy is public catalogue data, so this request discloses nothing
    about the account.  If it is unavailable the classifier still resolves the
    documented competition strings directly; coverage narrows, correctness does not.
    """
    try:
        taxonomy = parse_filters_by_sport(client.get_filters_by_sport())
    except KalshiRouterError:
        report.taxonomy_available = False
        return None
    report.taxonomy_available = True
    report.taxonomy_sports = taxonomy.sport_count
    report.taxonomy_competitions = taxonomy.competition_count
    report.taxonomy_competition_collisions = taxonomy.collision_count
    report.taxonomy_skipped_sports = taxonomy.skipped_sports
    # Sorted so the diagnostic is stable run to run, and rendered as key:kind so
    # one line says both what exists and what type it holds.
    report.taxonomy_observed_keys = tuple(
        name
        for key in sorted(taxonomy.observed_keys)
        if (name := safe_schema_name(key, taxonomy.observed_key_kinds.get(key))) is not None
    )
    report.taxonomy_observed_entry_keys = tuple(
        name
        for key in sorted(taxonomy.observed_entry_keys)
        if (name := safe_schema_name(key)) is not None
    )
    return taxonomy


def _record_milestone_stats(report: AuditReport, index: MilestoneIndex) -> None:
    report.milestone_index_built = True
    report.milestone_requests_issued = index.requests_issued
    report.milestone_events_indexed = index.indexed_events
    report.milestone_event_conflicts = index.conflict_count
    report.milestone_fetch_failed = index.fetch_failed
    report.milestone_budget_exhausted = index.budget_exhausted
    report.milestone_rows_seen = index.rows_seen
    report.milestone_entry_keys = tuple(
        name
        for key in sorted(index.observed_entry_keys)
        if (name := safe_schema_name(key)) is not None
    )


def run_audit(
    client: KalshiReadOnlyClient,
    max_fills: int | None = None,
    collect_details: bool = False,
    use_milestones: bool = True,
    reconcile: bool = False,
    full_history: bool = False,
) -> AuditResult:
    """Run one complete Phase 0.1 audit.

    Fails closed: a malformed fill or a malformed API envelope raises rather than
    being skipped.  Public-catalogue failures (taxonomy, milestones) and metadata
    lookup failures degrade classification instead, with counters bumped.
    """
    report = AuditReport()
    resolver = MetadataResolver(client)

    # Strict parsing, tolerant ingestion: a fill that cannot be interpreted is
    # EXCLUDED from accounting and counted, never admitted with guessed values.
    # Aborting the whole audit on one odd fill would teach us nothing about the
    # live schema, which is the entire point of this run.
    normalized, coverage, history_evidence = _ingest_fills(
        client, max_fills, full_history
    )
    report.fills_fetched = coverage.fills_seen

    deduped = dedupe_fills(normalized)
    report.unique_fills = deduped.unique_count
    report.duplicate_fill_ids_observed = deduped.duplicate_count
    fills = deduped.fills

    for fill in fills:
        # Which outcome the fill left the account positioned for. That is
        # the EXPOSURE, not the `outcome_side` field: live data shows the field
        # reports the contract, so a sell-NO arrives as `no` while moving the
        # position toward YES.
        if fill.exposure_side is OutcomeSide.YES:
            report.yes_side_fills += 1
        else:
            report.no_side_fills += 1
        # The deprecated action verb, reported only while Kalshi still sends it.
        if fill.legacy_action is Action.BUY:
            report.buy_fills += 1
        elif fill.legacy_action is Action.SELL:
            report.sell_fills += 1
        else:
            report.fills_without_legacy_action += 1
        if not fill.order_id:
            report.fills_without_an_order_id += 1
        if fill.count_source == "count_fp":
            report.fills_quantity_from_count_fp += 1
        elif fill.count_source == "count":
            report.fills_quantity_from_legacy_count += 1
        if fill.price_source == "unified_price_dollars":
            report.fills_price_from_dollars += 1
        elif fill.price_source == "legacy_price_cents":
            report.fills_price_from_legacy_cents += 1
        else:
            report.fills_without_price += 1
        if fill.fixed_point_number_typed:
            report.fills_with_number_typed_fixed_point += 1

    report.orders_observed = len(group_by_order(fills))
    report.partial_order_groups = count_partial_order_groups(fills)

    # ---- shadow accounting (Phase 1A): replay only, routes nothing ----------
    # The audit samples a bounded recent window, so the replay is told exactly
    # that and refuses to describe its output as the account's position state.
    # Settlements are fetched only when reconciliation was asked for, because
    # the walk is unbounded. Without them the replay cannot see a close: a
    # settled market pays out with no fill at all.
    settlements = _fetch_settlements(client, report) if reconcile else []

    # The replay is told exactly what the walks earned. A history is COMPLETE
    # only when both fill routes ran out of data rather than out of budget, so
    # asking for a full walk does not by itself grant the claim.
    replay = None
    try:
        replay = AccountingEngine().replay(
            fills, history_evidence.completeness, settlements=settlements
        )
        accounting = build_diagnostics(replay)
    except SchemaError:
        accounting = AccountingDiagnostics(accounting_schema_failures=1)

    tickers = sorted({fill.ticker for fill in fills})
    report.unique_markets_observed = len(tickers)
    report.fills_requiring_metadata_lookup = len(fills)

    contexts = {ticker: resolver.resolve(ticker) for ticker in tickers}
    taxonomy = _fetch_taxonomy(client, report) if tickers else None

    classifications: dict[str, Classification] = {}
    for ticker, context in contexts.items():
        try:
            classifications[ticker] = classify_market(context, taxonomy=taxonomy)
        except KalshiRouterError:
            report.classification_failures += 1
            classifications[ticker] = Classification(
                sport=Sport.UNRESOLVED, reason="classifier_error", market_ticker=ticker
            )

    # The milestone sweep is a backstop, not a default cost: it only runs when
    # markets remain unresolved for a reason public milestones could repair.
    repairable = [
        ticker for ticker, c in classifications.items()
        if c.sport is Sport.UNRESOLVED and c.unresolved_reason in MILESTONE_REPAIRABLE
    ]
    if use_milestones and repairable:
        index = build_milestone_index(client)
        _record_milestone_stats(report, index)
        # A conflicted event carries no competition, so ``indexed_events`` can be
        # zero while the sweep still has something decisive to say: that the
        # evidence conflicts and the market must stay unresolved.
        if index.indexed_events or index.conflict_count:
            for ticker in repairable:
                try:
                    retry = classify_market(
                        contexts[ticker], taxonomy=taxonomy, milestone_index=index
                    )
                except KalshiRouterError:
                    continue
                if retry.sport is not Sport.UNRESOLVED or (
                    retry.unresolved_reason is UnresolvedReason.MILESTONE_CONFLICT
                ):
                    classifications[ticker] = retry

    # ------------------------------------------------------------- counting
    report.classification_counts = {sport: 0 for sport in REPORT_ORDER}
    fills_per_ticker: dict[str, int] = {}
    for fill in fills:
        fills_per_ticker[fill.ticker] = fills_per_ticker.get(fill.ticker, 0) + 1

    reason_fields = {
        UnresolvedReason.COMPETITION_ABSENT: "unresolved_competition_absent",
        UnresolvedReason.COMPETITION_UNKNOWN: "unresolved_competition_unknown",
        UnresolvedReason.COMPETITION_AMBIGUOUS: "unresolved_competition_ambiguous",
        UnresolvedReason.MILESTONE_CONFLICT: "unresolved_milestone_conflict",
        UnresolvedReason.MALFORMED_EVENT_METADATA: "unresolved_malformed_event_metadata",
        UnresolvedReason.EVIDENCE_CONFLICT: "unresolved_evidence_conflict",
        UnresolvedReason.AMBIGUOUS_FAMILY: "unresolved_ambiguous_family",
        UnresolvedReason.METADATA_LOOKUP_FAILED: "unresolved_metadata_lookup_failed",
        UnresolvedReason.NO_METADATA: "unresolved_metadata_lookup_failed",
        UnresolvedReason.INSUFFICIENT: "unresolved_insufficient_metadata",
    }

    for ticker, classification in classifications.items():
        weight = fills_per_ticker.get(ticker, 0)
        report.classification_counts[classification.sport] += weight

        if classification.resolved_by is not None:
            report.fills_resolved_by_level[classification.resolved_by] = (
                report.fills_resolved_by_level.get(classification.resolved_by, 0) + weight
            )
            report.markets_resolved_by_level[classification.resolved_by] = (
                report.markets_resolved_by_level.get(classification.resolved_by, 0) + 1
            )
        if classification.lower_level_conflict:
            report.lower_level_conflicts_overruled += weight
        if classification.sport is Sport.UNRESOLVED:
            field_name = reason_fields.get(
                classification.unresolved_reason, "unresolved_insufficient_metadata"
            )
            setattr(report, field_name, getattr(report, field_name) + weight)

    report.classifications_using_unverified_series_ticker = sum(
        1 for c in classifications.values() if c.used_unverified_series_ticker
    )
    report.metadata_lookup_failures = resolver.stats.market_lookup_failures
    report.metadata_partial_failures = resolver.stats.partial_lookup_failures
    report.metadata_cache_hits = resolver.stats.cache_hits
    report.unique_events_observed = resolver.stats.events_observed
    report.events_with_metadata_retrieved = resolver.stats.event_metadata_retrieved
    report.events_with_competition = resolver.stats.events_with_competition
    report.events_with_competition_scope = resolver.stats.events_with_competition_scope
    report.events_with_malformed_metadata = resolver.stats.events_with_malformed_metadata
    report.event_metadata_lookup_failures = resolver.stats.event_metadata_failures
    report.api_requests = client.request_count

    details: tuple[SensitiveDetail, ...] = ()
    if collect_details:
        details = tuple(
            SensitiveDetail(
                market_ticker=ticker,
                series_ticker=c.series_ticker,
                event_ticker=c.event_ticker,
                competition=c.competition,
                competition_scope=c.competition_scope,
                sport=c.sport,
                resolved_by=c.resolved_by,
                reason=c.reason,
                fill_count=fills_per_ticker.get(ticker, 0),
                evidence=_describe_evidence(c),
            )
            for ticker, c in sorted(classifications.items())
        )

    # A replay that failed has nothing to reconcile against, and comparing an
    # empty replay would report every market as "missing from the replay" --
    # a fabricated finding.
    reconciliation = (
        _probe_reconciliation(client, replay) if reconcile and replay is not None else None
    )

    return AuditResult(
        report=report,
        accounting=accounting,
        coverage=coverage,
        history=history_evidence,
        reconciliation=reconciliation,
        details=details,
        _classifications=classifications,
    )


def _ingest_fills(
    client: KalshiReadOnlyClient, max_fills: int | None, full_history: bool
) -> tuple[list, SchemaCoverage, HistoryEvidence]:
    """Walk the fill routes and record what may be claimed about the result.

    Strict parsing, tolerant ingestion: a fill that cannot be interpreted is
    EXCLUDED from accounting and counted, never admitted with guessed values.

    The archive route is walked only when a full history was asked for, because
    it is unbounded. Skipping it is recorded rather than ignored: the live route
    does not serve fills older than the cutoff at all, so not asking is not
    evidence that none exist.
    """
    evidence = HistoryEvidence()
    live = WalkStats()

    if not full_history:
        normalized, coverage = probe_fills(
            client.iter_fills(max_fills=max_fills, stats=live)
        )
        evidence.historical_skipped = True
    else:
        archive = WalkStats()
        normalized, coverage = probe_fills(
            _chain_fill_routes(client, max_fills, live, archive, evidence)
        )
        evidence.historical_rows = archive.rows
        evidence.historical_pages = archive.pages
        evidence.historical_exhausted = archive.exhausted
        evidence.historical_truncated = archive.truncated

    evidence.live_rows = live.rows
    evidence.live_pages = live.pages
    evidence.live_exhausted = live.exhausted
    evidence.live_truncated = live.truncated
    evidence.fills_rejected = coverage.fills_rejected
    return normalized, coverage, evidence


def _chain_fill_routes(
    client: KalshiReadOnlyClient,
    max_fills: int | None,
    live: WalkStats,
    archive: WalkStats,
    evidence: HistoryEvidence,
):
    """Yield the live route then the archive, as one stream.

    An archive that fails is UNPROVEN, never empty -- treating a failure as
    "there was nothing older" is how a partial history gets promoted to a
    complete one, silently.
    """
    yield from client.iter_fills(max_fills=max_fills, stats=live)

    try:
        client.get_historical_cutoff()
        evidence.cutoff_retrieved = True
    except KalshiRouterError:
        evidence.cutoff_retrieved = False

    try:
        yield from client.iter_historical_fills(max_fills=max_fills, stats=archive)
    except KalshiRouterError:
        evidence.historical_failed = True


def _fetch_settlements(client: KalshiReadOnlyClient, report: AuditReport) -> list:
    """Normalize the settlement walk, excluding-and-counting what will not parse.

    Same rule as the fill probe: a settlement that cannot be interpreted is left
    out of accounting and counted, never admitted with an invented payout.
    """
    settlements = []
    for raw in client.iter_settlements():
        try:
            settlements.append(normalize_settlement(raw))
        except SchemaError:
            report.settlements_rejected += 1
    report.settlements_fetched = len(settlements) + report.settlements_rejected
    return settlements


def _probe_reconciliation(client: KalshiReadOnlyClient, replay) -> ReconciliationReport:
    """Measure the replay against the exchange's own position view.

    Opt-in, because it walks two more paginated collections and the bounded
    200-fill audit already issues hundreds of requests.  Nothing downstream
    depends on the result: it is a measurement, not a verdict.
    """
    replayed = {
        ticker: ledger.position
        for (_subaccount, ticker), ledger in replay.ledgers.items()
    }
    return probe_reconciliation(
        client.iter_positions(), client.iter_settlements(), replayed
    )
