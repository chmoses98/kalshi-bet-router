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

#: HOW A NET FIGURE WAS COMPUTED. Carried on every settlement row a v2 destination receives, so a reader can
#: never mistake one contract's number for the other's.
#:
#: v1 (every settlement delivered before 2026-09-24): ``net = gross - stake - fee_cost``. WRONG. ``stake``
#: already includes the entry fees read from the order's own fills, and Kalshi's settlement ``fee_cost`` is
#: the CUMULATIVE TRADING FEE of the whole market position -- every one of the owner's orders on it, both
#: sides -- not a separate charge at payout. Measured on every established 2026 NFL wager (40/40): fee_cost
#: equals, to the cent, the sum of the entry fees of the owner's orders on that market. So v1 subtracts a
#: single order's fee twice, and on a YES+NO pair subtracts the market's combined fee once more on EACH leg.
#:
#: v2: ``net = gross - stake``, and ONLY when the settlement's fee_cost equals the entry fees of the owner's
#: orders on that market (the evidence that no further fee exists). Anything else is refused
#: (FEE_NOT_RECONCILED), never guessed.
ECONOMICS_V1 = "router-settlement-economics.v1"
ECONOMICS_V2 = "router-settlement-economics.v2"
ECONOMICS_VERSIONS = (ECONOMICS_V1, ECONOMICS_V2)

#: A fee comparison tolerance well below a hundredth of a cent. Fees are exchange-stated fixed-point dollars;
#: an honest equality is exact, and anything larger is a different fee.
FEE_TOLERANCE = Decimal("0.00005")

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
    #: v2 only: the settlement's fee_cost does not equal the entry fees of the owner's orders on this market,
    #: so either a fee exists that no fill explains or an order on this market is outside this batch. The net
    #: is refused rather than charged to whichever order happens to be visible.
    FEE_NOT_RECONCILED = "fee_not_reconciled"


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
    economics_version: str = ECONOMICS_V1

    @property
    def is_established(self) -> bool:
        return self.gross_return is not None and self.net_profit_loss is not None


def settle_wager(wager, settlement, *, orders_on_this_position: int = 1,
                 economics_version: str = ECONOMICS_V1,
                 market_entry_fees: Decimal | None = None) -> WagerSettlement:
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

    if economics_version == ECONOMICS_V2:
        return _settle_v2(wager, settlement, side, result, gross, market_entry_fees)
    if economics_version != ECONOMICS_V1:
        raise ValueError(f"unknown settlement economics version {economics_version!r}")

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


def _settle_v2(wager, settlement, side, result, gross, market_entry_fees) -> WagerSettlement:
    """``net = gross - stake`` -- once the exchange's own fee figure proves there is no fee left to charge.

    The settlement's ``fee_cost`` is the market position's cumulative trading fee. The entry fee of every one of
    the owner's orders on this market is already inside that order's ``stake``, so when the two agree to the
    tolerance, nothing remains and the cash identity is exact:

        net = payout received - (contracts x price + entry fees)  =  gross - stake

    ``market_entry_fees`` is the sum of ``total_fees`` over EVERY order of the owner's on this market in the
    batch, both sides. A fee_cost absent with no entry fee is zero on both sides; absent with an entry fee, or
    different in either direction, is FEE_NOT_RECONCILED.
    """
    base = dict(source_bet_key=wager.source_key, market_ticker=wager.market_ticker, side=side,
                settlement_status=SETTLED, settled_at=settlement.settled_time, result=result,
                economics_version=ECONOMICS_V2)
    entry = market_entry_fees
    fee = settlement.fee_dollars
    if entry is None:
        reconciled = False
    else:
        reconciled = (entry == ZERO) if fee is None else abs(fee - entry) <= FEE_TOLERANCE
    if not reconciled:
        return WagerSettlement(**base, gross_return=gross,
                               refusals=(ReturnRefusal.FEE_NOT_RECONCILED.value,))
    return WagerSettlement(**base, gross_return=gross, net_profit_loss=gross - wager.stake)


def market_entry_fees(wagers) -> dict:
    """ticker -> the sum of ``total_fees`` over every order in the batch on that market, BOTH sides."""
    out: dict[str, Decimal | None] = {}
    for wager in wagers:
        fee = getattr(wager, "total_fees", None)
        if wager.market_ticker in out and out[wager.market_ticker] is None:
            continue
        # One order whose fee is unknown makes the market's total unknown -- never a partial sum.
        out[wager.market_ticker] = None if fee is None else out.get(wager.market_ticker, ZERO) + fee
    return out


