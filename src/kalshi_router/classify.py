"""Explainable, fail-closed sport classification for Kalshi markets.

Evidence hierarchy (Phase 0.1)
------------------------------
The first live audit resolved only 28% of fills because its strongest signal was
a hand-guessed series-ticker registry. Kalshi publishes the league itself, so the
hierarchy is now built on that:

===== ============================================================ =============
Level Source                                                        Decides?
===== ============================================================ =============
L1    ``GET /events/{ticker}/metadata`` -> ``competition``           yes
L2    ``GET /search/filters_by_sport`` -> sport owning that          yes
      competition
L3    ``GET /milestones`` -> competition linked to the event ticker  yes
L4    series ``tags`` / ``categories`` / ``category`` / ``title``    yes
L5    exact series-ticker registry (our own table)                   last resort
===== ============================================================ =============

L1 is what finally separates **"Pro Football"** from **"College Football"** -- the
NFL/CFB ambiguity Phase 0 had to refuse 144 times.

Precedence and conflict rules
-----------------------------
* The highest level that produces a verdict wins.
* A **present but unrecognized** competition is fail-closed (``UNRESOLVED``), not a
  licence to fall through to weaker evidence. A *null* competition does fall
  through -- that is the documented shape for a non-sports event.
* A conflict between a competition verdict (L1-L3) and series metadata (L4) is
  ``UNRESOLVED``: both are real Kalshi metadata, so disagreement means we do not
  understand the market.
* The L5 registry is our own unverified table. It never overrides a contradictory
  higher level; it yields, and the disagreement is counted.
* Within L4, a sport *family* with no league distinction ("Football") stays
  ``UNRESOLVED``.

Privacy
-------
:class:`Classification` carries tickers, competition strings and matched tokens so
a decision can be audited. That makes it sensitive when bound to the owner's
fills: it is surfaced only by the local sensitive diagnostic mode, never by
aggregates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Iterable

from .competitions import (
    is_ambiguous_sport,
    normalize,
    sport_from_competition,
    sport_from_taxonomy_sport,
)
from .series_registry import lookup_series_ticker
from .sports import ROUTABLE_SPORTS, Sport

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .milestones import MilestoneIndex
    from .taxonomy import SportTaxonomy


class EvidenceLevel(str, Enum):
    """Ordered strongest to weakest; the numeric order drives precedence."""

    L1_EVENT_COMPETITION = "L1_event_competition"
    L2_SPORT_TAXONOMY = "L2_sport_taxonomy"
    L3_MILESTONE = "L3_milestone"
    L4_SERIES_METADATA = "L4_series_metadata"
    L5_SERIES_REGISTRY = "L5_series_registry"


LEVEL_ORDER: tuple[EvidenceLevel, ...] = (
    EvidenceLevel.L1_EVENT_COMPETITION,
    EvidenceLevel.L2_SPORT_TAXONOMY,
    EvidenceLevel.L3_MILESTONE,
    EvidenceLevel.L4_SERIES_METADATA,
    EvidenceLevel.L5_SERIES_REGISTRY,
)

#: Levels that represent Kalshi's authoritative competition identification.
COMPETITION_LEVELS = frozenset({
    EvidenceLevel.L1_EVENT_COMPETITION,
    EvidenceLevel.L2_SPORT_TAXONOMY,
    EvidenceLevel.L3_MILESTONE,
})


class UnresolvedReason(str, Enum):
    METADATA_LOOKUP_FAILED = "metadata_lookup_failed"
    NO_METADATA = "no_metadata_resolved"
    COMPETITION_ABSENT = "competition_absent"
    COMPETITION_UNKNOWN = "competition_unknown"
    EVIDENCE_CONFLICT = "evidence_conflict"
    AMBIGUOUS_FAMILY = "ambiguous_sport_family_without_league"
    INSUFFICIENT = "insufficient_authoritative_metadata"


class EvidenceStrength(str, Enum):
    AUTHORITATIVE = "authoritative"
    SUPPORTING = "supporting"


@dataclass(frozen=True)
class Evidence:
    """One matched signal behind a classification."""

    source: str
    strength: EvidenceStrength
    matched_token: str
    sport: Sport | None = None
    ambiguous_family: str | None = None
    out_of_scope_kind: str | None = None
    level: EvidenceLevel | None = None


@dataclass(frozen=True)
class Verdict:
    """A decisive (or explicitly fail-closed) outcome from one evidence level."""

    level: EvidenceLevel
    sport: Sport | None
    detail: str
    unresolved_reason: UnresolvedReason | None = None


@dataclass(frozen=True)
class MarketContext:
    """Resolved public metadata for one market ticker."""

    market_ticker: str
    market: dict[str, Any] | None = None
    event: dict[str, Any] | None = None
    series: dict[str, Any] | None = None
    event_metadata: dict[str, Any] | None = None
    lookup_error: str | None = None
    event_metadata_error: str | None = None

    @property
    def competition(self) -> str | None:
        value = (self.event_metadata or {}).get("competition")
        return value if isinstance(value, str) and value.strip() else None

    @property
    def competition_scope(self) -> str | None:
        value = (self.event_metadata or {}).get("competition_scope")
        return value if isinstance(value, str) and value.strip() else None


@dataclass(frozen=True)
class Classification:
    """An explainable classification decision for one market."""

    sport: Sport
    reason: str
    market_ticker: str
    evidence: tuple[Evidence, ...] = ()
    event_ticker: str | None = None
    series_ticker: str | None = None
    competition: str | None = None
    competition_scope: str | None = None
    resolved_by: EvidenceLevel | None = None
    unresolved_reason: UnresolvedReason | None = None
    used_unverified_series_ticker: bool = False
    #: A weaker level disagreed with the winning one and was overruled.
    lower_level_conflict: bool = False

    @property
    def is_routable(self) -> bool:
        return self.sport in ROUTABLE_SPORTS


# ------------------------------------------------------ L4 series token tables

LEAGUE_TOKENS: dict[Sport, tuple[str, ...]] = {
    Sport.MLB: ("mlb", "major league baseball", "world series"),
    Sport.NFL: ("nfl", "national football league", "super bowl"),
    Sport.CFB: ("cfb", "ncaaf", "ncaa football", "college football", "college football playoff"),
    Sport.TENNIS: ("tennis", "atp", "wta"),
}

AMBIGUOUS_FAMILY_TOKENS: tuple[str, ...] = (
    "football", "american football", "baseball", "college sports", "ncaa",
)

NON_TARGET_SPORT_TOKENS: tuple[str, ...] = (
    "basketball", "nba", "wnba", "ncaab", "college basketball",
    "soccer", "football club", "premier league", "epl", "uefa", "mls", "la liga",
    "bundesliga", "serie a", "ligue 1", "world cup",
    "hockey", "nhl",
    "golf", "pga", "liv golf", "masters tournament",
    "mma", "ufc", "boxing",
    "cricket", "rugby", "esports", "league of legends", "counter-strike", "dota",
    "motorsport", "formula 1", "nascar", "f1",
    "olympics", "chess", "darts", "cycling", "auto racing",
)

NON_SPORTS_CATEGORY_TOKENS: tuple[str, ...] = (
    "politics", "elections", "economics", "financials", "financial markets",
    "climate", "weather", "entertainment", "science and technology", "technology",
    "health", "world", "companies", "crypto", "cryptocurrency", "transportation",
    "culture", "media", "awards", "inflation", "fed", "commodities",
)


def _contains(text: str, token: str) -> bool:
    if not text or not token:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text) is not None


def _iter_texts(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        text = normalize(value)
        if text:
            yield text
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str):
                text = normalize(item)
                if text:
                    yield text
            elif isinstance(item, dict):
                for key in ("name", "title", "label"):
                    text = normalize(item.get(key))
                    if text:
                        yield text
                        break


def _scan_text(text: str, source: str, strength: EvidenceStrength) -> list[Evidence]:
    found: list[Evidence] = []
    level = EvidenceLevel.L4_SERIES_METADATA if strength is EvidenceStrength.AUTHORITATIVE else None
    for sport, tokens in LEAGUE_TOKENS.items():
        for token in tokens:
            if _contains(text, token):
                found.append(Evidence(source, strength, token, sport=sport, level=level))
                break
    for token in NON_TARGET_SPORT_TOKENS:
        if _contains(text, token):
            found.append(Evidence(source, strength, token, sport=Sport.OTHER,
                                  out_of_scope_kind="sport", level=level))
            break
    for token in NON_SPORTS_CATEGORY_TOKENS:
        if _contains(text, token):
            found.append(Evidence(source, strength, token, sport=Sport.OTHER,
                                  out_of_scope_kind="category", level=level))
            break
    for token in AMBIGUOUS_FAMILY_TOKENS:
        if _contains(text, token):
            found.append(Evidence(source, strength, token, ambiguous_family=token, level=level))
            break
    return found


def _get(mapping: dict[str, Any] | None, key: str) -> Any:
    return mapping.get(key) if isinstance(mapping, dict) else None


def derive_series_ticker(context: MarketContext) -> tuple[str | None, str]:
    """Resolve the series ticker and say where it came from."""
    for mapping, key, source in (
        (context.series, "ticker", "series.ticker"),
        (context.event, "series_ticker", "event.series_ticker"),
        (context.market, "series_ticker", "market.series_ticker"),
    ):
        value = _get(mapping, key)
        if isinstance(value, str) and value.strip():
            return value.strip(), source
    if context.market_ticker and "-" in context.market_ticker:
        return context.market_ticker.split("-", 1)[0].strip(), "market_ticker.series_prefix"
    return None, "none"


# ------------------------------------------------------------- level verdicts

def _competition_verdict(
    context: MarketContext,
    taxonomy: "SportTaxonomy | None",
    evidence: list[Evidence],
) -> Verdict | None:
    """L1/L2: resolve from the event's own competition, helped by the taxonomy."""
    competition = context.competition
    if competition is None:
        return None

    evidence.append(
        Evidence("event_metadata.competition", EvidenceStrength.AUTHORITATIVE,
                 competition, level=EvidenceLevel.L1_EVENT_COMPETITION)
    )
    if context.competition_scope:
        evidence.append(
            Evidence("event_metadata.competition_scope", EvidenceStrength.SUPPORTING,
                     context.competition_scope, level=EvidenceLevel.L1_EVENT_COMPETITION)
        )

    direct = sport_from_competition(competition)
    if direct is not None:
        return Verdict(EvidenceLevel.L1_EVENT_COMPETITION, direct, f"competition={competition!r}")

    sport_name = taxonomy.sport_for_competition(competition) if taxonomy else None
    if sport_name:
        evidence.append(
            Evidence("taxonomy.sport", EvidenceStrength.AUTHORITATIVE, sport_name,
                     level=EvidenceLevel.L2_SPORT_TAXONOMY)
        )
        mapped = sport_from_taxonomy_sport(sport_name)
        if mapped is not None:
            return Verdict(EvidenceLevel.L2_SPORT_TAXONOMY, mapped,
                           f"taxonomy sport={sport_name!r}")
        if is_ambiguous_sport(sport_name):
            # e.g. a new Football competition we do not recognize: refusing is
            # the whole point -- this is where a guess would put a college game
            # into the NFL ledger.
            return Verdict(EvidenceLevel.L2_SPORT_TAXONOMY, None,
                           f"unrecognized competition within ambiguous sport {sport_name!r}",
                           UnresolvedReason.COMPETITION_UNKNOWN)
        return Verdict(EvidenceLevel.L2_SPORT_TAXONOMY, None,
                       f"unrecognized sport {sport_name!r}",
                       UnresolvedReason.COMPETITION_UNKNOWN)

    return Verdict(EvidenceLevel.L1_EVENT_COMPETITION, None,
                   f"unrecognized competition {competition!r}",
                   UnresolvedReason.COMPETITION_UNKNOWN)


