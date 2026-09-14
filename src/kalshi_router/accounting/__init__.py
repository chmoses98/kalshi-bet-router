"""Shadow-only fill -> order -> position accounting.

Phase 1A. This package answers "what logical positions did the account actually
establish, increase, reduce, close or reverse?" and **routes nothing**. There is
no downstream write, no canonical wager emission, no persistence.

The four layers are kept distinct on purpose:

``RAW FILL``
    Immutable execution evidence, identified by Kalshi's ``fill_id``.
``ORDER EXECUTION GROUP``
    The fills of one submitted order, summarized with a quantity-weighted
    average price.  One order can produce many fills.
``POSITION TRANSITION``
    The signed effect of one fill on one market's inventory.
``POSITION EPISODE``
    A span from flat to flat on one market -- the candidate "logical wager".

Nothing here is collapsed into a canonical bet: the downstream importer will
make that policy choice explicitly, and this package exists to give it the
structure to choose from.
"""

from __future__ import annotations

__all__ = [
    "canonical_fill_sort_key",
    "sort_fills",
    "OrderExecution",
    "aggregate_orders",
    "PositionEpisode",
    "PositionTransition",
    "TransitionKind",
    "HistoryCompleteness",
    "AccountingEngine",
    "AccountingResult",
]

from .engine import AccountingEngine, AccountingResult, HistoryCompleteness
from .execution import OrderExecution, aggregate_orders
from .ordering import canonical_fill_sort_key, sort_fills
from .position import PositionEpisode, PositionTransition, TransitionKind
