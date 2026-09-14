"""Deterministic replay of a fill stream into positions.

Determinism contract
--------------------
``replay`` is a **pure function of its inputs** -- the fill set, the settlements
and the settlement floor.  It sorts into canonical order (see :mod:`.ordering`),
de-duplicates by ``fill_id``, sorts settlements by ``settled_time``, and builds
fresh ledgers on every call.  Therefore:

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
from typing import Iterable, Mapping

from ..models import NormalizedFill, NormalizedSettlement
from .execution import OrderAggregationStats, OrderExecution, aggregate_orders
from .ordering import sort_fills
from .position import (
    EARNED_AUTHORITY,
    MarketLedger,
    PositionAuthority,
    PositionEpisode,
    PositionTransition,
    SettlementRefusal,
    SettlementRefused,
    TransitionKind,
    apply_fill,
    apply_settlement,
)


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
    #: Keyed by ``(subaccount_number, ticker)`` -- positions never net across
    #: subaccounts.
    ledgers: dict[tuple[int | None, str], MarketLedger] = field(default_factory=dict)
    orders: dict[str, OrderExecution] = field(default_factory=dict)
    transitions: list[PositionTransition] = field(default_factory=list)
    order_stats: OrderAggregationStats = field(default_factory=OrderAggregationStats)
    fills_replayed: int = 0
    duplicate_fills_ignored: int = 0
    #: Settlements that closed a replayed position.
    settlements_applied: int = 0
    #: Named a ticker this replay never saw -- expected on a bounded window.
    settlements_without_a_position: int = 0
    #: Size disagreed with the replay, so the history is incomplete there.
    settlements_refused_unreconciled: int = 0
    #: The ticker is held in several subaccounts and settlements name none.
    settlements_refused_ambiguous_subaccount: int = 0
    #: Every refusal by SHAPE.  A count says how often reconciliation failed;
    #: only the shape says which way, and the repairs point opposite ways.
    settlements_refused_by_shape: dict[SettlementRefusal, int] = field(
        default_factory=dict
    )
    #: Exact sum of exchange-reported fees across every replayed fill, counted
    #: once each.  ``None`` if any fill lacked a fee field.
    total_fees: Decimal | None = None
    fills_with_fee: int = 0
    subaccounts_observed: set[int | None] = field(default_factory=set)
    #: Tickers a settlement row named, whether it applied or was refused. For
    #: these the settlements route DID produce evidence, so coverage can never
    #: be the explanation for an episode still open on them.
    tickers_with_settlement_evidence: set[str] = field(default_factory=set)
    #: Earliest settlement time the settlements route was shown to serve, on the
    #: shared epoch-second axis.  ``None`` when no trustworthy floor exists, in
    #: which case no episode is reclassified -- see :mod:`kalshi_router.coverage`.
    settlement_floor: Decimal | None = None
    #: Whether the caller supplied the exchange's own current-position view.
    #: Without it nothing was checked, and an unchecked open position is not an
    #: authoritative one.
    exchange_view_supplied: bool = False

    def ledger_for(self, ticker: str, subaccount: int | None = None) -> MarketLedger | None:
        return self.ledgers.get((subaccount, ticker))

    @property
    def fill_history_complete(self) -> bool:
        """Did both fill routes exhaust, with nothing rejected?

        This is the RAW completeness of the fill walk and nothing more.  It is
        necessary for position authority and nowhere near sufficient, so it is
        named for what it measures rather than for what a reader might hope it
        implies.
        """
        return self.completeness is HistoryCompleteness.COMPLETE

    @property
    def claims_complete_position_state(self) -> bool:
        """The EFFECTIVE authority claim -- the one machine consumers read.

        A complete walk of fills proves the fill history.  It does not prove the
        position story.  So this is true only when the fill history is complete
        AND every episode's position story has been earned: closed by observed
        fills, closed by an authoritative settlement, or reconciled against the
        exchange's own current position.

        One unexplained or conflicted market makes this False, because the claim
        is about the account's position state as a whole and that state is then
        partly unknown.  Per-episode authority is NOT destroyed with it -- see
        :attr:`PositionEpisode.authority` -- so a reconciled market keeps its
        importable identity while the global claim is withheld.

        There is deliberately no second, ungated value under this name.  The
        object, ``as_dict()`` and the rendered report all report this.
        """
        if not self.fill_history_complete:
            return False
        return all(e.authority in EARNED_AUTHORITY for e in self.episodes)

    @property
    def episodes(self) -> list[PositionEpisode]:
        return [e for ledger in self.ledgers.values() for e in ledger.episodes]

    @property
    def open_episodes(self) -> list[PositionEpisode]:
        return [e for e in self.episodes if e.is_open]

    @property
    def episodes_with_an_unprovable_outcome(self) -> list[PositionEpisode]:
        """Open episodes whose outcome the available evidence cannot establish.

        These are NOT open positions.  They are markets the replay can no longer
        follow, because a settlement that would have closed them predates the
        settlements route's reach.  Downstream must treat them as unknown, never
        as inventory.
        """
        return [e for e in self.episodes if e.is_open and not e.outcome_provable]

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
        settlements: Iterable[NormalizedSettlement] | None = None,
        settlement_floor: Decimal | None = None,
        exchange_positions: Mapping[str, Decimal | None] | None = None,
    ) -> AccountingResult:
        """Build the full accounting view from a fill set.

        The fills may arrive in any order and may contain duplicates; both are
        normalized away before anything is applied.
        """
        result = AccountingResult(
            completeness=completeness, settlement_floor=settlement_floor
        )

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
        fee_total = Decimal(0)
        fees_complete = True
        for fill in ordered:
            key = fill.position_key
            result.subaccounts_observed.add(fill.subaccount_number)
            ledger = result.ledgers.get(key)
            if ledger is None:
                ledger = MarketLedger(
                    ticker=fill.ticker, subaccount_number=fill.subaccount_number
                )
                result.ledgers[key] = ledger
            result.transitions.append(apply_fill(ledger, fill, provable=provable))
            if fill.fee_dollars is None:
                fees_complete = False
            else:
                fee_total += fill.fee_dollars
                result.fills_with_fee += 1

        # The account-level total counts every execution's fee exactly once,
        # including a cross-zero reversal whose per-episode split is undefined.
        result.total_fees = fee_total if fees_complete else None

        result.orders = aggregate_orders(ordered, result.order_stats)

        if settlements:
            self._apply_settlements(result, settlements)
        self._mark_settlement_coverage(result, settlement_floor)
        self._assign_authority(result, exchange_positions)
        return result

    @staticmethod
    def _assign_authority(
        result: AccountingResult,
        exchange_positions: Mapping[str, Decimal | None] | None,
    ) -> None:
        """Decide, per episode, whether its position story is proven and by what.

        A CLOSED episode is proven by the events that closed it: a settlement is
        the exchange's own closure, and a close by fills is fully observed inside
        the history. The exchange's current-position view has nothing to say
        about a span that already ended -- a settled market leaves that response
        entirely -- so it is not consulted for them.

        An OPEN episode is a claim about what the account holds NOW, and only the
        exchange can confirm that. Every way of failing to confirm it is kept
        distinct, because they call for different responses: a contradiction is a
        defect, a conflict is a reconciliation failure, and an absent exchange
        view is simply a question never asked.
        """
        result.exchange_view_supplied = exchange_positions is not None

        held_in: dict[str, int] = {}
        for _subaccount, ticker in result.ledgers:
            held_in[ticker] = held_in.get(ticker, 0) + 1

        for ledger in result.ledgers.values():
            for episode in ledger.episodes:
                if not episode.is_open:
                    episode.authority = (
                        PositionAuthority.EXPLAINED_SETTLED
                        if _closed_by_settlement(episode)
                        else PositionAuthority.CLOSED_BY_FILLS
                    )
                    continue
                episode.authority = _open_episode_authority(
                    result, ledger, exchange_positions, held_in
                )

    @staticmethod
    def _mark_settlement_coverage(
        result: AccountingResult, floor: Decimal | None
    ) -> None:
        """Separate "still open" from "outcome unknowable" among open episodes.

        An episode whose activity ends at or after ``floor`` sits in a period the
        settlements route demonstrably covered.  No settlement row closed it, and
        the route was walked to exhaustion, so it really is open.

        An episode whose activity ends BEFORE ``floor`` sits where the route
        returned nothing at all.  It may have settled long ago; the route will
        never say.  Its outcome is marked unprovable -- neither open nor closed
        -- so that nothing downstream mistakes it for live inventory.

        A ``None`` floor reclassifies nothing.  That is the fail-closed
        direction: over-marking would quietly convert genuine contradictions,
        which are defects to chase, into an explained boundary.
        """
        if floor is None:
            return
        for episode in result.episodes:
            if not episode.is_open:
                continue
            if episode.ticker in result.tickers_with_settlement_evidence:
                # A settlement row named this market. It was refused rather
                # than applied, which is a reconciliation problem -- already
                # recorded as such -- and never a coverage one.
                continue
            at = episode.last_activity_at
            if at is None:  # pragma: no cover - episodes always carry a time
                episode.outcome_provable = False
                continue
            if at < floor:
                episode.outcome_provable = False
                episode.outcome_bounded_by_settlement_coverage = True

    @staticmethod
    def _apply_settlements(
        result: AccountingResult, settlements: Iterable[NormalizedSettlement]
    ) -> None:
        """Close markets the exchange has already settled.

        Applied after the fills rather than merged into them, because a market
        settles at expiry and cannot take a fill afterwards -- so for any one
        ticker every fill already precedes its settlement. Ordering settlements
        among themselves by ``settled_time`` keeps the result deterministic.

        A settlement naming a ticker held in more than one subaccount is
        REFUSED: the live settlement schema carries no subaccount field, so
        attributing it would merge two independent positions.
        """
        by_ticker: dict[str, list[tuple[int | None, str]]] = {}
        for key in result.ledgers:
            by_ticker.setdefault(key[1], []).append(key)

        for settlement in sorted(settlements, key=lambda s: s.settled_time):
            keys = by_ticker.get(settlement.ticker)
            if not keys:
                result.settlements_without_a_position += 1
                continue
            # The route spoke about this market. Whatever happens next, its
            # silence is not what leaves an episode open here.
            result.tickers_with_settlement_evidence.add(settlement.ticker)
            if len(keys) > 1:
                result.settlements_refused_ambiguous_subaccount += 1
                _count_refusal(result, SettlementRefusal.AMBIGUOUS_SUBACCOUNT)
                _mark_outcome_unprovable(result, settlement.ticker)
                continue
            ledger = result.ledgers[keys[0]]
            try:
                transition = apply_settlement(ledger, settlement)
            except SettlementRefused as refused:
                result.settlements_refused_unreconciled += 1
                _count_refusal(result, refused.refusal)
                # A refused settlement is NOT an open position. The exchange
                # settled the market; the replay simply cannot reconcile the
                # size, so the outcome is unresolved rather than pending.
                # Leaving it to look open would overstate live inventory.
                _mark_outcome_unprovable(result, settlement.ticker)
                continue
            result.transitions.append(transition)
            result.settlements_applied += 1


def _closed_by_settlement(episode: PositionEpisode) -> bool:
    return any(t.kind is TransitionKind.SETTLE for t in episode.transitions)


def _open_episode_authority(
    result: AccountingResult,
    ledger: MarketLedger,
    exchange_positions: Mapping[str, Decimal | None] | None,
    held_in: dict[str, int],
) -> PositionAuthority:
    ticker = ledger.ticker
    if ticker in result.tickers_with_settlement_evidence:
        # A settlement named this market and could not be applied. The exchange
        # says the market ended; the replay still holds it. That is a
        # disagreement with exchange truth, not an unasked question.
        return PositionAuthority.CONFLICTED
    if exchange_positions is None:
        return PositionAuthority.NOT_RECONCILED
    if ticker not in exchange_positions:
        return PositionAuthority.UNEXPLAINED
    if held_in.get(ticker, 0) > 1:
        # The positions response carries no subaccount, so a reported quantity
        # cannot be attributed to one of several independent positions.
        # Attributing it anyway would merge them.
        return PositionAuthority.CONFLICTED
    reported = exchange_positions[ticker]
    if reported is None:
        # The exchange named the market but its quantity would not parse.
        # That is a failure to compare, not a comparison that succeeded -- and
        # treating it as absence would report a contradiction we did not observe.
        return PositionAuthority.CONFLICTED
    if reported == ledger.position:
        return PositionAuthority.RECONCILED_CURRENT
    return PositionAuthority.CONFLICTED


def _count_refusal(result: AccountingResult, refusal: SettlementRefusal) -> None:
    result.settlements_refused_by_shape[refusal] = (
        result.settlements_refused_by_shape.get(refusal, 0) + 1
    )


def _mark_outcome_unprovable(result: AccountingResult, ticker: str) -> None:
    """Flag every open episode on one market as having an unresolved outcome.

    Used where a settlement exists but could not be applied.  The outcome is
    then neither open nor closed, and saying so is the difference between a
    known unknown and a phantom position.
    """
    for (_subaccount, held), ledger in result.ledgers.items():
        if held != ticker:
            continue
        for episode in ledger.episodes:
            if episode.is_open:
                episode.outcome_provable = False


def net_position(
    result: AccountingResult, ticker: str, subaccount: int | None = None
) -> Decimal:
    """Signed net inventory for one market in one subaccount, positive for YES."""
    ledger = result.ledger_for(ticker, subaccount)
    return ledger.position if ledger is not None else Decimal(0)
