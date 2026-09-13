"""The classification vocabulary.

``OTHER`` and ``UNRESOLVED`` are not synonyms and the distinction is the point of
Phase 0:

``OTHER``
    The system positively identified the market as outside MLB / NFL / CFB /
    Tennis -- for example a series whose category is Economics, or whose sport
    tag is Basketball.

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
    TENNIS = "TENNIS"
    OTHER = "OTHER"
    UNRESOLVED = "UNRESOLVED"


#: The four sports Phase 1 will eventually route to a downstream repository.
ROUTABLE_SPORTS = (Sport.MLB, Sport.NFL, Sport.CFB, Sport.TENNIS)

#: Stable ordering for aggregate reports.
REPORT_ORDER = (
    Sport.MLB,
    Sport.NFL,
    Sport.CFB,
    Sport.TENNIS,
    Sport.OTHER,
    Sport.UNRESOLVED,
)
