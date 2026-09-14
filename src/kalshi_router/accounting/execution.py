"""Order execution groups: the fills of one submitted order, summarized.

One submitted order executes against whatever resting liquidity exists, so it can
come back as several fills at several prices.  The live audit measured this
directly: 164 orders produced 200 fills, and 25 orders (15%) filled in more than
one execution.

The summary is quantity-weighted, never arithmetically averaged.  For

    BUY YES  40 @ 0.57, 65 @ 0.58, 71 @ 0.59

the arithmetic mean of the prices is 0.58, but the quantity-weighted average is

    (40*0.57 + 65*0.58 + 71*0.59) / 176 = 0.58085227...

Using the arithmetic mean would misstate cost basis on every unevenly-filled
order, which is most of them.

The original fills are preserved: this is a *view over* them, not a replacement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from ..errors import SchemaError
from ..models import BookSide, NormalizedFill, OutcomeSide
from .identity import digest_for, order_source_key
from .ordering import parse_execution_time, sort_fills


@dataclass(frozen=True)
class OrderExecution:
    """Aggregate of every fill sharing one ``order_id``.

    SENSITIVE: carries tickers, fill ids and prices.  Internal only.
    """

    order_id: str
    ticker: str
    #: Canonical direction of the submission.
    outcome_side: OutcomeSide
    book_side: BookSide | None
    subaccount_number: int | None
    total_quantity: Decimal
    fill_count: int
    fill_ids: tuple[str, ...]
    first_execution_time: Decimal
    last_execution_time: Decimal
    #: Quantity-weighted average execution price, in dollars.  ``None`` when any
    #: constituent fill lacked a price, rather than a partial average.
    vwap_price: Decimal | None = None
    #: Sum of exchange-reported fees.  ``None`` unless **every** fill carried a
    #: fee, so a partial total can never masquerade as a complete one.
    total_fee: Decimal | None = None
    fills_with_fee: int = 0
    #: True when every fill in the group reported taker; ``None`` when mixed or
    #: unreported.
    all_taker: bool | None = None

    @property
    def source_key(self) -> str:
        return order_source_key(self.order_id)

    @property
    def source_id(self) -> str:
        return digest_for(self.source_key)

    @property
    def is_partially_filled_order(self) -> bool:
        """True when this submitted order needed more than one execution."""
        return self.fill_count > 1

    @property
    def fee_complete(self) -> bool:
        return self.total_fee is not None


@dataclass
class OrderAggregationStats:
    """Privacy-safe counters describing aggregation."""

    orders: int = 0
    partial_orders: int = 0
    fills_without_order_id: int = 0
    orders_with_complete_fees: int = 0
    orders_missing_fees: int = 0
    orders_without_price: int = 0


def weighted_average_price(fills: Iterable[NormalizedFill]) -> Decimal | None:
    """Quantity-weighted average execution price, exact in :class:`Decimal`.

    Returns ``None`` if any fill lacks a price: a weighted average computed over
    a subset would understate or overstate the basis with no way to tell.
    """
    total_notional = Decimal(0)
    total_quantity = Decimal(0)
    for fill in fills:
        if fill.price_dollars is None or fill.count is None:
            return None
        total_notional += fill.price_dollars * fill.count
        total_quantity += fill.count
    if total_quantity == 0:
        return None
    # Decimal division at the default context precision (28 significant digits):
    # deterministic across runs and platforms, and never binary floating point.
    return total_notional / total_quantity


def aggregate_orders(
    fills: Iterable[NormalizedFill],
    stats: OrderAggregationStats | None = None,
) -> dict[str, OrderExecution]:
    """Group fills by ``order_id`` and summarize each group.

    Fails closed when one ``order_id`` spans more than one market, action or
    side: that would mean ``order_id`` does not identify a single submitted
    order, and every downstream assumption built on it would be unsound.

    Fills without an ``order_id`` cannot be attributed to a submission and are
    excluded from the order view (they still participate in position accounting,
    which is keyed on the market, not the order).
    """
    stats = stats if stats is not None else OrderAggregationStats()
    groups: dict[str, list[NormalizedFill]] = {}

    for fill in fills:
        if not fill.order_id:
            stats.fills_without_order_id += 1
            continue
        groups.setdefault(fill.order_id, []).append(fill)

    executions: dict[str, OrderExecution] = {}
    for order_id, group in groups.items():
        ordered = sort_fills(group)

        tickers = {f.ticker for f in ordered}
        outcomes = {f.outcome_side for f in ordered}
        exposures = {f.exposure_side for f in ordered}
        subaccounts = {f.subaccount_number for f in ordered}
        book_sides = {f.book_side for f in ordered if f.book_side is not None}
        if len(tickers) > 1:
            raise SchemaError("one order_id spanned more than one market ticker")
        if len(outcomes) > 1:
            raise SchemaError("one order_id spanned more than one outcome_side")
        if len(exposures) > 1:
            # One submission cannot both buy and sell, so mixed exposure under a
            # single order id means the grouping key is not what it claims.
            raise SchemaError("one order_id spanned more than one exposure")
        if len(book_sides) > 1:
            raise SchemaError("one order_id spanned more than one book_side")
        if len(subaccounts) > 1:
            # Two subaccounts sharing an order id would mean order_id is not a
            # per-account identity, and every position keyed on it is unsound.
            raise SchemaError("one order_id spanned more than one subaccount")

        quantities = [f.count for f in ordered]
        if any(q is None for q in quantities):
            raise SchemaError("order contained a fill with no quantity")
        total_quantity = sum(quantities, Decimal(0))

        fees = [f.fee_dollars for f in ordered]
        with_fee = sum(1 for value in fees if value is not None)
        total_fee = sum(fees, Decimal(0)) if with_fee == len(ordered) else None

        taker_flags = {f.is_taker for f in ordered}
        all_taker = taker_flags.pop() if len(taker_flags) == 1 else None

        vwap = weighted_average_price(ordered)

        execution = OrderExecution(
            order_id=order_id,
            ticker=next(iter(tickers)),
            outcome_side=next(iter(outcomes)),
            book_side=next(iter(book_sides)) if book_sides else None,
            subaccount_number=next(iter(subaccounts)),
            total_quantity=total_quantity,
            fill_count=len(ordered),
            fill_ids=tuple(f.fill_id for f in ordered),
            first_execution_time=parse_execution_time(ordered[0]),
            last_execution_time=parse_execution_time(ordered[-1]),
            vwap_price=vwap,
            total_fee=total_fee,
            fills_with_fee=with_fee,
            all_taker=all_taker if isinstance(all_taker, bool) else None,
        )
        executions[order_id] = execution

        stats.orders += 1
        if execution.is_partially_filled_order:
            stats.partial_orders += 1
        if execution.fee_complete:
            stats.orders_with_complete_fees += 1
        else:
            stats.orders_missing_fees += 1
        if execution.vwap_price is None:
            stats.orders_without_price += 1

    return executions
