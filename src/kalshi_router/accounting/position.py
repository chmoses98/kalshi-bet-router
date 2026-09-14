"""Position state machine and flat-to-flat episodes.

Contract identity and the YES/NO question
-----------------------------------------
Kalshi's own portfolio API settles this, so it is not guessed here.
``GET /portfolio/positions`` returns, per market ticker, a **single signed
quantity** (``position_fp``) alongside ``market_exposure_dollars``,
``realized_pnl_dollars`` and ``fees_paid_dollars``.  There is no separate YES
inventory and NO inventory -- there is one number per market.  This matches the
order book, where a YES bid at price *X* is the same thing as a NO ask at
*$1 - X*.

So YES and NO are modelled as **binary complements on one signed axis**, keyed by
market ticker:

* positive net position  -> long YES
* negative net position  -> long NO
* zero                   -> flat

Prices are complementary legs; the axis is YES
---------------------------------------------
``yes_price_dollars`` and ``no_price_dollars`` are the two legs of one trade and
sum to ``1.00``.  Live evidence is unanimous: of 200 fills, 200 pairs summed to
exactly 1.00 and none supported reading them as a single identical price.

Since the position axis is signed YES, the accounting price must be a coordinate
on that axis, so the **YES leg** is used whichever contract was traded.  The
direction comes from the buy/sell verb and the contract together:

====================  ==============  ==================  =================
``action``            ``side``        Signed quantity     Accounting price
====================  ==============  ==================  =================
``buy``               ``yes``         ``+count``          yes leg
``sell``              ``no``          ``+count``          yes leg
``buy``               ``no``          ``-count``          yes leg
``sell``              ``yes``         ``-count``          yes leg
====================  ==============  ==================  =================

Selecting the YES leg is not complementing: the axis price is a single
consistent coordinate and is never transformed a second time.  Direction is
carried entirely by the sign of the quantity.

``outcome_side`` is the CONTRACT, not the direction
---------------------------------------------------
Live data shows ``outcome_side`` equals the deprecated ``side`` on every fill,
sells included -- a sell-NO arrives as ``outcome_side=no`` while moving the
position toward YES.  ``book_side`` tracks the contract too (every ``no`` fill
reported ``ask``, buys and sells alike), so it does not carry the verb either.
Only ``action`` does.  A fill without it is rejected rather than assumed to be a
buy, because that assumption would invert a sale.

Reversal is legal
-----------------
Selling more YES than is held does not fail: on a signed axis it simply carries
the position through zero into long-NO.  Kalshi's representation permits this, so
:class:`TransitionKind.REVERSE` is modelled rather than rejected -- it closes the
outgoing episode and opens a new one in the opposite direction at the crossing
price.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from ..models import NormalizedFill, NormalizedSettlement, OutcomeSide
from .identity import (
    Identity,
    ProvisionalIdentity,
    StableIdentity,
    episode_source_key,
)

ONE = Decimal(1)
ZERO = Decimal(0)


class TransitionKind(str, Enum):
    """What one fill did to a market's net position."""

    OPEN = "open"
    INCREASE = "increase"
    REDUCE = "reduce"
    CLOSE = "close"
    REVERSE = "reverse"
    #: The exchange closed the position itself, at expiry, with no fill.
    SETTLE = "settle"


class Direction(str, Enum):
    LONG_YES = "long_yes"
    LONG_NO = "long_no"


