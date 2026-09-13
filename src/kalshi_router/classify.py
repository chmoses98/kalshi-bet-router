"""Explainable, fail-closed sport classification for Kalshi markets.

Evidence hierarchy
------------------
Strongest to weakest.  Only *authoritative* evidence can decide a sport;
*supporting* evidence is recorded for explainability and never resolves a market
on its own.

1. ``series.tags`` / ``series.categories`` -- Kalshi's own discovery taxonomy.
2. ``series.category`` -- the series' primary category.
3. ``series.title`` -- the series template name ("MLB Game Winner").
4. Exact ``series_ticker`` match against :mod:`kalshi_router.series_registry`,
   including the series prefix of a market ticker, which Kalshi forms as
   ``SERIES-EVENT-OUTCOME``.
5. ``event`` / ``market`` titles and subtitles -- **supporting only**.  An event
   title is usually the two competitors, which does not identify a league.

Fail-closed rules
-----------------
* Two authoritative signals naming different sports -> ``UNRESOLVED``.
* A sport *family* with no league distinction -> ``UNRESOLVED``.  This is what
  stops a generic "Football" market from being routed to NFL or CFB.
* No authoritative signal at all -> ``UNRESOLVED``.
* ``OTHER`` is only returned when authoritative metadata positively places the
  market outside the four supported sports.

Privacy
-------
:class:`Classification` carries tickers and matched tokens so a human can audit a
decision.  That makes it sensitive when bound to the owner's fills: it is
surfaced only by the local sensitive diagnostic mode and never by aggregates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from .series_registry import lookup_series_ticker
from .sports import ROUTABLE_SPORTS, Sport


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
    #: For OTHER evidence: ``"sport"`` when a specific rival sport was named,
    #: ``"category"`` when only a non-sports category was named.
    out_of_scope_kind: str | None = None


@dataclass(frozen=True)
class MarketContext:
    """Resolved public metadata for one market ticker.

    Any of ``market``/``event``/``series`` may be ``None`` when a lookup was not
    reached or failed; ``lookup_error`` records a non-secret reason.
    """

    market_ticker: str
    market: dict[str, Any] | None = None
    event: dict[str, Any] | None = None
    series: dict[str, Any] | None = None
    lookup_error: str | None = None


@dataclass(frozen=True)
class Classification:
    """An explainable classification decision for one market."""

    sport: Sport
    reason: str
    market_ticker: str
    evidence: tuple[Evidence, ...] = ()
    event_ticker: str | None = None
    series_ticker: str | None = None
    used_unverified_series_ticker: bool = False

    @property
    def is_routable(self) -> bool:
        return self.sport in ROUTABLE_SPORTS


# --------------------------------------------------------------------- tokens

#: League tokens that positively identify one of the four supported sports.
LEAGUE_TOKENS: dict[Sport, tuple[str, ...]] = {
    Sport.MLB: ("mlb", "major league baseball", "world series"),
    Sport.NFL: ("nfl", "national football league", "super bowl"),
    Sport.CFB: (
        "cfb",
        "ncaaf",
        "ncaa football",
        "college football",
        "college football playoff",
    ),
    Sport.TENNIS: ("tennis", "atp", "wta"),
}

#: Sport families that do **not** identify a league.  Seeing one of these without
#: a league token is the ambiguity the fail-closed rule exists for: "Football"
#: could be NFL or college, "Baseball" could be MLB, NCAA, KBO or NPB.
AMBIGUOUS_FAMILY_TOKENS: tuple[str, ...] = (
    "football",
    "american football",
    "baseball",
    "college sports",
    "ncaa",
)

#: Sports Kalshi covers that are positively *not* ours.
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

#: Non-sports Kalshi categories.  A market in one of these is positively OTHER.
NON_SPORTS_CATEGORY_TOKENS: tuple[str, ...] = (
    "politics", "elections", "economics", "financials", "financial markets",
    "climate", "weather", "entertainment", "science and technology", "technology",
    "health", "world", "companies", "crypto", "cryptocurrency", "transportation",
    "culture", "media", "awards", "inflation", "fed", "commodities",
)

def _normalize(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", text).strip().lower()


def _contains(text: str, token: str) -> bool:
    """Word-boundary containment, so 'atp' never matches inside 'adaptation'."""
    if not text or not token:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text) is not None


def _iter_texts(value: Any) -> Iterable[str]:
    """Yield normalized strings from a string or a list of strings."""
    if isinstance(value, str):
        normalized = _normalize(value)
        if normalized:
            yield normalized
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str):
                normalized = _normalize(item)
                if normalized:
                    yield normalized
            elif isinstance(item, dict):
                # Tag objects have been exposed both as bare strings and as
                # ``{"name": ...}`` objects; accept either shape.
                for key in ("name", "title", "label"):
                    normalized = _normalize(item.get(key))
                    if normalized:
                        yield normalized
                        break


def _scan_text(text: str, source: str, strength: EvidenceStrength) -> list[Evidence]:
    """Match one metadata string against every token table."""
    found: list[Evidence] = []
    for sport, tokens in LEAGUE_TOKENS.items():
        for token in tokens:
            if _contains(text, token):
                found.append(Evidence(source, strength, token, sport=sport))
                break
    for token in NON_TARGET_SPORT_TOKENS:
        if _contains(text, token):
            found.append(
                Evidence(source, strength, token, sport=Sport.OTHER, out_of_scope_kind="sport")
            )
            break
    for token in NON_SPORTS_CATEGORY_TOKENS:
        if _contains(text, token):
            found.append(
                Evidence(source, strength, token, sport=Sport.OTHER, out_of_scope_kind="category")
            )
            break
    for token in AMBIGUOUS_FAMILY_TOKENS:
        if _contains(text, token):
            found.append(Evidence(source, strength, token, ambiguous_family=token))
            break
    return found


def _get(mapping: dict[str, Any] | None, key: str) -> Any:
    return mapping.get(key) if isinstance(mapping, dict) else None


def derive_series_ticker(context: MarketContext) -> tuple[str | None, str]:
    """Resolve the series ticker and say where it came from.

    Falls back to the series prefix of the market ticker, which Kalshi documents
    as the first hyphen-delimited segment of ``SERIES-EVENT-OUTCOME``.
    """
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


def collect_evidence(context: MarketContext) -> tuple[list[Evidence], str | None, bool]:
    """Gather every signal for a market.

    Returns ``(evidence, series_ticker, used_unverified_registry_entry)``.
    """
    evidence: list[Evidence] = []
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
    for mapping, key, source in authoritative_fields:
        for text in _iter_texts(_get(mapping, key)):
            evidence.extend(_scan_text(text, source, A))

    supporting_fields = (
        (context.event, "title", "event.title"),
        (context.event, "sub_title", "event.sub_title"),
        (context.market, "title", "market.title"),
        (context.market, "subtitle", "market.subtitle"),
        (context.market, "yes_sub_title", "market.yes_sub_title"),
    )
    for mapping, key, source in supporting_fields:
        for text in _iter_texts(_get(mapping, key)):
            evidence.extend(_scan_text(text, source, S))

    series_ticker, ticker_source = derive_series_ticker(context)
    used_unverified = False
    entry = lookup_series_ticker(series_ticker)
    if entry is not None:
        evidence.append(
            Evidence(
                source=f"series_registry[{ticker_source}]",
                strength=A,
                matched_token=(series_ticker or "").upper(),
                sport=entry.sport,
            )
        )
        used_unverified = not entry.verified

    return evidence, series_ticker, used_unverified


def classify_market(context: MarketContext) -> Classification:
    """Classify one market, failing closed on ambiguity."""
    event_ticker = _get(context.event, "event_ticker") or _get(context.market, "event_ticker")
    if not isinstance(event_ticker, str):
        event_ticker = None

    if context.lookup_error:
        return Classification(
            sport=Sport.UNRESOLVED,
            reason=f"metadata_lookup_failed: {context.lookup_error}",
            market_ticker=context.market_ticker,
            event_ticker=event_ticker,
        )

    if not any(isinstance(m, dict) for m in (context.market, context.event, context.series)):
        return Classification(
            sport=Sport.UNRESOLVED,
            reason="no_metadata_resolved",
            market_ticker=context.market_ticker,
            event_ticker=event_ticker,
        )

    evidence, series_ticker, used_unverified = collect_evidence(context)
    packed = tuple(evidence)

    authoritative = [e for e in evidence if e.strength is EvidenceStrength.AUTHORITATIVE]
    league_sports = {e.sport for e in authoritative if e.sport in ROUTABLE_SPORTS}

    if len(league_sports) > 1:
        names = ", ".join(sorted(s.value for s in league_sports))
        return Classification(
            sport=Sport.UNRESOLVED,
            reason=f"conflicting_authoritative_evidence: {names}",
            market_ticker=context.market_ticker,
            evidence=packed,
            event_ticker=event_ticker,
            series_ticker=series_ticker,
            used_unverified_series_ticker=used_unverified,
        )

    if len(league_sports) == 1:
        sport = next(iter(league_sports))
        sources = ", ".join(sorted({e.source for e in authoritative if e.sport is sport}))
        return Classification(
            sport=sport,
            reason=f"authoritative_league_match: {sources}",
            market_ticker=context.market_ticker,
            evidence=packed,
            event_ticker=event_ticker,
            series_ticker=series_ticker,
            used_unverified_series_ticker=used_unverified,
        )

    # No supported league proven.  A *specifically named* rival sport is a
    # positive out-of-scope identification and outranks a generic family word
    # (a series tagged "NCAA" + "Basketball" is college basketball, not unknown).
    non_target_sport = [
        e for e in authoritative if e.sport is Sport.OTHER and e.out_of_scope_kind == "sport"
    ]
    if non_target_sport:
        sources = ", ".join(sorted({e.source for e in non_target_sport}))
        return Classification(
            sport=Sport.OTHER,
            reason=f"authoritative_out_of_scope_sport: {sources}",
            market_ticker=context.market_ticker,
            evidence=packed,
            event_ticker=event_ticker,
            series_ticker=series_ticker,
            used_unverified_series_ticker=used_unverified,
        )

    # A sport family with no league distinction is the ambiguity the fail-closed
    # rule exists for: "Football" is not evidence for NFL, for CFB, or for OTHER.
    ambiguous = [e for e in authoritative if e.ambiguous_family]
    if ambiguous:
        families = ", ".join(sorted({e.ambiguous_family or "" for e in ambiguous}))
        return Classification(
            sport=Sport.UNRESOLVED,
            reason=f"ambiguous_sport_family_without_league: {families}",
            market_ticker=context.market_ticker,
            evidence=packed,
            event_ticker=event_ticker,
            series_ticker=series_ticker,
            used_unverified_series_ticker=used_unverified,
        )

    non_sports = [
        e for e in authoritative if e.sport is Sport.OTHER and e.out_of_scope_kind == "category"
    ]
    if non_sports:
        sources = ", ".join(sorted({e.source for e in non_sports}))
        return Classification(
            sport=Sport.OTHER,
            reason=f"authoritative_non_sports_category: {sources}",
            market_ticker=context.market_ticker,
            evidence=packed,
            event_ticker=event_ticker,
            series_ticker=series_ticker,
            used_unverified_series_ticker=used_unverified,
        )

    return Classification(
        sport=Sport.UNRESOLVED,
        reason="insufficient_authoritative_metadata",
        market_ticker=context.market_ticker,
        evidence=packed,
        event_ticker=event_ticker,
        series_ticker=series_ticker,
        used_unverified_series_ticker=used_unverified,
    )
