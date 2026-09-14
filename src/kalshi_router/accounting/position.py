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

Every fill is projected onto that axis as a signed quantity and a
**YES-equivalent price**:

======================  ==================  ==========================
Fill                    Signed quantity     YES-equivalent price
======================  ==================  ==========================
``buy``  / ``yes``      ``+count``          ``price``
``sell`` / ``yes``      ``-count``          ``price``
``buy``  / ``no``       ``-count``          ``1 - price``
``sell`` / ``no``       ``+count``          ``1 - price``
======================  ==================  ==========================

Because a NO contract pays out exactly when a YES contract does not, buying NO at
$0.43 is economically selling YES at $0.57, and the projection is exact rather
than approximate.

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

from ..models import Action, NormalizedFill, Side
from .identity import digest_for, episode_source_key

ONE = Decimal(1)
ZERO = Decimal(0)


class TransitionKind(str, Enum):
    """What one fill did to a market's net position."""

    OPEN = "open"
    INCREASE = "increase"
    REDUCE = "reduce"
    CLOSE = "close"
    REVERSE = "reverse"


class Direction(str, Enum):
    LONG_YES = "long_yes"
    LONG_NO = "long_no"


def project_fill(fill: NormalizedFill) -> tuple[Decimal, Decimal | None]:
    """Project one fill onto the signed axis.

    Returns ``(signed_quantity, yes_equivalent_price)``.  The price is ``None``
    when the fill carried none; quantity is always present (normalization
    already fails closed without it).
    """
    quantity = fill.count
    if quantity is None:  # pragma: no cover - normalization guarantees this
        raise ValueError("fill has no quantity")

    price = fill.price_dollars
    if fill.side is Side.YES:
        signed = quantity if fill.action is Action.BUY else -quantity
        yes_price = price
    else:
        signed = -quantity if fill.action is Action.BUY else quantity
        yes_price = (ONE - price) if price is not None else None
    return signed, yes_price


def _sign(value: Decimal) -> int:
    return (value > 0) - (value < 0)


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
    yes_equivalent_price: Decimal | None
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
    closing_fill_id: str | None = None
    closed_at: Decimal | None = None

    remaining_quantity: Decimal = ZERO
    peak_quantity: Decimal = ZERO
    total_opened_quantity: Decimal = ZERO
    total_closed_quantity: Decimal = ZERO

    #: Quantity-weighted YES-equivalent cost of the *open* inventory.
    average_entry_price: Decimal | None = None
    #: Shadow estimate only -- see the module and docs for what it excludes.
    realized_pnl: Decimal = ZERO
    fees_paid: Decimal = ZERO

    cost_basis_complete: bool = True
    fee_complete: bool = True
    #: False when bounded history means the opening was never observed.
    provable: bool = True

    transitions: list[PositionTransition] = field(default_factory=list)
    order_ids: list[str] = field(default_factory=list)

    @property
    def source_key(self) -> str:
        return episode_source_key(self.ticker, self.opening_fill_id)

    @property
    def source_id(self) -> str:
        return digest_for(self.source_key)

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
    """Running signed inventory for one market ticker."""

    ticker: str
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
    signed, yes_price = project_fill(fill)
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
        ledger.average_entry_price = yes_price
        ledger.cost_basis_complete = yes_price is not None
        ledger.position = after
        episode = _new_episode(ledger, fill, after, provable)
        ledger.current_episode = episode
        _record(episode, fill, opened=opened, closed=ZERO, realized=None,
                position_after=after, ledger=ledger, charge_fee=True)

    elif kind is TransitionKind.INCREASE:
        if yes_price is None or ledger.average_entry_price is None:
            ledger.cost_basis_complete = False
            ledger.average_entry_price = None
        else:
            notional = ledger.average_entry_price * abs(before) + yes_price * abs(signed)
            ledger.average_entry_price = notional / abs(after)
        ledger.position = after
        _record(ledger.current_episode, fill, opened=opened, closed=ZERO, realized=None,
                position_after=after, ledger=ledger, charge_fee=True)

    elif kind in (TransitionKind.REDUCE, TransitionKind.CLOSE):
        realized = _realized(ledger, yes_price, closed, _sign(before))
        ledger.position = after
        episode = ledger.current_episode
        _record(episode, fill, opened=ZERO, closed=closed, realized=realized,
                position_after=after, ledger=ledger, charge_fee=True)
        if kind is TransitionKind.CLOSE and episode is not None:
            _close_episode(ledger, episode, fill)
            ledger.average_entry_price = None
            ledger.cost_basis_complete = True

    else:  # REVERSE
        realized = _realized(ledger, yes_price, closed, _sign(before))
        outgoing = ledger.current_episode
        ledger.position = ZERO
        _record(outgoing, fill, opened=ZERO, closed=closed, realized=realized,
                position_after=ZERO, ledger=ledger, charge_fee=True)
        if outgoing is not None:
            _close_episode(ledger, outgoing, fill)
        # The remainder opens a fresh episode on the opposite side, at the price
        # the position crossed through zero at.
        ledger.average_entry_price = yes_price
        ledger.cost_basis_complete = yes_price is not None
        ledger.position = after
        incoming = _new_episode(ledger, fill, after, provable)
        ledger.current_episode = incoming
        # The fee was already charged to the outgoing episode; charging it again
        # would double-count one execution.
        _record(incoming, fill, opened=opened, closed=ZERO, realized=None,
                position_after=after, ledger=ledger, charge_fee=False)

    return PositionTransition(
        fill_id=fill.fill_id,
        order_id=fill.order_id,
        ticker=fill.ticker,
        kind=kind,
        signed_quantity=signed,
        position_before=before,
        position_after=after,
        yes_equivalent_price=yes_price,
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
        direction=Direction.LONG_YES if position_after > 0 else Direction.LONG_NO,
        opening_fill_id=fill.fill_id,
        opened_at=parse_execution_time(fill),
        provable=provable,
    )


def _realized(
    ledger: MarketLedger, yes_price: Decimal | None, closed: Decimal, position_sign: int
) -> Decimal | None:
    """Realized P&L on a reduction, in YES-equivalent dollars.

    For a long-YES position this is ``(exit - entry) * quantity``; for a long-NO
    position the sign flips, which the ``position_sign`` factor handles.
    """
    if yes_price is None or ledger.average_entry_price is None or not ledger.cost_basis_complete:
        ledger.cost_basis_complete = False
        return None
    return (yes_price - ledger.average_entry_price) * closed * Decimal(position_sign)


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