class PositionAuthority(str, Enum):
    """Whether one episode's POSITION STORY is proven, and by what.

    A complete walk of fills proves the fill history.  It does not, by itself,
    prove the position story: fills say what was executed, and only the
    exchange's own view -- a current position, or an authoritative closure --
    says what the account actually holds now.

    The first three states are EARNED.  The last three are not, and each is
    unearned for a different reason, so they are kept apart rather than collapsed
    into one "unknown".
    """

    #: The episode returned to flat by trading, inside a complete fill history.
    #: Every fill that opened and closed it was observed, and the exchange's
    #: current-position view has nothing to say about a span that already ended.
    CLOSED_BY_FILLS = "closed_by_fills"
    #: An authoritative settlement event closed it, with the exchange's own
    #: payout and fee.
    EXPLAINED_SETTLED = "explained_settled"
    #: Still open, and the exchange reports the same net position on that market.
    RECONCILED_CURRENT = "reconciled_current"

    #: Still open, the exchange reports no such position, and no settlement
    #: explains the closure.  The replay's inventory is contradicted.
    UNEXPLAINED = "unexplained"
    #: The exchange reports a position and it is not the one the replay computed
    #: -- or a settlement named the market and disagreed about its size.
    CONFLICTED = "conflicted"
    #: Still open and NO exchange view was supplied, so nothing was checked.
    #: Not a contradiction; not authority either.  Absence of a reconciliation is
    #: not evidence of a successful one.
    NOT_RECONCILED = "not_reconciled"


#: The states in which a position story is proven.  Everything else is unearned,
#: and unearned authority must never produce an importable identity.
EARNED_AUTHORITY = frozenset(
    {
        PositionAuthority.CLOSED_BY_FILLS,
        PositionAuthority.EXPLAINED_SETTLED,
        PositionAuthority.RECONCILED_CURRENT,
    }
)


def project_fill(fill: NormalizedFill) -> tuple[Decimal, Decimal | None]:
    """Project one fill onto the signed YES axis.

    Direction comes from :attr:`NormalizedFill.exposure_side`, which already
    combines the contract with the buy/sell verb; the price is the YES-axis
    coordinate.  Neither is transformed here -- doing the direction work twice
    is exactly the defect this replaces.

    Returns ``(signed_quantity, execution_price)``.
    """
    quantity = fill.count
    if quantity is None:  # pragma: no cover - normalization guarantees this
        raise ValueError("fill has no quantity")

    price = fill.price_dollars
    if fill.exposure_side is OutcomeSide.YES:
        return quantity, price
    return -quantity, price


def _sign(value: Decimal) -> int:
    return (value > 0) - (value < 0)


class SettlementRefusal(str, Enum):
    """The SHAPE of a refusal, so the population can be measured.

    A count of refusals says how often reconciliation failed.  It does not say
    what failed, and the repairs point in opposite directions: a settlement
    LARGER than the replayed position suggests fills the replay never saw or a
    gross rather than net count, while a SMALLER one suggests the opposite.
    Guessing between them would be exactly the kind of assumption this codebase
    keeps having to unlearn, so the shape is recorded and left to a live run to
    decide.
    """

    #: The replay holds nothing, yet the exchange settled something.
    REPLAY_FLAT = "replay_flat"
    #: Same direction, settlement states MORE contracts than the replay holds.
    SETTLEMENT_LARGER = "settlement_larger"
    #: Same direction, settlement states FEWER.
    SETTLEMENT_SMALLER = "settlement_smaller"
    #: The settlement is on the opposite side of the YES axis entirely.
    OPPOSITE_DIRECTION = "opposite_direction"
    #: The settlement carries no subaccount and the ticker is held in several.
    AMBIGUOUS_SUBACCOUNT = "ambiguous_subaccount"


class SettlementRefused(Exception):
    """A settlement that cannot be applied safely, with the reason why.

    Raised rather than applied, because the alternatives are worse: forcing the
    position to zero would invent a close the evidence does not support, and
    skipping silently would leave an episode open forever with no record of why.
    """

    def __init__(self, reason: str, refusal: SettlementRefusal) -> None:
        super().__init__(reason)
        self.reason = reason
        self.refusal = refusal


