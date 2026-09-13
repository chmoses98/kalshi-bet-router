"""Mapping Kalshi's own competition / sport vocabulary onto our four sports.

Why this replaces guessed tickers
---------------------------------
The first live Phase 0 audit classified only 28% of fills because the strongest
signal it had was a hand-guessed series-ticker registry. Kalshi actually exposes
the league directly: ``GET /events/{event_ticker}/metadata`` returns a
``competition`` string, and ``GET /search/filters_by_sport`` returns the live
taxonomy that groups competitions under a sport.

Crucially, ``competition`` is what distinguishes **"Pro Football"** from
**"College Football"** -- the exact NFL/CFB ambiguity the Phase 0 fail-closed rule
had to refuse 144 times.

Provenance of the strings below
-------------------------------
These are Kalshi's own values, not inventions:

* ``GET /milestones`` documents its ``competition`` filter with the examples
  *Pro Football*, *Pro Baseball*, *Pro Basketball (M)*, *Pro Hockey* and
  *College Football*.
* Kalshi's public site filters on the same vocabulary
  (``kalshi.com/sports/football?competition=College+Football``,
  ``kalshi.com/sports/football/pro-football/game``).

Matching is on a normalized (lower-cased, whitespace-collapsed) exact string, or
on an explicitly listed token, never on resemblance.

Tennis is deliberately different: Kalshi's tennis competitions are *tournaments*
(``US Open Men Singles``, ``ATP Madrid``, ``WTA Indian Wells``, ``ATP Challenger``),
not a single league name. Tennis is therefore resolved at the **sport** level from
the live taxonomy, with tour tokens (ATP/WTA/ITF) as a documented fallback for
when the taxonomy is unavailable.
"""

from __future__ import annotations

import re

from .sports import Sport

# --------------------------------------------------------------- competitions

#: Exact normalized competition strings that identify one of our four sports.
COMPETITION_TO_SPORT: dict[str, Sport] = {
    "pro baseball": Sport.MLB,
    "mlb": Sport.MLB,
    "major league baseball": Sport.MLB,
    "pro football": Sport.NFL,
    "nfl": Sport.NFL,
    "national football league": Sport.NFL,
    "college football": Sport.CFB,
    "ncaa football": Sport.CFB,
    "ncaaf": Sport.CFB,
}

#: Exact normalized competition strings that are positively out of scope.
COMPETITION_OUT_OF_SCOPE: frozenset[str] = frozenset({
    "pro basketball (m)", "pro basketball (w)", "pro basketball", "nba", "wnba",
    "college basketball (m)", "college basketball (w)", "college basketball",
    "pro hockey", "nhl", "college hockey",
    "soccer", "college baseball", "pro golf", "golf", "esports",
    "mma", "boxing", "cricket", "rugby", "motorsport", "auto racing",
})

# --------------------------------------------------------------------- sports

#: Normalized top-level sport names, as they appear in the live taxonomy.
SPORT_TENNIS = "tennis"

#: Sports where the sport name alone settles the classification.
SPORT_TO_SPORT: dict[str, Sport] = {SPORT_TENNIS: Sport.TENNIS}

#: Sports Kalshi covers that are positively outside our four.
OUT_OF_SCOPE_SPORTS: frozenset[str] = frozenset({
    "basketball", "soccer", "hockey", "golf", "esports", "mma", "boxing",
    "cricket", "rugby", "motorsport", "auto racing", "racing", "olympics",
    "chess", "darts", "cycling", "table tennis", "volleyball", "lacrosse",
    "softball", "track and field", "swimming", "wrestling", "sumo", "surfing",
    "skiing", "snowboarding", "badminton", "handball", "formula 1", "nascar",
})

#: Sports that contain more than one of our target leagues, so the sport name is
#: NOT sufficient and the competition must disambiguate. Seeing one of these
#: without a recognized competition is the fail-closed case.
AMBIGUOUS_SPORTS: frozenset[str] = frozenset({"football", "baseball"})

#: Tour tokens that identify tennis when the live taxonomy is unavailable.
#: Matched with word boundaries against the competition string.
TENNIS_COMPETITION_TOKENS: tuple[str, ...] = ("atp", "wta", "itf")


def normalize(text: object) -> str:
    """Lower-case and collapse whitespace; non-strings normalize to ``""``."""
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", text).strip().lower()


def _has_token(text: str, token: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text) is not None


def sport_from_competition(competition: str) -> Sport | None:
    """Resolve a competition string on its own, without taxonomy help.

    Returns ``None`` when the competition is not recognized -- which the caller
    must treat as fail-closed, not as permission to fall back to a guess.
    """
    key = normalize(competition)
    if not key:
        return None
    if key in COMPETITION_TO_SPORT:
        return COMPETITION_TO_SPORT[key]
    if key in COMPETITION_OUT_OF_SCOPE:
        return Sport.OTHER
    if any(_has_token(key, token) for token in TENNIS_COMPETITION_TOKENS):
        return Sport.TENNIS
    return None


def sport_from_taxonomy_sport(sport_name: str) -> Sport | None:
    """Resolve using the taxonomy's top-level sport name.

    Returns ``None`` for a sport that cannot settle the question by itself --
    either an ambiguous family (Football, Baseball) or an unknown sport.
    """
    key = normalize(sport_name)
    if not key:
        return None
    if key in SPORT_TO_SPORT:
        return SPORT_TO_SPORT[key]
    if key in OUT_OF_SCOPE_SPORTS:
        return Sport.OTHER
    return None


def is_ambiguous_sport(sport_name: str) -> bool:
    """True for a sport that holds more than one of our target leagues."""
    return normalize(sport_name) in AMBIGUOUS_SPORTS
