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

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .errors import KalshiRouterError, SchemaError
from .safety import safe_schema_name

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
    #: Milestone rows returned at all, before any ticker extraction.  This is
    #: what separates "the sweep found nothing" from "the sweep found rows and
    #: read the wrong field" -- two failures with opposite repairs, and the
    #: audit could not tell them apart while it only reported indexed links.
    rows_seen: int = 0
    #: Observed milestone entry key names, allowlisted.  Public catalogue schema,
    #: recorded for the same reason the taxonomy shape is.
    observed_entry_keys: dict[str, int] = field(default_factory=dict)

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


#: Cap on remembered schema names, so a pathological payload cannot turn a
#: diagnostic into an unbounded dump.
MAX_OBSERVED_KEYS = 40


def _observe_entry_keys(milestone: dict[str, Any], index: MilestoneIndex) -> None:
    """Record which fields a milestone row actually carries.

    L2 failed for exactly one reason -- the parser looked for a key the payload
    does not use -- and took three live runs to notice, because the only symptom
    was a zero. Recording the real key names makes the same failure visible in
    one run here.
    """
    for key in milestone:
        name = safe_schema_name(key) if isinstance(key, str) else None
        if name is None:
            continue
        if name in index.observed_entry_keys or len(index.observed_entry_keys) < MAX_OBSERVED_KEYS:
            index.observed_entry_keys[name] = index.observed_entry_keys.get(name, 0) + 1


def build_milestone_index(
    client: KalshiReadOnlyClient,
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
                index.rows_seen += 1
                _observe_entry_keys(milestone, index)
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


# --------------------------------------------------------------------------------------------------------------------
# Exchange-stated game start (milestone ``start_date``) -- used ONLY as a close-resolution cutoff.
# --------------------------------------------------------------------------------------------------------------------

#: The football milestone type; the working server-side filter (a ``competition`` filter matches nothing for football).
MILESTONE_TYPE_FOOTBALL_GAME = "football_game"

#: ``details.status`` values whose ``start_date`` is a stated game start. ``time-tbd`` (start not yet scheduled) and
#: ``created`` carry placeholders and are NEVER trusted; a missing status is not trusted either.
START_TRUSTED_STATUSES = frozenset({"scheduled", "inprogress", "closed", "complete"})

#: Placeholder dates (year 1, 2000-01-01) appear on non-game milestones. Nothing before this is a game start.
MIN_PLAUSIBLE_START_EPOCH = 1_577_836_800  # 2020-01-01T00:00:00Z

#: A football-game sweep is ~11 pages of 200 for the whole exchange; the budget leaves headroom, never a runaway.
START_INDEX_REQUEST_BUDGET = 40


def _start_epoch(value: Any) -> int | None:
    """An ISO-8601 instant WITH an explicit timezone; anything else (date only, naive, garbage) is no start."""
    from datetime import datetime

    if not isinstance(value, str) or "T" not in value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    epoch = int(parsed.timestamp())
    return epoch if epoch >= MIN_PLAUSIBLE_START_EPOCH else None


@dataclass
class MilestoneStartIndex:
    """Event ticker -> the exchange's stated game start (epoch seconds). Fails closed.

    An event yields a start only when every milestone naming it is a trusted-status milestone with a plausible start
    and they all agree. One untrusted or disagreeing claim removes the event for good (order-independent).
    """

    starts: dict[str, int] = field(default_factory=dict)
    #: events claimed by an untrusted milestone (time-tbd/created/no status/placeholder or unparseable date)
    untrusted_events: set[str] = field(default_factory=set)
    #: events claimed by trusted milestones that disagree on the start
    conflicted_events: set[str] = field(default_factory=set)
    rows_seen: int = 0
    requests_issued: int = 0
    budget_exhausted: bool = False
    fetch_failed: bool = False

    def record(self, event_ticker: str, start_epoch: int | None) -> None:
        key = event_ticker.strip().upper()
        if not key or key in self.untrusted_events or key in self.conflicted_events:
            return
        if start_epoch is None:
            self.starts.pop(key, None)
            self.untrusted_events.add(key)
            return
        existing = self.starts.get(key)
        if existing is None:
            self.starts[key] = start_epoch
        elif existing != start_epoch:
            del self.starts[key]
            self.conflicted_events.add(key)

    def start_for_event(self, event_ticker: str | None) -> int | None:
        if not event_ticker:
            return None
        return self.starts.get(event_ticker.strip().upper())


def milestone_start_epoch(milestone: dict[str, Any]) -> int | None:
    """The stated start of one milestone, or ``None`` when it is not a trustworthy game start."""
    details = milestone.get("details")
    status = details.get("status") if isinstance(details, dict) else None
    if not isinstance(status, str) or status.strip().lower() not in START_TRUSTED_STATUSES:
        return None
    return _start_epoch(milestone.get("start_date"))


def build_milestone_start_index(
    client: KalshiReadOnlyClient,
    request_budget: int = START_INDEX_REQUEST_BUDGET,
    page_limit: int = DEFAULT_PAGE_LIMIT,
) -> MilestoneStartIndex:
    """Sweep the PUBLIC football-game milestones once; the account's tickers are looked up locally, never sent.

    Never raises on a milestone failure: an unreadable sweep leaves the index (partly) empty, and every market it
    cannot cover stays ``no_cutoff``.
    """
    index = MilestoneStartIndex()
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        if index.requests_issued >= request_budget:
            index.budget_exhausted = True
            break
        params: dict[str, Any] = {"type": MILESTONE_TYPE_FOOTBALL_GAME, "limit": page_limit}
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
            index.rows_seen += 1
            start = milestone_start_epoch(milestone)
            for ticker in _event_tickers(milestone):
                index.record(ticker, start)
        next_cursor = payload.get("cursor") or ""
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors or not milestones:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return index