def apply_settlement(
    ledger: MarketLedger, settlement: NormalizedSettlement
) -> PositionTransition:
    """Close a market at expiry, using the exchange's own economics.

    A settlement pays out against the final result and generates **no fill**, so
    a replay built only from fills reports a long-settled market as still open.
    The live audit showed exactly that: 155 episodes open, 0 closed, in a window
    of games that had all finished.

    Nothing is reconstructed. Realized P&L is the exchange's stated ``revenue``
    against the episode's own cost basis, and the fee is the exchange's
    ``fee_cost``.

    Two refusals, both fail-closed:

    * **Size disagreement.** When the settlement states a quantity and it does
      not match the replayed position, this history is incomplete for that
      market -- some of the settled contracts were bought outside the window.
      Applying it anyway would attribute a payout to a cost basis that does not
      cover it, and quietly overstate profit.
    * **Ambiguous subaccount.** The live settlement schema carries **no**
      subaccount field. With one subaccount that is harmless; with several, a
      settlement cannot be attributed, and guessing would merge two independent
      positions. The caller must establish that only one subaccount holds the
      ticker.
    """
    before = ledger.position
    if before == 0:
        raise SettlementRefused(
            "settlement for a market the replay shows as flat",
            SettlementRefusal.REPLAY_FLAT,
        )

    stated = settlement.settled_quantity
    if stated is not None and stated != before:
        raise SettlementRefused(
            "settlement quantity does not match the replayed position; the "
            "history for this market is incomplete",
            _refusal_shape(stated, before),
        )

    closed = abs(before)
    realized: Decimal | None = None
    if ledger.cost_basis_complete and ledger.average_entry_price is not None:
        # The member paid the cost basis for `closed` contracts on their side;
        # the exchange paid back `revenue`. Both are exchange-stated.
        cost = ledger.average_entry_price if before > 0 else ONE - ledger.average_entry_price
        realized = settlement.revenue_dollars - (cost * closed)

    episode = ledger.current_episode
    ledger.position = ZERO
    ledger.average_entry_price = None

    transition = PositionTransition(
        fill_id=f"settlement:{settlement.ticker}",
        order_id=None,
        ticker=ledger.ticker,
        kind=TransitionKind.SETTLE,
        signed_quantity=-before,
        position_before=before,
        position_after=ZERO,
        execution_price=settlement.market_value_dollars,
        quantity_opened=ZERO,
        quantity_closed=closed,
        realized_pnl=realized,
        fee_dollars=settlement.fee_dollars,
    )

    if episode is not None:
        # The exchange's own settlement clock, not the opening fill's: an
        # episode that opened in March and settled in June did not close in
        # March, and a holding period derived from that would be wrong by
        # months.  ``None`` when the timestamp will not parse -- an unreadable
        # clock is left unread rather than filled in with a nearby one.
        episode.closed_at = settlement.settled_at
        episode.last_activity_at = settlement.settled_at or episode.last_activity_at
        episode.closing_fill_id = transition.fill_id
        episode.remaining_quantity = ZERO
        episode.total_closed_quantity += closed
        episode.transitions.append(transition)
        if realized is None:
            episode.cost_basis_complete = False
        else:
            episode.realized_pnl += realized
        if settlement.fee_dollars is None:
            episode.fee_complete = False
        else:
            episode.fees_paid += settlement.fee_dollars
        ledger.closed_episodes.append(episode)
        ledger.current_episode = None

    return transition


def _refusal_shape(stated: Decimal, replayed: Decimal) -> SettlementRefusal:
    """Classify a size disagreement without interpreting it.

    Direction first: a settlement on the other side of the YES axis is a
    different kind of wrong from one that merely disagrees on size, and folding
    them together would hide it.
    """
    if _sign(stated) != _sign(replayed):
        return SettlementRefusal.OPPOSITE_DIRECTION
    if abs(stated) > abs(replayed):
        return SettlementRefusal.SETTLEMENT_LARGER
    return SettlementRefusal.SETTLEMENT_SMALLER


@dataclass(frozen=True)
class PositionTransition:
    """The signed effect of one fill on one market.

    SENSITIVE: carries ticker, fill id and price.  Internal only.
    """

    fill_id: str
    order_id: str | None
    ticker: str
    kind: TransitionKind
    signed_quantity: Decimal
    position_before: Decimal
    position_after: Decimal
    execution_price: Decimal | None
    quantity_opened: Decimal = ZERO
    quantity_closed: Decimal = ZERO
    realized_pnl: Decimal | None = None
    fee_dollars: Decimal | None = None


