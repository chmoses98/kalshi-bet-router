"""Kalshi's live sport / competition / scope taxonomy.

Source: ``GET /search/filters_by_sport``, which returns

    {"filters_by_sports": {<sport>: {...scopes and competitions...}},
     "sport_ordering": [<sport>, ...]}

This is **public** taxonomy data: fetching it discloses nothing about the
account, and it is fetched **once per audit** and cached in memory only.

Its job here is to answer one question: *which sport does this competition
belong to?* That is what lets a tennis fill whose competition is a tournament
name (``ATP Madrid``) resolve to TENNIS without hard-coding every tournament,
and it is what tells us that an unfamiliar competition sits under Football and
therefore must fail closed rather than be guessed.

The exact inner shape of each sport's filter object is not fully pinned down in
the published reference, so parsing is deliberately structural rather than
positional: any list found under a competitions-like or scopes-like key is read,
and entries are accepted as bare strings or as objects carrying a name field.
Unrecognized shapes are skipped and counted, never guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .competitions import normalize
from .errors import SchemaError

FILTERS_BY_SPORT_PATH = "/search/filters_by_sport"

_COMPETITION_KEYS = ("competitions", "competition", "leagues")
_SCOPE_KEYS = ("scopes", "scope")
_NAME_KEYS = ("name", "title", "label", "value", "competition", "display_name")


def _entry_names(value: Any) -> list[str]:
    """Pull display names out of a list of strings or of small objects."""
    names: list[str] = []
    if not isinstance(value, (list, tuple)):
        return names
    for item in value:
        if isinstance(item, str):
            if item.strip():
                names.append(item)
        elif isinstance(item, dict):
            for key in _NAME_KEYS:
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    names.append(candidate)
                    break
    return names


@dataclass
class SportTaxonomy:
    """Competition -> sport index built from the live taxonomy."""

    #: normalized competition name -> normalized sport name
    competition_to_sport: dict[str, str] = field(default_factory=dict)
    #: normalized sport name -> original display name
    sports: dict[str, str] = field(default_factory=dict)
    #: normalized scope names observed, for diagnostics only
    scopes: set[str] = field(default_factory=set)
    #: sport entries whose shape could not be read
    skipped_sports: int = 0

    @property
    def sport_count(self) -> int:
        return len(self.sports)

    @property
    def competition_count(self) -> int:
        return len(self.competition_to_sport)

    def sport_for_competition(self, competition: str) -> str | None:
        """Return the normalized sport name owning this competition, if known."""
        return self.competition_to_sport.get(normalize(competition))


def parse_filters_by_sport(payload: dict[str, Any]) -> SportTaxonomy:
    """Parse a ``/search/filters_by_sport`` response, failing closed on the envelope.

    A missing or non-object ``filters_by_sports`` is a hard error: silently
    returning an empty taxonomy would look identical to "Kalshi has no sports"
    and would quietly degrade every classification to the weaker levels.
    """
    filters = payload.get("filters_by_sports")
    if filters is None:
        raise SchemaError("taxonomy response is missing the 'filters_by_sports' field")
    if not isinstance(filters, dict):
        raise SchemaError(
            f"taxonomy field 'filters_by_sports' was {type(filters).__name__}, expected object"
        )

    taxonomy = SportTaxonomy()
    for sport_name, details in filters.items():
        if not isinstance(sport_name, str) or not sport_name.strip():
            taxonomy.skipped_sports += 1
            continue
        normalized_sport = normalize(sport_name)
        taxonomy.sports[normalized_sport] = sport_name

        if not isinstance(details, dict):
            # The sport is still known to exist; only its filters are unreadable.
            taxonomy.skipped_sports += 1
            continue

        for key in _COMPETITION_KEYS:
            for competition in _entry_names(details.get(key)):
                taxonomy.competition_to_sport.setdefault(normalize(competition), normalized_sport)
        for key in _SCOPE_KEYS:
            for scope in _entry_names(details.get(key)):
                taxonomy.scopes.add(normalize(scope))

    return taxonomy
