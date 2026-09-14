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
from .coverage import SettlementCoverage, build_settlement_coverage
from .errors import HttpStatusError, KalshiRouterError, SchemaError
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
from .reconcile import (
    ReconciliationReport,
    exchange_position_view,
    probe_reconciliation,
)
from .safety import safe_schema_name
from .schema_probe import SchemaCoverage, probe_fills
from .sports import REPORT_ORDER, Sport
from .taxonomy import SportTaxonomy, parse_filters_by_sport
from .timeaxis import parse_rfc3339_seconds

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
    #: How far back settlement evidence reaches. Empty unless reconciling.
    settlement_coverage: SettlementCoverage = field(
        default_factory=SettlementCoverage
    )
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
    max_classify_markets: int | None = None,
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
    if reconcile:
        settlement_rows, settlements, settlement_coverage = _fetch_settlements(
            client, report
        )
        _probe_below_the_floor(client, settlement_coverage)
        _probe_settlement_archive(client, settlement_coverage)
        # Fetched BEFORE the replay, not after it. Position authority is an
        # input to the accounting, not a comment on it: an episode the exchange
        # does not confirm must never be handed a stable identity in the first
        # place, and a check that runs afterwards can only complain about one
        # that already exists.
        position_rows = list(client.iter_positions())
        exchange_positions = exchange_position_view(position_rows)
    else:
        settlement_rows, settlements = [], []
        settlement_coverage = SettlementCoverage()
        position_rows, exchange_positions = [], None

    # The replay is told exactly what the walks earned. A history is COMPLETE
    # only when both fill routes ran out of data rather than out of budget, so
    # asking for a full walk does not by itself grant the claim. The settlement
    # floor is a SECOND completeness dimension: complete fills prove where a
    # position opened, settlement coverage is what can prove whether it ever
    # closed, and the two routes do not reach equally far back.
    replay = None
    try:
        replay = AccountingEngine().replay(
            fills,
            history_evidence.completeness,
            settlements=settlements,
            settlement_floor=settlement_coverage.floor,
            exchange_positions=exchange_positions,
        )
        accounting = build_diagnostics(replay)
    except SchemaError:
        accounting = AccountingDiagnostics(accounting_schema_failures=1)

    # Accounting and classification have different natural scopes, and forcing
    # them to share one is what makes a full-history walk unaffordable. The
    # ledger needs every fill the account ever had; classification only needs
    # enough markets to judge routing readiness, and it costs several metadata
    # requests per market -- 155 markets already cost 486 requests.
    #
    # A market that was never classified is NOT unresolved. Conflating the two
    # would understate classification quality by counting work never attempted
    # as work that failed, so it gets its own counter and is left out of the
    # classification totals entirely.
    all_tickers = sorted({fill.ticker for fill in fills})
    report.unique_markets_observed = len(all_tickers)
    if max_classify_markets is not None and len(all_tickers) > max_classify_markets:
        tickers = all_tickers[:max_classify_markets]
        report.markets_not_classified = len(all_tickers) - len(tickers)
    else:
        tickers = all_tickers
    report.markets_classified = len(tickers)
    report.fills_requiring_metadata_lookup = sum(
        1 for fill in fills if fill.ticker in set(tickers)
    )

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
        _probe_reconciliation(replay, position_rows, settlement_rows)
        if reconcile and replay is not None
        else None
    )

    # Position authority is decided inside the replay, from the exchange view
    # fed into it above, and the diagnostics derive every authority flag from
    # the episodes themselves. Nothing is set here after the fact: a claim this
    # load-bearing must not depend on an orchestrator remembering to revoke it.

    return AuditResult(
        report=report,
        accounting=accounting,
        coverage=coverage,
        history=history_evidence,
        settlement_coverage=settlement_coverage,
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
    # A complete history and a fill budget are mutually exclusive, so
    # --full-history walks to exhaustion and ignores max_fills entirely rather
    # than making the operator guess a number large enough to be safe.
    yield from client.iter_fills(stats=live, unbounded=True)

    try:
        client.get_historical_cutoff()
        evidence.cutoff_retrieved = True
    except KalshiRouterError:
        evidence.cutoff_retrieved = False

    try:
        yield from client.iter_historical_fills(stats=archive, unbounded=True)
    except KalshiRouterError:
        evidence.historical_failed = True


def _fetch_settlements(
    client: KalshiReadOnlyClient, report: AuditReport
) -> tuple[list[dict], list, SettlementCoverage]:
    """Walk the settlements route once, keeping raw rows, values and reach.

    Same rule as the fill probe: a settlement that cannot be interpreted is left
    out of accounting and counted, never admitted with an invented payout.

    The raw rows are retained so the reconciliation probe can reuse them.  The
    walk used to happen twice -- once to build the replay's settlements and once
    inside the probe -- which doubled the request cost and, worse, allowed the
    two views to disagree if a settlement landed between them.

    A rejected row is a hole in the coverage measurement, not only in the
    accounting: its ``settled_time`` might have been the earliest of all.  So the
    rejects are folded in as unreadable times, which withholds the floor.
    """
    raw_rows: list[dict] = []
    settlements = []
    stats = WalkStats()
    for raw in client.iter_settlements(stats=stats):
        raw_rows.append(raw)
        try:
            settlements.append(normalize_settlement(raw))
        except SchemaError:
            report.settlements_rejected += 1
    report.settlements_fetched = len(settlements) + report.settlements_rejected

    coverage = build_settlement_coverage(
        settlements, exhausted=stats.exhausted, truncated=stats.truncated
    )
    coverage.rows += report.settlements_rejected
    coverage.rows_with_an_unreadable_time += report.settlements_rejected
    return raw_rows, settlements, coverage


def _probe_below_the_floor(
    client: KalshiReadOnlyClient, coverage: SettlementCoverage
) -> None:
    """Ask the route for one settlement older than the walk's earliest row.

    This checks an assumption of this module's own making.  The settlements walk
    ends when its cursor runs out, which is easy to read as "the route gave
    everything".  It only means the route gave everything for the query asked --
    and if the default query carries an implicit window, an exhausted walk and a
    complete one are indistinguishable.

    A row that really is older proves the walk was windowed.  The floor is then
    withheld and nothing is reclassified, because the right response is to
    re-walk with ``min_ts``, not to state a limit that is really a missing
    parameter.

    A route that ignores an unknown parameter answers with its newest rows, so
    every returned row's own timestamp is checked against the floor rather than
    trusted because it arrived.
    """
    floor = coverage.observed_floor
    if floor is None:
        return
    coverage.below_floor_probed = True
    try:
        payload = client.probe_settlements_before(max_ts=int(floor) - 1)
    except HttpStatusError as exc:
        coverage.below_floor_probe_status = exc.status
        return
    except KalshiRouterError:
        return
    coverage.below_floor_probe_status = 200
    rows = payload.get("settlements")
    if not isinstance(rows, list):
        return
    coverage.below_floor_rows_returned = len(rows)
    for row in rows:
        if not isinstance(row, dict):
            continue
        at = parse_rfc3339_seconds(row.get("settled_time"))
        if at is not None and at < floor:
            coverage.below_floor_rows_older_than_the_floor += 1


def _probe_settlement_archive(
    client: KalshiReadOnlyClient, coverage: SettlementCoverage
) -> None:
    """Ask, in one request, whether an archival settlements route exists.

    The fill routes come in a pair: a live one and an archive one that serves
    everything older than the cutoff.  If settlements have the same pair, the
    gap this module bounds can be closed outright instead.  The question costs
    one request, and the answer is recorded either way -- an absent route is a
    finding, not an error, so a failure degrades the measurement rather than the
    audit.
    """
    coverage.archive_route_probed = True
    try:
        payload = client.probe_historical_settlements()
    except HttpStatusError as exc:
        coverage.archive_route_status = exc.status
        return
    except KalshiRouterError:
        return
    coverage.archive_route_available = True
    coverage.archive_route_status = 200
    rows = payload.get("settlements")
    if isinstance(rows, list):
        coverage.archive_first_page_rows = len(rows)


def _markets_outside_settlement_evidence(replay) -> frozenset[str]:
    """Tickers whose every open episode sits below the settlement floor.

    "Every", not "any": a ticker holding one bounded episode and one that the
    settlements route did cover is not bounded -- the covered one is still an
    unexplained open position, and letting the bounded sibling speak for it
    would hide exactly the contradiction this is meant to expose.
    """
    open_episodes: dict[str, list] = {}
    for episode in replay.episodes:
        if episode.is_open:
            open_episodes.setdefault(episode.ticker, []).append(episode)
    return frozenset(
        ticker
        for ticker, episodes in open_episodes.items()
        if all(e.outcome_bounded_by_settlement_coverage for e in episodes)
    )


def _probe_reconciliation(
    replay, position_rows: list[dict], settlement_rows: list[dict]
) -> ReconciliationReport:
    """Measure the replay against the exchange's own position view.

    Opt-in, because it walks another paginated collection and the bounded
    200-fill audit already issues hundreds of requests.  Nothing downstream
    depends on the result: it is a measurement, not a verdict.

    The settlement rows are the ones already walked, not a fresh walk: one view
    of the settlements, used for both the replay and the measurement.
    """
    replayed = {
        ticker: ledger.position
        for (_subaccount, ticker), ledger in replay.ledgers.items()
    }
    return probe_reconciliation(
        position_rows,
        settlement_rows,
        replayed,
        _markets_outside_settlement_evidence(replay),
    )