def _milestone_verdict(
    context: MarketContext,
    milestone_index: "MilestoneIndex | None",
    event_ticker: str | None,
    evidence: list[Evidence],
) -> Verdict | None:
    """L3: the event ticker appears in a public milestone for a competition."""
    if milestone_index is None or not event_ticker:
        return None
    competition = milestone_index.competition_for_event(event_ticker)
    if not competition:
        return None
    evidence.append(
        Evidence("milestone.competition", EvidenceStrength.AUTHORITATIVE, competition,
                 level=EvidenceLevel.L3_MILESTONE)
    )
    mapped = sport_from_competition(competition)
    if mapped is None:
        return None
    return Verdict(EvidenceLevel.L3_MILESTONE, mapped, f"milestone competition={competition!r}")


def _series_metadata_verdict(context: MarketContext, evidence: list[Evidence]) -> Verdict | None:
    """L4: Kalshi series tags / categories / title."""
    A = EvidenceStrength.AUTHORITATIVE
    S = EvidenceStrength.SUPPORTING

    authoritative_fields = (
        (context.series, "tags", "series.tags"),
        (context.series, "categories", "series.categories"),
        (context.series, "category", "series.category"),
        (context.series, "title", "series.title"),
        (context.event, "category", "event.category"),
        (context.market, "category", "market.category"),
    )
    found: list[Evidence] = []
    for mapping, key, source in authoritative_fields:
        for text in _iter_texts(_get(mapping, key)):
            found.extend(_scan_text(text, source, A))

    for mapping, key, source in (
        (context.event, "title", "event.title"),
        (context.event, "sub_title", "event.sub_title"),
        (context.market, "title", "market.title"),
        (context.market, "subtitle", "market.subtitle"),
        (context.market, "yes_sub_title", "market.yes_sub_title"),
    ):
        for text in _iter_texts(_get(mapping, key)):
            evidence.extend(_scan_text(text, source, S))

    evidence.extend(found)

    leagues = {e.sport for e in found if e.sport in ROUTABLE_SPORTS}
    if len(leagues) > 1:
        names = ", ".join(sorted(s.value for s in leagues))
        return Verdict(EvidenceLevel.L4_SERIES_METADATA, None,
                       f"series metadata named {names}", UnresolvedReason.EVIDENCE_CONFLICT)
    if len(leagues) == 1:
        sport = next(iter(leagues))
        sources = ", ".join(sorted({e.source for e in found if e.sport is sport}))
        return Verdict(EvidenceLevel.L4_SERIES_METADATA, sport, sources)

    rivals = [e for e in found if e.sport is Sport.OTHER and e.out_of_scope_kind == "sport"]
    if rivals:
        sources = ", ".join(sorted({e.source for e in rivals}))
        return Verdict(EvidenceLevel.L4_SERIES_METADATA, Sport.OTHER, sources)

    ambiguous = [e for e in found if e.ambiguous_family]
    if ambiguous:
        families = ", ".join(sorted({e.ambiguous_family or "" for e in ambiguous}))
        return Verdict(EvidenceLevel.L4_SERIES_METADATA, None, families,
                       UnresolvedReason.AMBIGUOUS_FAMILY)

    non_sports = [e for e in found if e.sport is Sport.OTHER and e.out_of_scope_kind == "category"]
    if non_sports:
        sources = ", ".join(sorted({e.source for e in non_sports}))
        return Verdict(EvidenceLevel.L4_SERIES_METADATA, Sport.OTHER, sources)
    return None


