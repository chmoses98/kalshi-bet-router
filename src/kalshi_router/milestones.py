"""Milestone-derived event -> competition index.

``GET /milestones`` filters by ``category`` (Sports, Elections, Esports, Crypto)
and by ``competition`` (documented examples: *Pro Football*, *Pro Baseball*,
*Pro Basketball (M)*, *Pro Hockey*, *College Football*), and each milestone
carries ``primary_event_tickers`` / ``related_event_tickers`` linking a real-world
fixture to Kalshi event tickers.

Privacy note -- this is the important part
------------------------------------------
The index is built by asking Kalshi for the **public** milestone list of each
competition we care about, and then looking the account's event tickers up
*locally* against that index. The account's tickers are never sent to the
milestone endpoint, so using this evidence discloses nothing about what the
owner traded.

Conflicts fail closed
---------------------
The same event ticker can surface under more than one competition sweep -- a data
error, a genuinely cross-listed fixture, or a competition boundary we do not
understand.  Keeping whichever competition was swept first would make the verdict
depend on sweep order, so a conflicted event is instead marked and yields **no**
competition at all; classification stays ``UNRESOLVED``.  Only an aggregate
conflict count is reported; event tickers are private.

Scope and cost
--------------
This is a **backstop**, not the primary signal: it only runs when events remain
unresolved after event metadata and the sport taxonomy, and it is bounded by a
request budget. Tennis is intentionally not swept -- Kalshi's tennis competitions
are per-tournament, so there is no small fixed set to enumerate, and tennis
resolves at the sport level anyway.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable

from .competitions import normalize
from .errors import KalshiRouterError, SchemaError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .client import KalshiReadOnlyClient

MILESTONES_PATH = "/milestones"
SPORTS_CATEGORY = "Sports"

#: Competitions worth sweeping: the ones that map onto a supported league.
#: Sweeping a competition we would classify as OTHER anyway buys nothing.
TARGET_COMPETITIONS: tuple[str, ...] = ("Pro Baseball", "Pro Football", "College Football")

#: Hard ceiling on milestone requests per audit, so a backstop can never become
#: the dominant cost of an audit.
DEFAULT_REQUEST_BUDGET = 24
DEFAULT_PAGE_LIMIT = 200


@dataclass
class MilestoneIndex:
    """Event ticker -> competition, built from public milestone listings."""

    #: event ticker (upper-cased) -> competition display name (unambiguous only)
    event_to_competition: dict[str, str] = field(default_factory=dict)
    #: event tickers seen under more than one competition; never resolvable
    conflicted_events: set[str] = field(default_factory=set)
    requests_issued: int = 0
    competitions_swept: int = 0
    budget_exhausted: bool = False
    fetch_failed: bool = False

    @property
    def indexed_events(self) -> int:
        """Events with exactly one observed competition."""
        return len(self.event_to_competition)

    @property
    def conflict_count(self) -> int:
        """Events claimed by more than one competition.  Aggregate only."""
        return len(self.conflicted_events)

    def record(self, event_ticker: str, competition: str) -> None:
        """Index one event -> competition link, failing closed on disagreement.

        Idempotent for a repeated identical link, and order-independent: once an
        event is conflicted it stays conflicted regardless of what is swept next.
        """
        key = event_ticker.strip().upper()
        if not key:
            return
        if key in self.conflicted_events:
            return
        existing = self.event_to_competition.get(key)
        if existing is None:
            self.event_to_competition[key] = competition
        elif existing != competition:
            del self.event_to_competition[key]
            self.conflicted_events.add(key)

    def is_conflicted(self, event_ticker: str | None) -> bool:
        if not event_ticker:
            return False
        return event_ticker.strip().upper() in self.conflicted_events

    def competition_for_event(self, event_ticker: str | None) -> str | None:
        """Return the single observed competition, or ``None`` if none/conflicted."""
        if not event_ticker:
            return None
        return self.event_to_competition.get(event_ticker.strip().upper())


def _event_tickers(milestone: dict[str, Any]) -> Iterable[str]:
    for key in ("primary_event_tickers", "related_event_tickers"):
        value = milestone.get(key)
        if isinstance(value, (list, tuple)):
            for ticker in value:
                if isinstance(ticker, str) and ticker.strip():
                    yield ticker.strip().upper()
        elif isinstance(value, str) and value.strip():
            yield value.strip().upper()


def build_milestone_index(
    client: "KalshiReadOnlyClient",
    competitions: Iterable[str] = TARGET_COMPETITIONS,
    request_budget: int = DEFAULT_REQUEST_BUDGET,
    page_limit: int = DEFAULT_PAGE_LIMIT,
) -> MilestoneIndex:
    """Sweep public milestones for the given competitions.

    Never raises on a milestone failure: this is supplementary evidence, and an
    outage here must degrade classification rather than fail the audit.
    """
    index = MilestoneIndex()

    for competition in competitions:
        if index.requests_issued >= request_budget:
            index.budget_exhausted = True
            break
        index.competitions_swept += 1
        cursor: str | None = None
        seen_cursors: set[str] = set()

        while True:
            if index.requests_issued >= request_budget:
                index.budget_exhausted = True
                break
            params: dict[str, Any] = {
                "category": SPORTS_CATEGORY,
                "competition": competition,
                "limit": page_limit,
            }
            if cursor:
                params["cursor"] = cursor
            try:
                payload = client.get_milestones(params)
            except KalshiRouterError:
                index.fetch_failed = True
                break
            index.requests_issued += 1

            milestones = payload.get("milestones")
            if not isinstance(milestones, list):
                index.fetch_failed = True
                break
            for milestone in milestones:
                if not isinstance(milestone, dict):
                    continue
                for ticker in _event_tickers(milestone):
                    index.record(ticker, competition)

            next_cursor = payload.get("cursor") or ""
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            if next_cursor in seen_cursors or not milestones:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    return index


def parse_milestones_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate a milestones envelope, failing closed on a wrong shape."""
    milestones = payload.get("milestones")
    if milestones is None:
        raise SchemaError("milestones response is missing the 'milestones' field")
    if not isinstance(milestones, list):
        raise SchemaError(
            f"milestones field was {type(milestones).__name__}, expected list"
        )
    return [m for m in milestones if isinstance(m, dict)]
