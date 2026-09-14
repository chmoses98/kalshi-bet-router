"""Privacy-safe aggregate reporting.

Everything this module can emit is a **count**.  There is no code path that
renders a ticker, a competition string, a fill id, an order id, a price, a
contract count, a monetary value, or a market title.  That is enforced by
construction -- :class:`AuditReport` has no field capable of holding a string --
and by ``tests/test_privacy.py``, which scans rendered output for fixture secrets.

Phase 0.1 adds instrumentation that explains *why* classification succeeded or
failed, still without naming anything the owner traded: counts of events whose
competition was present, counts resolved at each evidence level, and counts of
each fail-closed reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .classify import EvidenceLevel
from .sports import REPORT_ORDER, Sport

#: Stable ordering for per-level reporting.
LEVEL_REPORT_ORDER: tuple[EvidenceLevel, ...] = (
    EvidenceLevel.L1_EVENT_COMPETITION,
    EvidenceLevel.L2_SPORT_TAXONOMY,
    EvidenceLevel.L3_MILESTONE,
    EvidenceLevel.L4_SERIES_METADATA,
    EvidenceLevel.L5_SERIES_REGISTRY,
)


def _percent(part: int, whole: int) -> str:
    return "n/a" if whole <= 0 else f"{100.0 * part / whole:.1f}%"


@dataclass
class AuditReport:
    """Counts only.  Safe to print in a public Actions log."""

    fills_fetched: int = 0
    unique_fills: int = 0
    duplicate_fill_ids_observed: int = 0

    classification_counts: dict[Sport, int] = field(
        default_factory=lambda: {sport: 0 for sport in REPORT_ORDER}
    )
    classification_failures: int = 0

    # --- resolution by evidence level (per fill, comparable to the counts above)
    fills_resolved_by_level: dict[EvidenceLevel, int] = field(
        default_factory=lambda: {level: 0 for level in LEVEL_REPORT_ORDER}
    )
    markets_resolved_by_level: dict[EvidenceLevel, int] = field(
        default_factory=lambda: {level: 0 for level in LEVEL_REPORT_ORDER}
    )

    # --- why classification failed (per fill)
    unresolved_competition_absent: int = 0
    unresolved_competition_unknown: int = 0
    unresolved_competition_ambiguous: int = 0
    unresolved_milestone_conflict: int = 0
    unresolved_malformed_event_metadata: int = 0
    unresolved_evidence_conflict: int = 0
    unresolved_ambiguous_family: int = 0
    unresolved_metadata_lookup_failed: int = 0
    unresolved_insufficient_metadata: int = 0
    lower_level_conflicts_overruled: int = 0

    # --- metadata coverage
    fills_requiring_metadata_lookup: int = 0
    unique_markets_observed: int = 0
    unique_events_observed: int = 0
    events_with_metadata_retrieved: int = 0
    events_with_competition: int = 0
    events_with_competition_scope: int = 0
    events_with_malformed_metadata: int = 0
    event_metadata_lookup_failures: int = 0
    metadata_lookup_failures: int = 0
    metadata_partial_failures: int = 0
    metadata_cache_hits: int = 0

    # --- public taxonomy
    taxonomy_available: bool = False
    taxonomy_sports: int = 0
    taxonomy_competitions: int = 0
    taxonomy_competition_collisions: int = 0
    taxonomy_skipped_sports: int = 0

    # --- public milestone backstop
    milestone_index_built: bool = False
    milestone_requests_issued: int = 0
    milestone_events_indexed: int = 0
    milestone_event_conflicts: int = 0
    milestone_fetch_failed: bool = False
    milestone_budget_exhausted: bool = False

    # --- fill shape
    buy_fills: int = 0
    sell_fills: int = 0
    yes_side_fills: int = 0
    no_side_fills: int = 0
    orders_observed: int = 0
    partial_order_groups: int = 0
    fills_without_an_order_id: int = 0

    # --- fixed-point schema observations
    fills_quantity_from_count_fp: int = 0
    fills_quantity_from_legacy_count: int = 0
    fills_price_from_dollars: int = 0
    fills_price_from_legacy_cents: int = 0
    fills_without_price: int = 0
    fills_with_number_typed_fixed_point: int = 0

    classifications_using_unverified_series_ticker: int = 0
    api_requests: int = 0

    @property
    def account_has_no_fills(self) -> bool:
        """An empty account is a valid success state, not a failure."""
        return self.fills_fetched == 0

    @property
    def supported_sport_count(self) -> int:
        return sum(self.classification_counts.get(s, 0)
                   for s in (Sport.MLB, Sport.NFL, Sport.CFB, Sport.TENNIS))

    @property
    def unresolved_count(self) -> int:
        return self.classification_counts.get(Sport.UNRESOLVED, 0)

    def as_dict(self) -> dict[str, int | bool]:
        data: dict[str, int | bool] = {}
        for name, value in vars(self).items():
            if name in ("classification_counts", "fills_resolved_by_level",
                        "markets_resolved_by_level"):
                continue
            data[name] = value
        data["account_has_no_fills"] = self.account_has_no_fills
        data["supported_sport_count"] = self.supported_sport_count
        for sport in REPORT_ORDER:
            data[f"classification_{sport.value}"] = self.classification_counts.get(sport, 0)
        for level in LEVEL_REPORT_ORDER:
            data[f"fills_resolved_{level.value}"] = self.fills_resolved_by_level.get(level, 0)
            data[f"markets_resolved_{level.value}"] = self.markets_resolved_by_level.get(level, 0)
        return data

    def render(self) -> str:
        """Render the aggregate block printed by the public audit."""
        total = self.unique_fills
        lines = [
            "Kalshi Phase 0.1 read-only audit -- aggregate diagnostics",
            "=========================================================",
            f"fills fetched: {self.fills_fetched}",
            f"unique fills: {self.unique_fills}",
            f"duplicate fill IDs observed: {self.duplicate_fill_ids_observed}",
            "",
            "classification:",
        ]
        for sport in REPORT_ORDER:
            count = self.classification_counts.get(sport, 0)
            lines.append(f"  {sport.value}: {count} ({_percent(count, total)})")
        lines += [
            f"  supported-sport total: {self.supported_sport_count} "
            f"({_percent(self.supported_sport_count, total)})",
            "",
            "resolved by evidence level (fills):",
        ]
        for level in LEVEL_REPORT_ORDER:
            count = self.fills_resolved_by_level.get(level, 0)
            lines.append(f"  {level.value}: {count} ({_percent(count, total)})")
        lines += [
            "",
            "resolved by evidence level (unique markets):",
        ]
        for level in LEVEL_REPORT_ORDER:
            lines.append(
                f"  {level.value}: {self.markets_resolved_by_level.get(level, 0)}"
            )
        lines += [
            "",
            "unresolved reasons (fills):",
            f"  competition absent: {self.unresolved_competition_absent}",
            f"  competition unknown (fail-closed): {self.unresolved_competition_unknown}",
            f"  competition ambiguous in taxonomy: {self.unresolved_competition_ambiguous}",
            f"  milestone competition conflict: {self.unresolved_milestone_conflict}",
            f"  malformed event metadata: {self.unresolved_malformed_event_metadata}",
            f"  authoritative evidence conflict: {self.unresolved_evidence_conflict}",
            f"  ambiguous sport family: {self.unresolved_ambiguous_family}",
            f"  metadata lookup failed: {self.unresolved_metadata_lookup_failed}",
            f"  insufficient metadata: {self.unresolved_insufficient_metadata}",
            f"  weaker evidence overruled by stronger: {self.lower_level_conflicts_overruled}",
            "",
            "metadata coverage:",
            f"  fills requiring additional metadata lookup: {self.fills_requiring_metadata_lookup}",
            f"  unique markets observed: {self.unique_markets_observed}",
            f"  unique events observed: {self.unique_events_observed}",
            f"  events with metadata retrieved: {self.events_with_metadata_retrieved}",
            f"  events with non-null competition: {self.events_with_competition}",
            f"  events with non-null competition_scope: {self.events_with_competition_scope}",
            f"  events with malformed metadata: {self.events_with_malformed_metadata}",
            f"  event metadata lookup failures: {self.event_metadata_lookup_failures}",
            f"  market lookup failures: {self.metadata_lookup_failures}",
            f"  partial lookup failures: {self.metadata_partial_failures}",
            f"  metadata cache hits: {self.metadata_cache_hits}",
            f"  classification failures: {self.classification_failures}",
            "",
            "public sport taxonomy:",
            f"  taxonomy available: {self.taxonomy_available}",
            f"  sports in taxonomy: {self.taxonomy_sports}",
            f"  competitions in taxonomy: {self.taxonomy_competitions}",
            f"  competitions claimed by >1 sport (fail-closed): "
            f"{self.taxonomy_competition_collisions}",
            f"  taxonomy entries skipped: {self.taxonomy_skipped_sports}",
            "",
            "public milestone backstop:",
            f"  milestone index built: {self.milestone_index_built}",
            f"  milestone requests issued: {self.milestone_requests_issued}",
            f"  milestone event links indexed: {self.milestone_events_indexed}",
            f"  events under >1 competition (fail-closed): {self.milestone_event_conflicts}",
            f"  milestone fetch failed: {self.milestone_fetch_failed}",
            f"  milestone budget exhausted: {self.milestone_budget_exhausted}",
            "",
            f"buy fills: {self.buy_fills}",
            f"sell fills: {self.sell_fills}",
            f"YES-side fills: {self.yes_side_fills}",
            f"NO-side fills: {self.no_side_fills}",
            "",
            f"orders observed: {self.orders_observed}",
            f"partial-order groups (orders with >1 fill): {self.partial_order_groups}",
            f"fills without an order id: {self.fills_without_an_order_id}",
            "",
            "fixed-point schema observations:",
            f"  quantity from count_fp: {self.fills_quantity_from_count_fp}",
            f"  quantity from legacy count: {self.fills_quantity_from_legacy_count}",
            f"  price from *_price_dollars: {self.fills_price_from_dollars}",
            f"  price from legacy integer cents: {self.fills_price_from_legacy_cents}",
            f"  fills with no interpretable price: {self.fills_without_price}",
            f"  fixed-point fields arriving as JSON numbers: "
            f"{self.fills_with_number_typed_fixed_point}",
            "",
            f"classifications relying on an unverified series ticker: "
            f"{self.classifications_using_unverified_series_ticker}",
            f"API requests issued: {self.api_requests}",
        ]
        if self.account_has_no_fills:
            lines += [
                "",
                "NOTE: the API responded successfully and the account returned no fills",
                "      in the sampled window. This is a valid empty state, not a failure.",
            ]
        return "\n".join(lines)