@dataclass
class PositionEpisode:
    """One flat-to-flat span on one market -- the candidate logical wager.

    An episode is *not* asserted to be "one bet": that is a downstream policy
    choice.  It is the span the exchange evidence actually supports, and it
    exposes both its constituent orders and its constituent fills so the importer
    can decide.

    SENSITIVE: carries ticker and fill ids.  Internal only.
    """

    ticker: str
    direction: Direction
    opening_fill_id: str
    opened_at: Decimal
    #: Positions never net across subaccounts, so it is part of the identity.
    subaccount_number: int | None = None
    closing_fill_id: str | None = None
    closed_at: Decimal | None = None
    #: Execution time of the most recent event applied to this episode, on the
    #: shared epoch-second axis.  Used to ask whether the episode's activity
    #: ends above or below the settlement route's evidence floor.
    last_activity_at: Decimal | None = None

    remaining_quantity: Decimal = ZERO
    peak_quantity: Decimal = ZERO
    total_opened_quantity: Decimal = ZERO
    total_closed_quantity: Decimal = ZERO

    #: Quantity-weighted YES-axis cost of the *open* inventory.
    average_entry_price: Decimal | None = None
    #: Shadow estimate only -- see the module and docs for what it excludes.
    realized_pnl: Decimal = ZERO
    fees_paid: Decimal = ZERO

    cost_basis_complete: bool = True
    fee_complete: bool = True
    #: True when an execution's fee spans two episodes (a cross-zero reversal)
    #: and Kalshi documents no allocation rule, so no split is invented.
    fee_allocation_ambiguous: bool = False
    #: False when bounded history means the opening was never observed.
    provable: bool = True
    #: False when the episode is still open and its activity ends below the
    #: settlement route's evidence floor.  The outcome is then neither open nor
    #: closed but UNKNOWN: a settlement may exist that the route will not serve.
    #: Distinct from :attr:`provable`, which is about the opening boundary.
    outcome_provable: bool = True
    #: True when :attr:`outcome_provable` was cleared by settlement coverage
    #: rather than by anything about the fills themselves.
    outcome_bounded_by_settlement_coverage: bool = False
    #: Whether this episode's POSITION STORY is proven, and by what.  Defaults to
    #: the unearned state: authority is granted by evidence, never assumed while
    #: waiting for it.
    authority: PositionAuthority = PositionAuthority.NOT_RECONCILED

    transitions: list[PositionTransition] = field(default_factory=list)
    order_ids: list[str] = field(default_factory=list)

    @property
    def authority_is_earned(self) -> bool:
        """Whether reconciliation has proven this episode's position story."""
        return self.authority in EARNED_AUTHORITY

    @property
    def identity(self) -> Identity:
        """Stable only when BOTH boundaries are proven.  Two gates, not one.

        **The opening boundary**, from the fill history.  A bounded window cannot
        prove where flat was, so back-filling older fills can merge this episode
        into an older one and change or remove its opening fill.

        **The position story**, from reconciliation.  A complete fill history
        says what was executed; it does not say what the account holds.  An
        episode the replay shows open while the exchange reports no such
        position is contradicted, and a contradicted position must not hand a
        downstream importer a stable key merely because every fill was seen.

        Either gate failing yields a
        :class:`~kalshi_router.accounting.identity.ProvisionalIdentity`, which
        carries no source key at all -- so the refusal is structural, not a
        boolean a caller has to remember to check.
        """
        if not self.provable or not self.authority_is_earned:
            return ProvisionalIdentity(
                debug_label=(
                    f"provisional:{self.ticker}:{self.opening_fill_id}"
                    f":{self.authority.value}"
                )
            )
        return StableIdentity(
            episode_source_key(self.subaccount_number, self.ticker, self.opening_fill_id)
        )

    @property
    def source_key(self) -> str | None:
        """``None`` unless the identity is importable."""
        identity = self.identity
        return identity.source_key if isinstance(identity, StableIdentity) else None

    @property
    def source_id(self) -> str | None:
        """``None`` unless the identity is importable."""
        identity = self.identity
        return identity.source_id if isinstance(identity, StableIdentity) else None

    @property
    def is_importable(self) -> bool:
        return self.identity.is_importable

    @property
    def is_open(self) -> bool:
        return self.closing_fill_id is None

    @property
    def fill_count(self) -> int:
        return len(self.transitions)

    @property
    def order_count(self) -> int:
        return len(set(self.order_ids))