def _registry_verdict(
    series_ticker: str | None, ticker_source: str, evidence: list[Evidence]
) -> tuple[Verdict | None, bool]:
    """L5: our own exact series-ticker table. Last resort, never an override."""
    entry = lookup_series_ticker(series_ticker)
    if entry is None:
        return None, False
    evidence.append(
        Evidence(f"series_registry[{ticker_source}]", EvidenceStrength.AUTHORITATIVE,
                 (series_ticker or "").upper(), sport=entry.sport,
                 level=EvidenceLevel.L5_SERIES_REGISTRY)
    )
    return Verdict(EvidenceLevel.L5_SERIES_REGISTRY, entry.sport,
                   f"registry[{series_ticker}]"), not entry.verified


# ---------------------------------------------------------------- entry point

def classify_market(
    context: MarketContext,
    taxonomy: "SportTaxonomy | None" = None,
    milestone_index: "MilestoneIndex | None" = None,
) -> Classification:
    """Classify one market against the Phase 0.1 hierarchy, failing closed."""
    event_ticker = _get(context.event, "event_ticker") or _get(context.market, "event_ticker")
    if not isinstance(event_ticker, str):
        event_ticker = None

    def build(
        sport: Sport,
        reason: str,
        evidence: list[Evidence],
        series_ticker: str | None = None,
        resolved_by: EvidenceLevel | None = None,
        unresolved_reason: UnresolvedReason | None = None,
        unverified: bool = False,
        conflict: bool = False,
    ) -> Classification:
        return Classification(
            sport=sport,
            reason=reason,
            market_ticker=context.market_ticker,
            evidence=tuple(evidence),
            event_ticker=event_ticker,
            series_ticker=series_ticker,
            competition=context.competition,
            competition_scope=context.competition_scope,
            resolved_by=resolved_by,
            unresolved_reason=unresolved_reason,
            used_unverified_series_ticker=unverified,
            lower_level_conflict=conflict,
        )

    if context.lookup_error:
        return build(Sport.UNRESOLVED, f"metadata_lookup_failed: {context.lookup_error}", [],
                     unresolved_reason=UnresolvedReason.METADATA_LOOKUP_FAILED)

    if not any(isinstance(m, dict) for m in
               (context.market, context.event, context.series, context.event_metadata)):
        return build(Sport.UNRESOLVED, "no_metadata_resolved", [],
                     unresolved_reason=UnresolvedReason.NO_METADATA)

    evidence: list[Evidence] = []
    series_ticker, ticker_source = derive_series_ticker(context)

    competition_verdict = _competition_verdict(context, taxonomy, evidence)
    milestone = (
        _milestone_verdict(context, milestone_index, event_ticker, evidence)
        if competition_verdict is None
        else None
    )
    series_verdict = _series_metadata_verdict(context, evidence)
    registry_verdict, used_unverified = _registry_verdict(series_ticker, ticker_source, evidence)

    # A present-but-unrecognized competition is terminal: falling through to a
    # weaker level here is exactly how a guessed registry would overrule Kalshi.
    if competition_verdict is not None and competition_verdict.unresolved_reason is not None:
        return build(Sport.UNRESOLVED,
                     f"{competition_verdict.unresolved_reason.value}: {competition_verdict.detail}",
                     evidence, series_ticker,
                     unresolved_reason=competition_verdict.unresolved_reason,
                     unverified=used_unverified)

    verdicts = [v for v in (competition_verdict, milestone, series_verdict, registry_verdict)
                if v is not None]
    decisive = [v for v in verdicts if v.sport is not None]

    if not decisive:
        blocking = next((v for v in verdicts if v.unresolved_reason is not None), None)
        if blocking is not None:
            return build(Sport.UNRESOLVED, f"{blocking.unresolved_reason.value}: {blocking.detail}",
                         evidence, series_ticker,
                         unresolved_reason=blocking.unresolved_reason, unverified=used_unverified)
        reason = (UnresolvedReason.COMPETITION_ABSENT
                  if context.event_metadata is not None and context.competition is None
                  else UnresolvedReason.INSUFFICIENT)
        return build(Sport.UNRESOLVED, reason.value, evidence, series_ticker,
                     unresolved_reason=reason, unverified=used_unverified)

    decisive.sort(key=lambda v: LEVEL_ORDER.index(v.level))
    winner = decisive[0]

    # Two pieces of real Kalshi metadata disagreeing means we do not understand
    # this market. The registry is ours, not Kalshi's, so it only ever yields.
    conflict = False
    for other in decisive[1:]:
        if other.sport == winner.sport:
            continue
        if other.level is EvidenceLevel.L5_SERIES_REGISTRY:
            conflict = True
            continue
        if winner.level in COMPETITION_LEVELS or other.level in COMPETITION_LEVELS:
            return build(
                Sport.UNRESOLVED,
                f"evidence_conflict: {winner.level.value}={winner.sport.value} vs "
                f"{other.level.value}={other.sport.value}",
                evidence, series_ticker,
                unresolved_reason=UnresolvedReason.EVIDENCE_CONFLICT,
                unverified=used_unverified,
            )
        conflict = True

    return build(winner.sport, f"{winner.level.value}: {winner.detail}", evidence, series_ticker,
                 resolved_by=winner.level, unverified=used_unverified, conflict=conflict)
