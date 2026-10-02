"""The classification vocabulary.

``OTHER`` and ``UNRESOLVED`` are not synonyms and the distinction is the point of
Phase 0:

``OTHER``
    The system positively identified the market as outside MLB / NFL / CFB /
    NHL / Tennis -- for example a series whose category is Economics, or whose
    sport tag is Basketball, or a non-NHL hockey league (KHL, college hockey).

``UNRESOLVED``
    The system could not prove ownership.  Metadata was missing, contradictory,
    or only ambiguous (a football market with no NFL/NCAA distinction).  Nothing
    downstream may act on an ``UNRESOLVED`` fill.
"""

from __future__ import annotations

from enum import Enum


class Sport(str, Enum):
    MLB = "MLB"
    NFL = "NFL"
    CFB = "CFB"
    #: Added 2026-09-29 for ACCOUNTING ONLY: wagers the owner places manually are recorded in
    #: NHL-edge-finder's `accounting-data` ledger. Nothing here gives any NHL model a say in a bet.
    NHL = "NHL"
    #: Added 2026-10-02 (app-readiness pass): NBA and SOCCER join the routable vocabulary. Like NHL they are
    #: ACCOUNTING ONLY destinations -- the owner's manual Kalshi wagers are recorded in each repository's
    #: accounting ledger; no model in either repository gains any say in a bet.
    NBA = "NBA"
    SOCCER = "SOCCER"
    TENNIS = "TENNIS"
    OTHER = "OTHER"
    UNRESOLVED = "UNRESOLVED"


#: The sports a classification may resolve TO (a league we could route). Whether one is actually delivered is
#: decided by `destinations.PROFILES`.
ROUTABLE_SPORTS = (Sport.MLB, Sport.NFL, Sport.CFB, Sport.NHL, Sport.NBA, Sport.SOCCER, Sport.TENNIS)

#: Stable ordering for aggregate reports.
REPORT_ORDER = (
    Sport.MLB,
    Sport.NFL,
    Sport.CFB,
    Sport.NHL,
    Sport.NBA,
    Sport.SOCCER,
    Sport.TENNIS,
    Sport.OTHER,
    Sport.UNRESOLVED,
)
