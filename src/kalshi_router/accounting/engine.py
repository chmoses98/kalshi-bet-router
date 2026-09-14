"""Deterministic replay of a fill stream into positions.

Determinism contract
--------------------
``replay`` is a **pure function of the fill set**.  It sorts into canonical order
(see :mod:`.ordering`), de-duplicates by ``fill_id``, and builds fresh ledgers on
every call.  Therefore:

* the same fills in any input order produce the same final state;
* replaying a set that contains duplicates produces the same state as the set
  without them;
* replaying ``A`` then replaying ``A + B`` gives the same result as replaying
  ``A + B`` once -- there is no carried-over mutable state to drift.

History completeness
--------------------
Position state is only meaningful if the replay started from a known position.
A bounded window of recent fills does **not** establish that: the first observed
fill on a market may be reducing a position opened before the window, in which
case the computed inventory is wrong by an unknown offset, and no amount of
internal consistency will reveal it.

So :class:`HistoryCompleteness` is an explicit input, and
``BOUNDED_WINDOW`` makes the engine refuse to claim position state -- every
episode is marked unprovable and
:attr:`AccountingResult.claims_complete_position_state` is ``False``.  This is
fail-closed by design: a plausible-looking position derived from a partial
history is exactly the kind of wrong answer that would corrupt a downstream
ledger silently.

See ``docs/ACCOUNTING.md`` for what a complete history actually requires.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Iterable

from ..models import NormalizedFill
from .execution import OrderAggregationStats, OrderExecution, aggregate_orders
from .ordering import sort_fills
from .position import MarketLedger, PositionEpisode, PositionTransition, TransitionKind, apply_fill


class HistoryCompleteness(str, Enum):
    """Whether the supplied fills are the account's whole history."""

    #: Every fill the account has ever had, so replay starts from a true flat.
    COMPLETE = "complete"
    #: A recent slice only.  Position state cannot be claimed.
    BOUNDED_WINDOW = "bounded_window"


@dataclass
class AccountingResult:
    """Outcome of one replay.

    SENSITIVE: ledgers, episodes, orders and transitions carry tickers, fill ids
    and prices.  Only :mod:`.diagnostics` renders anything publicly.
    """

    completeness: HistoryCompleteness
    ledgers: dict[str, MarketLedger] = field(default_factory=dict)
    orders: dict[str, OrderExecution] = field(default_factory=dict)
    transitions: list[PositionTransition] = field(default_factory=list)
    order_stats: OrderAggregationStats = field(default_factory=OrderAggregationStats)
    fills_replayed: int = 0
    duplicate_fills_ignored: int = 0

    @property
    def claims_complete_position_state(self) -> bool:
        """True only when the replay can honestly assert the account's positions."""
        return self.completeness is HistoryCompleteness.COMPLETE

    @property
    def episodes(self) -> list[PositionEpisode]:
        return [e for ledger in self.ledgers.values() for e in ledger.episodes]

    @property
    def open_episodes(self) -> list[PositionEpisode]:
        return [e for e in self.episodes if e.is_open]

    @property
    def closed_episodes(self) -> list[PositionEpisode]:
        return [e for e in self.episodes if not e.is_open]

    def transitions_of(self, kind: TransitionKind) -> list[PositionTransition]:
        return [t for t in self.transitions if t.kind is kind]


class AccountingEngine:
    """Replays fills into order groups, position transitions and episodes."""

    def replay(
        self,
        fills: Iterable[NormalizedFill],
        completeness: HistoryCompleteness = HistoryCompleteness.BOUNDED_WINDOW,
    ) -> AccountingResult:
        """Build the full accounting view from a fill set.

        The fills may arrive in any order and may contain duplicates; both are
        normalized away before anything is applied.
        """
        result = AccountingResult(completeness=completeness)

        unique: dict[str, NormalizedFill] = {}
        duplicates = 0
        for fill in fills:
            if fill.fill_id in unique:
                duplicates += 1
                continue
            unique[fill.fill_id] = fill
        result.duplicate_fills_ignored = duplicates

        ordered = sort_fills(unique.values())
        result.fills_replayed = len(ordered)

        provable = completeness is HistoryCompleteness.COMPLETE
        for fill in ordered:
            ledger = result.ledgers.get(fill.ticker)
            if ledger is None:
                ledger = MarketLedger(ticker=fill.ticker)
                result.ledgers[fill.ticker] = ledger
            result.transitions.append(apply_fill(ledger, fill, provable=provable))

        result.orders = aggregate_orders(ordered, result.order_stats)
        return result


def net_position(result: AccountingResult, ticker: str) -> Decimal:
    """Signed net inventory for one market, positive for long YES."""
    ledger = result.ledgers.get(ticker)
    return ledger.position if ledger is not None else Decimal(0)
