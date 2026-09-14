"""Settlement coverage: how far back settlement evidence actually reaches.

Why this exists
---------------
Phase C.14 established that a complete FILL history does not earn authority over
positions.  The live run showed why in numbers: 1,933 fills across both routes,
both walks exhausted, history provably COMPLETE -- and still 943 markets that the
replay holds open while ``GET /portfolio/positions`` reports nothing at all.

The cause is not a hole in the fill history.  It is that a market settles
**without a fill**, and ``GET /portfolio/settlements`` does not reach as far back
as ``GET /historical/fills`` does.  A market that was bought, held and settled
before the settlements route's reach leaves a complete fill trail and no
settlement row, so the replay keeps it open forever and nothing in the data will
ever close it.

Treating those as "still open" is the defect.  They are not open; their outcome
is **unprovable from this route**, which is a different and permanent condition.
This module measures the boundary and applies that distinction.

The asymmetry that makes this honest
------------------------------------
The floor is the **earliest settlement the route actually returned**, not a
documented retention boundary.  That gives evidence of two different strengths,
and they must not be conflated:

* **At or above the floor** the route demonstrably had data for that period.  An
  episode there with no settlement row is real evidence of absence -- a genuine
  contradiction with the exchange's own position view, and a defect to chase.
* **Below the floor** the route returned nothing at all.  That is not evidence
  the market did not settle; it is the absence of evidence either way.  Such an
  episode is marked unprovable rather than open, and never counted as a
  contradiction.

Fail-closed rules
-----------------
The floor is withheld -- ``None``, so no episode is reclassified -- whenever it
cannot be trusted:

* the settlements walk did not exhaust its cursor (a partial walk's earliest row
  is not the route's earliest row);
* any settlement row carried a ``settled_time`` that would not parse (the
  unreadable row could be older than every readable one);
* no settlement row was returned at all (there is no floor to speak of).

A withheld floor leaves every open episode exactly as the replay left it.  That
is deliberate: the failure mode this module must never have is silently
downgrading a real contradiction into "outside coverage".

Privacy
-------
Nothing here is rendered as an absolute timestamp.  The reach is reported as a
whole number of days, which describes the route rather than the account.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .models import NormalizedSettlement
from .timeaxis import seconds_to_days


@dataclass
class SettlementCoverage:
    """What the settlements walk earned the right to say about its own reach.

    Counts, booleans and epoch-second bounds.  :meth:`render` emits only counts,
    booleans and a day span; the bounds themselves never reach a public log.
    """

    rows: int = 0
    rows_with_a_readable_time: int = 0
    rows_with_an_unreadable_time: int = 0
    #: The cursor ran out -- the route had nothing more to give.
    exhausted: bool = False
    #: A caller-imposed limit stopped the walk.  Never true today; the
    #: settlements walk is unbounded by construction.
    truncated: bool = False

    earliest_settled_at: Decimal | None = None
    latest_settled_at: Decimal | None = None

    #: Whether an archival settlements route was probed, and what came back.
    #: A route that exists would close this gap outright rather than bound it.
    archive_route_probed: bool = False
    archive_route_available: bool = False
    archive_route_status: int | None = None
    archive_first_page_rows: int = 0

    @property
    def floor(self) -> Decimal | None:
        """The earliest settlement time that may be relied on, or ``None``.

        ``None`` means no episode may be reclassified -- see the module docstring
        for each condition and why it fails closed.
        """
        if not self.exhausted or self.truncated:
            return None
        if self.rows_with_an_unreadable_time:
            return None
        return self.earliest_settled_at

    @property
    def floor_is_usable(self) -> bool:
        return self.floor is not None

    @property
    def span_days(self) -> int | None:
        """Days between the earliest and latest settlement observed."""
        if self.earliest_settled_at is None or self.latest_settled_at is None:
            return None
        return seconds_to_days(self.latest_settled_at - self.earliest_settled_at)

    def reaches_back_days(self, now: Decimal) -> int | None:
        """Days from ``now`` back to the earliest settlement observed."""
        if self.earliest_settled_at is None:
            return None
        return seconds_to_days(now - self.earliest_settled_at)

    def as_dict(self) -> dict[str, int | bool]:
        """Counts, booleans and a day span.  No absolute timestamp escapes.

        The epoch-second bounds stay on the object for the engine to compare
        against; they are deliberately absent here, because "the account's
        earliest settlement was at T" is a fact about the member's history while
        "settlement evidence spans N days" is a fact about the route.
        """
        return {
            "rows": self.rows,
            "rows_with_a_readable_time": self.rows_with_a_readable_time,
            "rows_with_an_unreadable_time": self.rows_with_an_unreadable_time,
            "exhausted": self.exhausted,
            "truncated": self.truncated,
            "floor_is_usable": self.floor_is_usable,
            # Counts and booleans only -- a test enforces that structurally, so
            # "not measured" is carried by a companion flag rather than by a
            # null. 0 is a real day span; unknown is not.
            "span_days_known": self.span_days is not None,
            "span_days": self.span_days or 0,
            "archive_route_probed": self.archive_route_probed,
            "archive_route_available": self.archive_route_available,
            # 0 means no HTTP status was observed (the probe never ran, or it
            # failed below the HTTP layer), never a status of zero.
            "archive_route_status": self.archive_route_status or 0,
            "archive_first_page_rows": self.archive_first_page_rows,
        }

    def render(self) -> str:
        lines = [
            "settlement coverage (how far back settlement evidence reaches):",
            f"  settlement rows walked: {self.rows}",
            f"    with a readable settled_time: {self.rows_with_a_readable_time}",
            f"    with an UNREADABLE settled_time: "
            f"{self.rows_with_an_unreadable_time}",
            f"  walk exhausted (route had no more): {self.exhausted}",
            f"  walk truncated (budget ran out): {self.truncated}",
            f"  evidence spans (days): "
            f"{'unknown' if self.span_days is None else self.span_days}",
            f"  evidence floor usable: {self.floor_is_usable}",
        ]
        if not self.floor_is_usable:
            lines += [
                "",
                "  NOTE: no trustworthy floor, so NOTHING was reclassified. Every open",
                "        episode is left exactly as the replay found it. Withholding the",
                "        floor is the fail-closed direction: a wrong floor would convert",
                "        genuine contradictions into an explained boundary.",
            ]
        lines += [
            "",
            f"  archival settlements route probed: {self.archive_route_probed}",
            f"    route available: {self.archive_route_available}",
            f"    probe status: "
            f"{'n/a' if self.archive_route_status is None else self.archive_route_status}",
            f"    rows on its first page: {self.archive_first_page_rows}",
        ]
        return "\n".join(lines)


def build_settlement_coverage(
    settlements: Iterable[NormalizedSettlement],
    exhausted: bool,
    truncated: bool = False,
) -> SettlementCoverage:
    """Measure the reach of one settlements walk.

    ``exhausted`` and ``truncated`` come from the walk itself, not from the row
    count: a short list and a truncated list are indistinguishable afterwards,
    and the whole point of a floor is that it was not cut off by a budget.
    """
    coverage = SettlementCoverage(exhausted=exhausted, truncated=truncated)
    for settlement in settlements:
        coverage.rows += 1
        at = settlement.settled_at
        if at is None:
            coverage.rows_with_an_unreadable_time += 1
            continue
        coverage.rows_with_a_readable_time += 1
        if coverage.earliest_settled_at is None or at < coverage.earliest_settled_at:
            coverage.earliest_settled_at = at
        if coverage.latest_settled_at is None or at > coverage.latest_settled_at:
            coverage.latest_settled_at = at
    return coverage
