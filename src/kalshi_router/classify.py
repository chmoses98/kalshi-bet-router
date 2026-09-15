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
    possible_sports,
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
    MALFORMED_EVENT_METADATA = "malformed_event_metadata"
    COMPETITION_ABSENT = "competition_absent"
    COMPETITION_UNKNOWN = "competition_unknown"
    COMPETITION_AMBIGUOUS = "competition_ambiguous_in_taxonomy"
    MILESTONE_CONFLICT = "milestone_competition_conflict"
    EVIDENCE_CONFLICT = "evidence_conflict"
    AMBIGUOUS_FAMILY = "ambiguous_sport_family_without_league"
    INSUFFICIENT = "insufficient_authoritative_metadata"


#: Fields of the event-metadata document this classifier reads.  Kalshi documents
#: both as ``string | null``.
EVENT_METADATA_STRING_FIELDS = ("competition", "competition_scope")


def validate_metadata_string_field(
    metadata: dict[str, Any] | None, key: str
) -> tuple[str | None, str | None]:
    """Validate one ``string | null`` event-metadata field.

    Returns ``(value, error)``.

    The governing rule is that **only an explicit null or absence may fall
    through** to weaker evidence.  A field that is present but unusable is
    malformed metadata, and a present-but-unusable authoritative field must never
    quietly demote itself into weaker evidence.

    * **absent field** -> ``(None, None)``.  Valid absence.
    * **JSON null** -> ``(None, None)``.  Valid absence: most events are not
      sports, and the classifier falls through to weaker evidence.
    * **non-empty string** -> ``(value, None)``.  Valid value.
    * **empty string** -> ``(None, error)``.  Malformed: the field is present and
      claims to carry a competition, but carries nothing usable.
    * **whitespace-only string** -> ``(None, error)``.  Malformed, as above.
    * **any other non-null type** (int, float, bool, list, dict, ...) ->
      ``(None, error)``.  Malformed.  Never coerced.

    Every ``error`` case must fail the classification closed.  The error text
    names the field and the shape problem only -- never the offending value,
    which is private account data and reaches a public log.
    """
    if not isinstance(metadata, dict) or key not in metadata:
        return None, None
    raw = metadata[key]
    if raw is None:
        return None, None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None, (
                f"{key} was present but empty, expected a non-empty string or null"
            )
        return text, None
    return None, f"{key} was {type(raw).__name__}, expected string or null"


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
    def metadata_schema_error(self) -> str | None:
        """A non-secret description of malformed event metadata, if any.

        Checked before any evidence is gathered, so a wrong-typed ``competition``
        cannot be quietly rescued by series metadata or by the ticker registry.
        """
        for key in EVENT_METADATA_STRING_FIELDS:
            _, error = validate_metadata_string_field(self.event_metadata, key)
            if error:
                return error
        return None

    @property
    def competition(self) -> str | None:
        value, error = validate_metadata_string_field(self.event_metadata, "competition")
        return None if error else value

    @property
    def competition_scope(self) -> str | None:
        value, error = validate_metadata_string_field(self.event_metadata, "competition_scope")
        return None if error else value


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
    #: MEASUREMENT ONLY. When a terminal L1/L2 refusal fired, the level that
    #: WOULD have decided had the classifier been allowed to fall through --
    #: or ``None`` if nothing would have. It never changes the verdict; it
    #: exists so the cost of the terminal rule can be counted before anyone
    #: argues about relaxing it.
    terminal_rescuable_by: EvidenceLevel | None = None
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

    # A competition claimed by several sports in Kalshi's own taxonomy cannot be
    # resolved by anyone -- not even by our direct rules, which would otherwise
    # quietly disagree with the exchange's catalogue.
    if taxonomy is not None and taxonomy.is_ambiguous_competition(competition):
        return Verdict(EvidenceLevel.L2_SPORT_TAXONOMY, None,
                       "competition is claimed by more than one sport",
                       UnresolvedReason.COMPETITION_AMBIGUOUS)

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
    if milestone_index.is_conflicted(event_ticker):
        return Verdict(EvidenceLevel.L3_MILESTONE, None,
                       "event appears under more than one competition",
                       UnresolvedReason.MILESTONE_CONFLICT)
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
        rescuable_by: EvidenceLevel | None = None,
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
            terminal_rescuable_by=rescuable_by,
        )

    if context.lookup_error:
        return build(Sport.UNRESOLVED, f"metadata_lookup_failed: {context.lookup_error}", [],
                     unresolved_reason=UnresolvedReason.METADATA_LOOKUP_FAILED)

    schema_error = context.metadata_schema_error
    if schema_error:
        # Deliberately returned before any evidence is collected: malformed
        # event metadata must not be rescued by L4 series metadata or by the L5
        # registry.
        return build(Sport.UNRESOLVED,
                     f"{UnresolvedReason.MALFORMED_EVENT_METADATA.value}: {schema_error}", [],
                     unresolved_reason=UnresolvedReason.MALFORMED_EVENT_METADATA)

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
    for terminal in (competition_verdict, milestone):
        if terminal is not None and terminal.unresolved_reason is not None:
            # Record what a fall-through WOULD have decided, without deciding
            # it. The terminal rule is deliberate, and the argument for or
            # against relaxing it should be made against a measured cost rather
            # than an impression -- L4 is Kalshi's own series metadata, while
            # L5 is this project's registry, and those are not the same kind of
            # evidence to fall back on.
            rescuable = next(
                (v.level for v in (series_verdict, registry_verdict)
                 if v is not None and v.sport is not None),
                None,
            )
            return build(Sport.UNRESOLVED,
                         f"{terminal.unresolved_reason.value}: {terminal.detail}",
                         evidence, series_ticker,
                         unresolved_reason=terminal.unresolved_reason,
                         unverified=used_unverified,
                         rescuable_by=rescuable)

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


