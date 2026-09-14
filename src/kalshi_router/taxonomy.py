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

The exact inner shape of each sport's filter object is not pinned down in the
published reference, so parsing is deliberately structural rather than
positional: any list found under a competitions-like or scopes-like key is read,
and entries are accepted as bare strings or as objects carrying a name field.
Unrecognized shapes are skipped and counted, never guessed at.

**The guessed key list is wrong.** Two live runs reported 22 sports, 0
competitions and 0 skipped sports -- so every sport's object parsed as a dict
and none of the competition-like keys matched.  Rather than guess again, the
parser now records the inner KEY NAMES it actually saw, so the next run reports
the real shape.  These are exchange schema names from a public endpoint, not
account data, so naming them discloses nothing about the owner.

Ownership collisions fail closed
--------------------------------
If one normalized competition name appears beneath **more than one sport**, the
taxonomy cannot say which sport owns it.  Picking whichever sport was parsed
first would make classification depend on dictionary iteration order, which is
exactly the kind of silent guess this system exists to avoid.  Such a competition
is marked ambiguous instead: :meth:`SportTaxonomy.sport_for_competition` returns
``None`` for it, and the classifier fails closed to ``UNRESOLVED``.  Only an
aggregate collision count is ever reported; the competition string itself is
private.
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

    #: normalized competition name -> normalized sport name (unambiguous only)
    competition_to_sport: dict[str, str] = field(default_factory=dict)
    #: normalized competitions claimed by more than one sport; never resolvable
    ambiguous_competitions: set[str] = field(default_factory=set)
    #: normalized sport name -> original display name
    sports: dict[str, str] = field(default_factory=dict)
    #: normalized scope names observed, for diagnostics only
    scopes: set[str] = field(default_factory=set)
    #: sport entries whose shape could not be read
    skipped_sports: int = 0
    #: observed inner key -> how many sports carried it.  Public schema names
    #: only; this is the diagnostic that says why no competition was found.
    observed_keys: dict[str, int] = field(default_factory=dict)
    #: observed inner key -> the JSON kind of its value, e.g. "list[str]".
    observed_key_kinds: dict[str, str] = field(default_factory=dict)
    #: keys seen inside list-of-object entries, for the same reason.
    observed_entry_keys: dict[str, int] = field(default_factory=dict)

    @property
    def sport_count(self) -> int:
        return len(self.sports)

    @property
    def competition_count(self) -> int:
        """Competitions with exactly one owning sport."""
        return len(self.competition_to_sport)

    @property
    def collision_count(self) -> int:
        """Competitions claimed by more than one sport.  Aggregate only."""
        return len(self.ambiguous_competitions)

    def is_ambiguous_competition(self, competition: str) -> bool:
        """True when more than one sport claims this competition."""
        return normalize(competition) in self.ambiguous_competitions

    def sport_for_competition(self, competition: str) -> str | None:
        """Return the sport owning this competition, or ``None``.

        ``None`` covers both "unknown" and "claimed by several sports"; callers
        must treat either as fail-closed rather than picking a winner.  Use
        :meth:`is_ambiguous_competition` to tell the two apart for reporting.
        """
        key = normalize(competition)
        if key in self.ambiguous_competitions:
            return None
        return self.competition_to_sport.get(key)


#: Cap on how many distinct schema names are remembered, so a pathological
#: payload cannot turn the diagnostic into an unbounded dump.
MAX_OBSERVED_KEYS = 40


def _kind_of(value: Any) -> str:
    """A short, non-revealing description of a value's JSON shape."""
    if isinstance(value, dict):
        return "object"
    if isinstance(value, (list, tuple)):
        if not value:
            return "list[empty]"
        inner = {"object" if isinstance(i, dict) else type(i).__name__ for i in value}
        return f"list[{'|'.join(sorted(inner))}]"
    if isinstance(value, bool):
        return "bool"
    return type(value).__name__


def _bump(counter: dict[str, int], key: str) -> None:
    if key in counter or len(counter) < MAX_OBSERVED_KEYS:
        counter[key] = counter.get(key, 0) + 1


def _observe_shape(details: dict[str, Any], taxonomy: SportTaxonomy) -> None:
    """Record the inner schema of one sport's filter object.

    Names and value KINDS only -- never a value.  A competition name is public
    taxonomy data anyway, but keeping values out of the diagnostic means this
    stays safe if the endpoint ever carries something less public.
    """
    for key, value in details.items():
        if not isinstance(key, str):
            continue
        _bump(taxonomy.observed_keys, key)
        taxonomy.observed_key_kinds.setdefault(key, _kind_of(value))
        if isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, dict):
                    for sub in item:
                        if isinstance(sub, str):
                            _bump(taxonomy.observed_entry_keys, sub)


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
    # Collect every claimant first, so the outcome cannot depend on the order in
    # which sports happen to be iterated.
    claimants: dict[str, set[str]] = {}

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

        _observe_shape(details, taxonomy)

        for key in _COMPETITION_KEYS:
            for competition in _entry_names(details.get(key)):
                claimants.setdefault(normalize(competition), set()).add(normalized_sport)
        for key in _SCOPE_KEYS:
            for scope in _entry_names(details.get(key)):
                taxonomy.scopes.add(normalize(scope))

    for competition, sports in claimants.items():
        if len(sports) == 1:
            taxonomy.competition_to_sport[competition] = next(iter(sports))
        else:
            # Two or more sports claim this name: the taxonomy cannot say who
            # owns it, so nobody does.
            taxonomy.ambiguous_competitions.add(competition)

    return taxonomy