@dataclass
class MarketLedger:
    """Running signed inventory for one market, within one subaccount.

    Keyed by ``(subaccount_number, ticker)``: two subaccounts holding the same
    ticker are two independent positions and must never net together.
    """

    ticker: str
    subaccount_number: int | None = None
    position: Decimal = ZERO
    average_entry_price: Decimal | None = None
    cost_basis_complete: bool = True
    current_episode: PositionEpisode | None = None
    closed_episodes: list[PositionEpisode] = field(default_factory=list)

    @property
    def episodes(self) -> list[PositionEpisode]:
        episodes = list(self.closed_episodes)
        if self.current_episode is not None:
            episodes.append(self.current_episode)
        return episodes


def _classify(before: Decimal, after: Decimal) -> TransitionKind:
    if before == 0:
        return TransitionKind.OPEN
    if after == 0:
        return TransitionKind.CLOSE
    if _sign(after) != _sign(before):
        return TransitionKind.REVERSE
    return TransitionKind.INCREASE if abs(after) > abs(before) else TransitionKind.REDUCE


def apply_fill(
    ledger: MarketLedger, fill: NormalizedFill, provable: bool = True
) -> PositionTransition:
    """Apply one fill to a market ledger, returning its transition.

    Cost basis is weighted-average on the open inventory.  When a fill carries no
    price the economics are marked incomplete for the affected episode rather
    than estimated -- a guessed basis would propagate silently into realized P&L.
    """
    signed, price = project_fill(fill)
    before = ledger.position
    after = before + signed
    kind = _classify(before, after)

    opened = ZERO
    closed = ZERO
    realized: Decimal | None = None

    if kind in (TransitionKind.OPEN, TransitionKind.INCREASE):
        opened = abs(signed)
    elif kind in (TransitionKind.REDUCE, TransitionKind.CLOSE):
        closed = abs(signed)
    else:  # REVERSE: close all of the old side, open the remainder on the new one
        closed = abs(before)
        opened = abs(after)

    if kind is TransitionKind.OPEN:
        ledger.average_entry_price = price
        ledger.cost_basis_complete = price is not None
        ledger.position = after
        episode = _new_episode(ledger, fill, after, provable)
        ledger.current_episode = episode
        _record(episode, fill, opened=opened, closed=ZERO, realized=None,
                position_after=after, ledger=ledger, charge_fee=True)

    elif kind is TransitionKind.INCREASE:
        if price is None or ledger.average_entry_price is None:
            ledger.cost_basis_complete = False
            ledger.average_entry_price = None
        else:
            notional = ledger.average_entry_price * abs(before) + price * abs(signed)
            ledger.average_entry_price = notional / abs(after)
        ledger.position = after
        _record(ledger.current_episode, fill, opened=opened, closed=ZERO, realized=None,
                position_after=after, ledger=ledger, charge_fee=True)

    elif kind in (TransitionKind.REDUCE, TransitionKind.CLOSE):
        realized = _realized(ledger, price, closed, _sign(before))
        ledger.position = after
        episode = ledger.current_episode
        _record(episode, fill, opened=ZERO, closed=closed, realized=realized,
                position_after=after, ledger=ledger, charge_fee=True)
        if kind is TransitionKind.CLOSE and episode is not None:
            _close_episode(ledger, episode, fill)
            ledger.average_entry_price = None
            ledger.cost_basis_complete = True

    else:  # REVERSE
        realized = _realized(ledger, price, closed, _sign(before))
        outgoing = ledger.current_episode
        ledger.position = ZERO
        # This one execution spans two episodes.  Kalshi documents no rule for
        # splitting its fee between them, so none is invented: the fee is not
        # attributed to either episode, and both are marked ambiguous.  The
        # exact amount is still counted once at the account level.
        _record(outgoing, fill, opened=ZERO, closed=closed, realized=realized,
                position_after=ZERO, ledger=ledger, charge_fee=False)
        if outgoing is not None:
            outgoing.fee_allocation_ambiguous = True
            outgoing.fee_complete = False
            _close_episode(ledger, outgoing, fill)
        # The remainder opens a fresh episode on the opposite side, at the price
        # the position crossed through zero at.
        ledger.average_entry_price = price
        ledger.cost_basis_complete = price is not None
        ledger.position = after
        incoming = _new_episode(ledger, fill, after, provable)
        ledger.current_episode = incoming
        _record(incoming, fill, opened=opened, closed=ZERO, realized=None,
                position_after=after, ledger=ledger, charge_fee=False)
        incoming.fee_allocation_ambiguous = True
        incoming.fee_complete = False

    return PositionTransition(
        fill_id=fill.fill_id,
        order_id=fill.order_id,
        ticker=fill.ticker,
        kind=kind,
        signed_quantity=signed,
        position_before=before,
        position_after=after,
        execution_price=price,
        quantity_opened=opened,
        quantity_closed=closed,
        realized_pnl=realized,
        fee_dollars=fill.fee_dollars,
    )