# ---------------------------------------------- measuring the L2 ambiguity gate
#
# The live run of 2026-09-15 refused 649 markets as COMPETITION_AMBIGUOUS and
# classified ZERO of the account's episodes as MLB, in an account whose
# destination ledger holds 457 MLB bets. So the ambiguity gate is not a rare
# edge; it is the dominant outcome, and the system cannot record the wagers it
# exists to record.
#
# That is a reason to MEASURE the gate, not to lower it. The gate is there
# because a competition claimed by two sports in Kalshi's own catalogue is one
# our local rules must not quietly overrule. But "two sports claim this name"
# and "two sports disagree about which game this is" are different statements,
# and only the second justifies refusing. A collision between a league and its
# own parent category is the first kind.
#
# Nothing below changes a verdict. It reports the SHAPE of the collisions so
# the difference can be established from evidence instead of argued about.


@dataclass
class CollisionStructure:
    """Counts only. What KIND of ambiguity the taxonomy's collisions are.

    The taxonomy's top level is SPORTS ("Baseball", "Football"), and our four
    are leagues inside them. So a collision is two sports claiming one
    competition name, and the question that matters is whether those two sports
    could contain DIFFERENT ones of our four.
    """

    collisions: int = 0

    #: The claimant sports could contain two or more DIFFERENT ones of our four.
    #: A real conflict: resolving it would mean choosing on our own say-so.
    conflicting_routable_sports: int = 0
    #: Every claimant that narrows anything narrows to the SAME one of our four.
    #: Nominal: the catalogue lists the name under two sports, it does not
    #: disagree about which of our leagues it could be.
    one_routable_claimant: int = 0
    #: No claimant could contain any of our four. Refusing costs nothing.
    no_routable_claimant: int = 0
    #: At least one claimant is a sport we have never heard of, so we cannot say
    #: what it narrows to. Counted apart from a real conflict, because "unknown"
    #: and "contradicted" are different and only one of them is evidence.
    unknown_claimant: int = 0

    #: Of the collisions, how many the DIRECT competition rule would decide on
    #: its own. This prices the current ordering: the gate runs BEFORE the
    #: direct rule, so every one of these is a market the router could name and
    #: refuses to.
    direct_rule_would_decide: int = 0
    #: ...and the claimant sports could contain exactly that answer. The gate is
    #: discarding a resolvable answer the catalogue does not contradict.
    direct_rule_agrees_with_claimant: int = 0
    #: ...and no claimant could contain that answer. A non-zero value here is
    #: the case FOR the gate, stated in its own terms.
    direct_rule_contradicts_claimant: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(vars(self))

    def render(self) -> str:
        return "\n".join([
            "taxonomy competition collisions (counts only; no verdict changes):",
            f"  collisions: {self.collisions}",
            f"    claimant sports could contain DIFFERENT ones of our four: "
            f"{self.conflicting_routable_sports}",
            f"    all claimants narrow to the same one (nominal): "
            f"{self.one_routable_claimant}",
            f"    no claimant could contain any of our four: "
            f"{self.no_routable_claimant}",
            f"    a claimant sport we have never heard of: {self.unknown_claimant}",
            "",
            f"  the direct competition rule would decide: "
            f"{self.direct_rule_would_decide}",
            f"    and the claimants could contain that answer: "
            f"{self.direct_rule_agrees_with_claimant}",
            f"    and no claimant could (the case FOR the gate): "
            f"{self.direct_rule_contradicts_claimant}",
        ])


