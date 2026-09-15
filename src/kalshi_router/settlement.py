"""What the exchange paid, attributed to one wager -- or refused.

THE PROBLEM THIS MODULE EXISTS TO GET RIGHT
--------------------------------------------
A Kalshi settlement is per MARKET and per POSITION. A wager is one ORDER. When
the owner placed two orders on the same market and side, ONE settlement covers
both, and there is no field in it that says which contracts came from which
order.

The tempting move is to split the settlement's ``revenue`` between them in
proportion to contracts. That is wrong in the way that matters: it produces a
number indistinguishable from an exchange-stated one, for a quantity the
exchange never stated per order.

WHAT MAKES A PER-WAGER RETURN EXACT ANYWAY
-------------------------------------------
A binary contract pays the same as every other contract on its side. The
exchange states that price directly -- ``value`` is the market's settlement
price for a YES contract -- so

    gross return = contracts x (value if YES else 1 - value)

is not a share of a lump sum. It is a per-contract price the exchange published,
applied to a quantity this router read from the order's own fills. Two orders on
one market each get their own exact return, and the two returns sum to the
position's revenue.

WHAT IS STILL REFUSED
----------------------
* **No settlement price.** Without ``value`` there is no per-contract payout and
  the return is UNESTABLISHED. It is never reconstructed from the result and a
  count: "the YES side won" does not establish what a contract paid, and a
  market can settle anywhere between 0 and 1.
* **A settlement fee on a shared position.** A fee charged on the position is
  not a per-contract price, so where a market carries more than one of the
  owner's orders, apportioning it IS a split of a lump sum. The net P&L is
  refused there and the reason is recorded. Where the fee is zero, or where the
  wager is the only order on its market and side, nothing needs splitting.
* **A market with no settlement at all.** Reported as PENDING, never as a loss.
  An unsettled wager and a wager that lost are different facts, and defaulting
  one to the other understates a bankroll that has not moved yet.

Nothing here writes. It produces per-wager settlement rows for a destination's
own importer, exactly as the wager payloads do.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

ONE_DOLLAR = Decimal("1")
ZERO = Decimal("0")

SETTLED = "SETTLED"
PENDING = "PENDING"

WON = "WON"
LOST = "LOST"


class ReturnRefusal(str, Enum):
    """Why a money figure is absent. Absent is never rendered as zero."""

    #: The exchange stated no settlement price, so there is no per-contract
    #: payout to apply. Never reconstructed from the result and a count.
    NO_SETTLEMENT_PRICE = "no_settlement_price"
    #: A settlement fee was charged on a position covering more than one of the
    #: owner's orders. Apportioning it would be a split of a lump sum.
    SHARED_POSITION_FEE = "shared_position_fee"
    #: The market has not settled. Not a loss.
    NOT_SETTLED = "not_settled"


@dataclass(frozen=True)
class WagerSettlement:
    """One wager's settlement, as far as the exchange establishes it."""

    source_bet_key: str
    market_ticker: str
    side: str
    settlement_status: str
    settled_at: str | None = None
    result: str | None = None
    gross_return: Decimal | None = None
    net_profit_loss: Decimal | None = None
    #: Present exactly when a money field above is absent. Both empty is a
    #: fully established settlement; both populated is impossible by
    #: construction below.
    refusals: tuple[str, ...] = ()

    @property
    def is_established(self) -> bool:
        return self.gross_return is not None and self.net_profit_loss is not None


def settle_wager(wager, settlement, *, orders_on_this_position: int = 1) -> WagerSettlement:
    """Attribute one settlement to one wager, refusing what it cannot establish.

    ``orders_on_this_position`` is how many of the owner's orders share this
    wager's market and side. It is a COUNT THE CALLER MUST SUPPLY rather than
    something inferred here, because this function sees one wager and cannot
    know what else stands beside it -- and defaulting it to 1 silently would
    turn the one case that needs refusing into the one case that looks cleanest.
    """
    side = (wager.side or "").strip().upper()

    if settlement is None:
        return WagerSettlement(
            source_bet_key=wager.source_key,
            market_ticker=wager.market_ticker,
            side=side,
            settlement_status=PENDING,
            refusals=(ReturnRefusal.NOT_SETTLED.value,),
        )

    result = _result_for_side(settlement, side)
    price = settlement.market_value_dollars

    if price is None:
        return WagerSettlement(
            source_bet_key=wager.source_key,
            market_ticker=wager.market_ticker,
            side=side,
            settlement_status=SETTLED,
            settled_at=settlement.settled_time,
            result=result,
            refusals=(ReturnRefusal.NO_SETTLEMENT_PRICE.value,),
        )

    per_contract = price if side == "YES" else ONE_DOLLAR - price
    gross = per_contract * wager.contracts

    fee = settlement.fee_dollars or ZERO
    if fee != ZERO and orders_on_this_position > 1:
        # The fee is charged on the POSITION. Splitting it is a split of a lump
        # sum, which is the exact thing this module refuses to disguise as
        # exchange evidence. The gross return stays -- it rests on a published
        # per-contract price and is unaffected.
        return WagerSettlement(
            source_bet_key=wager.source_key,
            market_ticker=wager.market_ticker,
            side=side,
            settlement_status=SETTLED,
            settled_at=settlement.settled_time,
            result=result,
            gross_return=gross,
            refusals=(ReturnRefusal.SHARED_POSITION_FEE.value,),
        )

    # `stake` already carries the entry fees this router read from the order's
    # own fills, so the settlement fee is the only one left to subtract.
    net = gross - wager.stake - fee
    return WagerSettlement(
        source_bet_key=wager.source_key,
        market_ticker=wager.market_ticker,
        side=side,
        settlement_status=SETTLED,
        settled_at=settlement.settled_time,
        result=result,
        gross_return=gross,
        net_profit_loss=net,
    )


def orders_by_position(wagers) -> dict:
    """How many of these wagers share each ``(market, side)``.

    The count :func:`settle_wager` needs, computed once over the whole batch.
    """
    counts: dict[tuple[str, str], int] = {}
    for wager in wagers:
        key = (wager.market_ticker, (wager.side or "").strip().upper())
        counts[key] = counts.get(key, 0) + 1
    return counts


def settle_batch(wagers, settlements_by_ticker) -> list:
    """Every wager's settlement, in one pass. Refusals included, not dropped."""
    positions = orders_by_position(wagers)
    out = []
    for wager in wagers:
        key = (wager.market_ticker, (wager.side or "").strip().upper())
        out.append(settle_wager(
            wager,
            settlements_by_ticker.get(wager.market_ticker),
            orders_on_this_position=positions.get(key, 1),
        ))
    return out


def _result_for_side(settlement, side: str) -> str | None:
    """WON or LOST for this side, or None when the exchange's word is not one
    this router is willing to map.

    ``market_result`` is normalized to lower case upstream. A value outside
    yes/no -- a void, a scalar settlement, anything new -- is left unmapped
    rather than forced into a binary that would misstate it.
    """
    outcome = (settlement.market_result or "").strip().lower()
    if outcome not in ("yes", "no"):
        return None
    if side not in ("YES", "NO"):
        return None
    return WON if outcome == side.lower() else LOST
