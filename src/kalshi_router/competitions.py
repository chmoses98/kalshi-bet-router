"""Mapping Kalshi's own competition / sport vocabulary onto our routable sports (MLB, NFL, CFB, NHL, NBA, Soccer, Tennis).

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
    # NHL. "Pro Hockey" is Kalshi's own competition string: /milestones documents it, and the live
    # filters_by_sport taxonomy (probed 2026-09-29 from NHL-edge-finder) files it under Hockey BESIDE the foreign
    # leagues it names separately (KHL, SHL, Finland Liiga, Germany DEL, Czech Extraliga, Switzerland National
    # League) -- so, exactly like "Pro Baseball" among NPB/KBO/LMB, it is the sibling term for the NHL rather than an
    # umbrella over them.
    "pro hockey": Sport.NHL,
    "nhl": Sport.NHL,
    "national hockey league": Sport.NHL,
    # NBA. "Pro Basketball (M)" is Kalshi's own competition string (/milestones documents it beside "Pro Hockey");
    # the "(W)" sibling is the WNBA and college basketball is named separately, so -- exactly as for the NHL --
    # the NBA is recognised ONLY by these exact names, never from the Basketball heading alone.
    "pro basketball (m)": Sport.NBA,
    "nba": Sport.NBA,
    "national basketball association": Sport.NBA,
}

#: The ONLY names under which the NHL may be recognised. The NHL is a CLOSED-vocabulary league here: it is never
#: inferred from the word "hockey", a taxonomy heading or a ticker shape, only from one of these exact strings. That
#: is what lets the collision gate below see that a Hockey claimant of some OTHER competition name (the live
#: catalogue has filed "Pro Baseball" under Hockey) cannot be offering the NHL as a rival reading of that name.
CLOSED_VOCABULARY_LEAGUES: dict[Sport, frozenset[str]] = {
    Sport.NHL: frozenset({"pro hockey", "nhl", "national hockey league"}),
    Sport.NBA: frozenset({"pro basketball (m)", "nba", "national basketball association"}),
}

#: Exact normalized competition strings that are positively out of scope.
COMPETITION_OUT_OF_SCOPE: frozenset[str] = frozenset({
    # Basketball that is NOT the NBA. (A bare "pro basketball", without the (M), is neither claimed nor
    # excluded: it falls to the taxonomy, where the Basketball heading is an ambiguous family.)
    "pro basketball (w)", "wnba",
    "college basketball (m)", "college basketball (w)", "college basketball", "ncaab",
    # Hockey that is NOT the NHL. Named explicitly so each stays a positive OTHER rather than an unknown
    # competition inside the (now ambiguous) Hockey family.
    "college hockey", "college hockey (m)", "college hockey (w)", "ncaa hockey", "khl", "shl", "ahl", "pwhl",
    "finland liiga", "liiga", "germany del", "czech extraliga", "switzerland national league",
    "iihf", "world juniors", "field hockey",
    "college baseball", "pro golf", "golf", "esports",
    "mma", "boxing", "cricket", "rugby", "motorsport", "auto racing",
})

# --------------------------------------------------------------------- sports

#: Normalized top-level sport names, as they appear in the live taxonomy.
SPORT_TENNIS = "tennis"
SPORT_SOCCER = "soccer"

#: Sports where the sport name alone settles the classification. Soccer, like tennis, is a sport of many
#: competitions (Premier League, La Liga, MLS, Champions League ...) rather than one league, so it resolves at the
#: taxonomy SPORT level: everything Kalshi files under its Soccer heading is SOCCER. soccer-edge-finder's own
#: discovery (2026-09-27, 1,523 series) classifies by the literal "Soccer" tag the same way.
SPORT_TO_SPORT: dict[str, Sport] = {
    SPORT_TENNIS: Sport.TENNIS,
    SPORT_SOCCER: Sport.SOCCER,
    "football (soccer)": Sport.SOCCER,
    "association football": Sport.SOCCER,
}

#: Sports Kalshi covers that are positively outside our four.
OUT_OF_SCOPE_SPORTS: frozenset[str] = frozenset({
    "golf", "esports", "mma", "boxing",
    "cricket", "rugby", "motorsport", "auto racing", "racing", "olympics",
    "chess", "darts", "cycling", "table tennis", "volleyball", "lacrosse",
    "softball", "track and field", "swimming", "wrestling", "sumo", "surfing",
    "skiing", "snowboarding", "badminton", "handball", "formula 1", "nascar",
})

#: Sports that contain more than one of our target leagues, so the sport name is
#: NOT sufficient and the competition must disambiguate. Seeing one of these
#: without a recognized competition is the fail-closed case.
# Basketball holds the NBA AND the WNBA / college basketball, so -- like Hockey -- the heading alone never
# resolves; the competition ("Pro Basketball (M)") must.
AMBIGUOUS_SPORTS: frozenset[str] = frozenset({"football", "baseball", "hockey", "basketball"})

#: Which of OUR four sports could live under a taxonomy SPORT name.
#:
#: The taxonomy's top level is sports ("Baseball", "Football", "Tennis"), and
#: our four are leagues inside them. AMBIGUOUS_SPORTS above says a sport name is
#: not sufficient; this says what it does narrow the answer TO, which is a
#: different and useful question when reasoning about a collision between two
#: sports that both claim one competition name.
#:
#: A sport absent from this map narrows nothing: ``possible_sports`` returns
#: None for it rather than an empty set, because "this sport contains none of
#: our four" and "we have never heard of this sport" must not be the same
#: answer.
SPORT_FAMILY_MEMBERS: dict[str, frozenset[Sport]] = {
    "baseball": frozenset({Sport.MLB}),
    "football": frozenset({Sport.NFL, Sport.CFB}),
    "american football": frozenset({Sport.NFL, Sport.CFB}),
    "college football": frozenset({Sport.CFB}),
    # Hockey holds the NHL AND leagues that are not ours (KHL, SHL, college ...), so it is ambiguous above and
    # narrows to {NHL} here -- never resolves to it on the heading alone.
    "hockey": frozenset({Sport.NHL}),
    "ice hockey": frozenset({Sport.NHL}),
    "basketball": frozenset({Sport.NBA}),
    SPORT_TENNIS: frozenset({Sport.TENNIS}),
    SPORT_SOCCER: frozenset({Sport.SOCCER}),
    "football (soccer)": frozenset({Sport.SOCCER}),
    "association football": frozenset({Sport.SOCCER}),
}


def possible_sports(sport_name: str) -> frozenset[Sport] | None:
    """Which of our four could be under this taxonomy sport, or None if unknown.

    ``frozenset()`` means "positively none of ours" -- an out-of-scope sport.
    ``None`` means "we cannot say", which callers must not read as "none".
    """
    key = normalize(sport_name)
    if not key:
        return None
    if key in SPORT_FAMILY_MEMBERS:
        return SPORT_FAMILY_MEMBERS[key]
    if key in OUT_OF_SCOPE_SPORTS:
        return frozenset()
    return None


#: Tour tokens that identify tennis when the live taxonomy is unavailable.
#: Matched with word boundaries against the competition string.
TENNIS_COMPETITION_TOKENS: tuple[str, ...] = ("atp", "wta", "itf")

#: Competition tokens that identify soccer when the live taxonomy is unavailable. Deliberately only names that
#: belong to no other sport on the exchange: "premier league" (the Indian Premier League is cricket), "world cup"
#: (rugby, cricket) and "champions league" (hockey) are NOT here, and resolve only through the Soccer heading.
SOCCER_COMPETITION_TOKENS: tuple[str, ...] = (
    "uefa", "la liga", "bundesliga", "ligue 1", "mls", "concacaf", "conmebol", "brasileirao", "brasileirão",
    "eredivisie", "liga mx", "copa libertadores", "copa america", "copa américa", "fifa", "epl",
    "english premier league", "serie a", "série a", "primeira liga", "a-league", "nwsl",
)


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
    if any(_has_token(key, token) for token in SOCCER_COMPETITION_TOKENS):
        return Sport.SOCCER
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


def closed_vocabulary_excludes(sport: Sport, competition: str) -> bool:
    """True when ``sport`` is recognised only by exact names and ``competition`` is not one of them.

    Such a league cannot be what that competition name means, so a claimant sport that holds only such leagues is
    no rival for the name. Every other league answers False (unknown vocabulary: assume it could be a rival)."""
    names = CLOSED_VOCABULARY_LEAGUES.get(sport)
    return names is not None and normalize(competition) not in names


def is_ambiguous_sport(sport_name: str) -> bool:
    """True for a sport that holds more than one of our target leagues."""
    return normalize(sport_name) in AMBIGUOUS_SPORTS