@dataclass(frozen=True)
class CollisionDetail:
    """One collision, named -- for the human-readable probe only.

    KEPT OUT OF ``CollisionStructure`` ON PURPOSE. That object is asserted to be
    structurally counts-only, and its ``as_dict()`` feeds the JSON audit
    payload. Adding names to it would have broken both, and the fix would have
    been to relax a privacy guard in order to print more -- which is backwards.
    So the names live here, in a separate value that only the probe's text
    output consumes.

    What is named is PUBLIC CATALOGUE DATA: a competition name and the sport
    headings it appears under, from ``GET /search/filters_by_sport``. That is
    the exchange's own catalogue, the same class of fact the series probe
    already prints as ``observed:``. Nothing here comes from the owner's
    account -- no ticker, no order, no count of anything he did. The counts-only
    rule exists to protect HIS data; a public competition name is not his data.

    It has to be named to be acted on. "One collision, nominal" cannot be
    checked by anyone, and cannot say WHICH sport it narrows to -- so a reader
    hoping for one sport can read it as being about that sport when it is
    about another. Naming it is what stops the count standing in for an answer
    it does not contain.
    """

    competition: str
    claimant_sports: tuple[str, ...]
    #: What each claimant narrows to, positionally matching claimant_sports.
    narrows_to: tuple[str, ...]
    #: What the direct competition rule would answer, if anything.
    direct_rule_says: str | None
    #: Whether a claimant could contain that answer; None when unknowable.
    claimants_could_contain_it: bool | None


def describe_competition_collisions(taxonomy) -> tuple[CollisionDetail, ...]:
    """Name each collision. Changes no verdict, exactly as the measurement does not."""
    if taxonomy is None:
        return ()
    details: list[CollisionDetail] = []
    for competition, claimants in sorted(
        getattr(taxonomy, "ambiguous_claimants", {}).items()
    ):
        claimant_names = tuple(sorted(claimants))
        narrowed = [possible_sports(name) for name in claimant_names]
        known = all(members is not None for members in narrowed)
        possible = frozenset().union(*narrowed) if known and narrowed else frozenset()

        direct = sport_from_competition(competition)
        routable = direct in ROUTABLE_SPORTS and direct is not None
        details.append(CollisionDetail(
            competition=competition,
            claimant_sports=claimant_names,
            narrows_to=tuple(
                "unknown" if members is None
                else ("/".join(sorted(sport.value for sport in members)) or "none of ours")
                for members in narrowed
            ),
            direct_rule_says=direct.value if routable else None,
            claimants_could_contain_it=(direct in possible) if (routable and known) else None,
        ))
    return tuple(details)