def _new_episode(
    ledger: MarketLedger, fill: NormalizedFill, position_after: Decimal, provable: bool
) -> PositionEpisode:
    from .ordering import parse_execution_time

    return PositionEpisode(
        ticker=ledger.ticker,
        subaccount_number=ledger.subaccount_number,
        direction=Direction.LONG_YES if position_after > 0 else Direction.LONG_NO,
        opening_fill_id=fill.fill_id,
        opened_at=parse_execution_time(fill),
        last_activity_at=parse_execution_time(fill),
        provable=provable,
    )


def _realized(
    ledger: MarketLedger, price: Decimal | None, closed: Decimal, position_sign: int
) -> Decimal | None:
    """Realized P&L on a reduction, in YES-axis dollars.

    For a long-YES position this is ``(exit - entry) * quantity``; for a long-NO
    position the sign flips, which the ``position_sign`` factor handles.  That
    factor is exactly why the price itself must not be complemented.
    """
    if price is None or ledger.average_entry_price is None or not ledger.cost_basis_complete:
        ledger.cost_basis_complete = False
        return None
    return (price - ledger.average_entry_price) * closed * Decimal(position_sign)


def _record(
    episode: PositionEpisode | None,
    fill: NormalizedFill,
    opened: Decimal,
    closed: Decimal,
    realized: Decimal | None,
    position_after: Decimal,
    ledger: MarketLedger,
    charge_fee: bool,
) -> None:
    if episode is None:  # pragma: no cover - defensive
        return
    from .ordering import parse_execution_time

    episode.last_activity_at = parse_execution_time(fill)
    episode.total_opened_quantity += opened
    episode.total_closed_quantity += closed
    episode.remaining_quantity = abs(position_after)
    episode.peak_quantity = max(episode.peak_quantity, abs(position_after))
    if fill.order_id:
        episode.order_ids.append(fill.order_id)
    if realized is not None:
        episode.realized_pnl += realized
    elif closed > 0:
        episode.cost_basis_complete = False
    if charge_fee:
        if fill.fee_dollars is None:
            episode.fee_complete = False
        else:
            episode.fees_paid += fill.fee_dollars
    if not ledger.cost_basis_complete:
        episode.cost_basis_complete = False
    episode.average_entry_price = ledger.average_entry_price


def _close_episode(
    ledger: MarketLedger, episode: PositionEpisode, fill: NormalizedFill
) -> None:
    from .ordering import parse_execution_time

    episode.closing_fill_id = fill.fill_id
    episode.closed_at = parse_execution_time(fill)
    episode.remaining_quantity = ZERO
    ledger.closed_episodes.append(episode)
    ledger.current_episode = None