def orders_by_position(wagers) -> dict:
    """How many of these wagers share each ``(market, side)``.

    The count :func:`settle_wager` needs, computed once over the whole batch.
    """
    counts: dict[tuple[str, str], int] = {}
    for wager in wagers:
        key = (wager.market_ticker, (wager.side or "").strip().upper())
        counts[key] = counts.get(key, 0) + 1
    return counts


def settle_batch(wagers, settlements_by_ticker, economics_by_sport: dict | None = None) -> list:
    """Every wager's settlement, in one pass. Refusals included, not dropped.

    ``economics_by_sport`` names the economics contract each destination speaks; a sport absent from it gets
    v1, which is what every destination received before the v2 contract existed.
    """
    positions = orders_by_position(wagers)
    entry = market_entry_fees(wagers)
    out = []
    for wager in wagers:
        key = (wager.market_ticker, (wager.side or "").strip().upper())
        out.append(settle_wager(
            wager,
            settlements_by_ticker.get(wager.market_ticker),
            orders_on_this_position=positions.get(key, 1),
            economics_version=(economics_by_sport or {}).get(getattr(wager, "sport", None), ECONOMICS_V1),
            market_entry_fees=entry.get(wager.market_ticker),
        ))
    return out


def fee_evidence(wagers, settlements_by_ticker) -> dict:
    """WHAT IS ``fee_cost``? Counts only, from raw exchange evidence -- safe for a public log.

    For every SETTLED market holding at least one of the given production orders, compare the settlement's
    ``fee_cost`` with the sum of the entry fees (from the orders' own fills) of every one of those orders on
    that market, by sport and by position shape (one order / several orders one side / YES+NO). And compare
    the settlement's per-leg cost basis with the orders' principal (contracts x price) and with their stake
    (principal + entry fee), which says which of the two the exchange's cost basis is.

    EQUAL everywhere says fee_cost is the cumulative trading fee already inside the stakes. FEE_COST_GREATER
    says a fee exists that no visible fill explains (or an order is outside the window); LESS says the orders
    were charged more than the settlement reports.
    """
    by_ticker: dict[str, list] = {}
    for w in wagers:
        by_ticker.setdefault(w.market_ticker, []).append(w)
    out: dict = {}

    def bump(sport, shape, verdict):
        key = f"{sport}|{shape}|{verdict}"
        out[key] = out.get(key, 0) + 1

    for ticker, ws in by_ticker.items():
        settlement = settlements_by_ticker.get(ticker)
        if settlement is None:
            continue
        sport = ws[0].sport
        sides = {(w.side or "").upper() for w in ws}
        shape = "YES_NO_PAIR" if len(sides) > 1 else ("ONE_ORDER" if len(ws) == 1 else "SAME_SIDE_ORDERS")
        entry = sum((w.total_fees for w in ws), ZERO)
        fee = settlement.fee_dollars
        if fee is None:
            bump(sport, shape, "FEE_COST_ABSENT")
        elif abs(fee - entry) <= FEE_TOLERANCE:
            bump(sport, shape, "FEE_COST_EQUALS_ENTRY_FEES")
        elif fee > entry:
            bump(sport, shape, "FEE_COST_GREATER")
        else:
            bump(sport, shape, "FEE_COST_LESS")
        for leg, cost in (("YES", settlement.yes_cost_dollars), ("NO", settlement.no_cost_dollars)):
            legs = [w for w in ws if (w.side or "").upper() == leg]
            if not legs or cost is None:
                continue
            principal = sum((w.vwap_price * w.contracts for w in legs), ZERO)
            stake = sum((w.stake for w in legs), ZERO)
            if abs(cost - principal) <= FEE_TOLERANCE * 10:
                bump(sport, "COST_BASIS", "EQUALS_PRINCIPAL_EXCL_FEES")
            elif abs(cost - stake) <= FEE_TOLERANCE * 10:
                bump(sport, "COST_BASIS", "EQUALS_STAKE_INCL_FEES")
            else:
                bump(sport, "COST_BASIS", "MATCHES_NEITHER")
    return dict(sorted(out.items()))


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