def competitions_under_our_sports(taxonomy) -> dict[str, tuple[str, ...]]:
    """Every competition the catalogue files under a sport that could hold one
    of our four leagues.

    THE QUESTION THIS EXISTS TO ANSWER. ``sport_from_competition`` maps
    "pro baseball" to MLB as a LOCAL rule, while the series probe refuses to
    promote a series on the strength of ``title='Pro Baseball ...'`` precisely
    because professional baseball also means NPB and KBO. Those two positions
    contradict each other, and the ambiguity gate is currently the only thing
    stopping the weaker one from deciding where wagers get recorded.

    Kalshi's own catalogue can settle it. If the Baseball heading lists NPB or
    KBO as competitions ALONGSIDE "Pro Baseball", then "Pro Baseball" is a
    distinct catalogue entry that does not cover them, and reading it as MLB is
    supported by the exchange rather than by our own say-so. If instead the
    catalogue has no separate entry for them, "Pro Baseball" may well be the
    label it files them under, and reading it as MLB would put a Japanese or
    Korean game into an MLB ledger.

    Public catalogue data: sport headings and competition names from
    ``GET /search/filters_by_sport``. No account data.
    """
    if taxonomy is None:
        return {}
    by_sport: dict[str, list[str]] = {}
    for competition, sport in getattr(taxonomy, "competition_to_sport", {}).items():
        if possible_sports(sport):
            by_sport.setdefault(sport, []).append(competition)
    for competition, claimants in getattr(taxonomy, "ambiguous_claimants", {}).items():
        for sport in claimants:
            if possible_sports(sport):
                by_sport.setdefault(sport, []).append(f"{competition} (contested)")
    return {sport: tuple(sorted(names)) for sport, names in sorted(by_sport.items())}


def render_competitions_under_our_sports(by_sport) -> str:
    if not by_sport:
        return "competitions under our sports: none resolved"
    lines = ["competitions the catalogue files under our sports "
             "(public catalogue names; no account data):"]
    for sport, names in by_sport.items():
        lines.append(f"  {sport}: {len(names)}")
        for name in names:
            lines.append(f"    {name}")
    return "\n".join(lines)


def render_collision_details(details) -> str:
    """Text for the probe. Empty when there is nothing to name."""
    if not details:
        return "each collision: none"
    lines = ["each collision (public catalogue names; no account data):"]
    for detail in details:
        lines.extend((
            f"  competition {detail.competition!r}",
            f"    claimed by: {', '.join(detail.claimant_sports)}",
            f"    which narrow to: {', '.join(detail.narrows_to)}",
            f"    the direct rule would say: {detail.direct_rule_says or '(nothing)'}",
            f"    claimants could contain that: {detail.claimants_could_contain_it}",
        ))
    return "\n".join(lines)


def measure_competition_collisions(taxonomy) -> CollisionStructure:
    """Describe the taxonomy's collisions without resolving any of them."""
    structure = CollisionStructure()
    if taxonomy is None:
        return structure

    for competition, claimants in sorted(
        getattr(taxonomy, "ambiguous_claimants", {}).items()
    ):
        structure.collisions += 1

        narrowed = [possible_sports(name) for name in sorted(claimants)]
        if any(members is None for members in narrowed):
            # An unheard-of sport narrows nothing, so this collision cannot be
            # called nominal OR conflicting. Saying so is the honest answer;
            # folding it into either bucket would be an opinion.
            structure.unknown_claimant += 1
            possible: frozenset = frozenset()
            known = False
        else:
            possible = frozenset().union(*narrowed) if narrowed else frozenset()
            known = True
            if len(possible) > 1:
                structure.conflicting_routable_sports += 1
            elif len(possible) == 1:
                structure.one_routable_claimant += 1
            else:
                structure.no_routable_claimant += 1

        direct = sport_from_competition(competition)
        if direct not in ROUTABLE_SPORTS:
            continue
        structure.direct_rule_would_decide += 1
        if not known:
            continue
        if direct in possible:
            structure.direct_rule_agrees_with_claimant += 1
        else:
            structure.direct_rule_contradicts_claimant += 1

    return structure
