"""Pure normalization primitives for Kalshi fills.

Phase 0 does **not** build canonical wagers.  It builds the smallest faithful
in-memory representation of a fill that a Phase 1 position-accounting layer will
need, and it records honestly when a field cannot be interpreted without further
verification rather than guessing a value.

Everything in this module is pure and side-effect free: no I/O, no logging.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable

from .errors import SchemaError
from .fixedpoint import parse_fixed_point

# --------------------------------------------------------------- value domains
#
# Syntactic validity is not the same as a valid fill.  ``fixedpoint`` guarantees
# an exact Decimal; these bounds guarantee it is a quantity or price a real fill
# could carry.  They live here, in the normalization layer, rather than in the
# generic parser, so that helper stays purely about decimal syntax.
#
# Basis (docs.kalshi.com, and the Q1-2026 fixed-point / sub-penny migration):
# a Kalshi contract trades strictly between $0 and $1 and settles *at* $0 or $1.
# The classic range is $0.01-$0.99 at whole-cent ticks; sub-penny markets taper
# to finer ticks near the edges (deci $0.001, centi $0.0001), so prices below
# $0.01 and above $0.99 are legitimate on those markets.  The open interval
# (0, 1) therefore admits every tick structure while still rejecting a
# settlement value, a zero, or a negative -- none of which is a tradeable price.
# Bounds are expressed exclusively rather than as a tick-derived min/max so a
# future tick change cannot make this reject a real fill.

#: Exclusive lower bound for a contract price, in dollars.
MIN_PRICE_DOLLARS = Decimal("0")
#: Exclusive upper bound for a contract price, in dollars.
MAX_PRICE_DOLLARS = Decimal("1")
#: Exclusive bounds for the legacy integer-cent fields.
MIN_PRICE_CENTS = 0
MAX_PRICE_CENTS = 100


def _require_positive_quantity(value: Decimal, field: str) -> Decimal:
    """A fill that executed moved a positive number of contracts.

    Zero or negative is rejected rather than normalized: direction lives in
    ``action``/``side``, so a signed quantity here would mean the schema is not
    what we think it is.  The offending value is never echoed -- a contract count
    is private account data.
    """
    if value <= 0:
        raise SchemaError(f"field {field!r} was not a positive contract quantity")
    return value


def _require_price_in_range(value: Decimal, field: str) -> Decimal:
    """A contract price must sit strictly between $0 and $1."""
    if not (MIN_PRICE_DOLLARS < value < MAX_PRICE_DOLLARS):
        raise SchemaError(
            f"field {field!r} was outside the valid contract price range "
            f"(must be greater than $0 and less than $1)"
        )
    return value


class Action(str, Enum):
    """Direction of the member's execution.

    ``buy`` opens or increases exposure to the contract named by ``side``;
    ``sell`` reduces or closes it.  A member can therefore exit a position, so
    Phase 1 must treat fills as a signed stream, not an append-only bet list.
    """

    BUY = "buy"
    SELL = "sell"


class Side(str, Enum):
    """Which leg of the binary contract was transacted."""

    YES = "yes"
    NO = "no"


@dataclass(frozen=True)
class NormalizedFill:
    """One execution, normalized and kept in memory only.

    Quantities and prices are :class:`~decimal.Decimal`, parsed exactly from
    Kalshi's fixed-point decimal strings. Binary floating point is never used for
    a financial quantity.

    Instances are never serialized to disk, never logged, and never emitted in
    aggregate output.
    """

    fill_id: str
    ticker: str
    action: Action
    side: Side
    order_id: str | None = None
    trade_id: str | None = None
    #: Contract quantity. ``count_fp`` of ``"10.00"`` parses to ``Decimal("10.00")``,
    #: which is ten contracts; fractional contracts are representable.
    count: Decimal | None = None
    #: Which field the quantity came from: ``"count_fp"`` or legacy ``"count"``.
    count_source: str | None = None
    #: Execution price of the transacted leg, in dollars (e.g. ``Decimal("0.6500")``).
    price_dollars: Decimal | None = None
    #: ``"price_dollars"`` or legacy ``"price_cents"``.
    price_source: str | None = None
    #: True when a fixed-point field arrived as a JSON number rather than the
    #: documented decimal string, so schema drift is observable in aggregate.
    fixed_point_number_typed: bool = False
    is_taker: bool | None = None
    created_time: str | None = None

    @property
    def has_quantity(self) -> bool:
        return self.count is not None


def _first_present(raw: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = raw.get(key)
        if value is not None and value != "":
            return value
    return None


def _parse_quantity(raw: dict[str, Any]) -> tuple[Decimal | None, str | None, bool]:
    """Resolve the contract quantity, preferring the current fixed-point field.

    Kalshi's Q1-2026 migration removed the integer ``count``; ``count_fp`` is the
    current field and is a decimal string of contracts. The legacy field is still
    accepted so an older or replayed payload parses, but it is never preferred.
    """
    parsed = parse_fixed_point(raw.get("count_fp"), "count_fp")
    if parsed is not None:
        value = _require_positive_quantity(parsed.value, "count_fp")
        return value, "count_fp", parsed.source_type == "number"

    legacy = raw.get("count")
    if isinstance(legacy, int) and not isinstance(legacy, bool):
        return _require_positive_quantity(Decimal(legacy), "count"), "count", False
    return None, None, False


def _parse_price(raw: dict[str, Any], side: Side) -> tuple[Decimal | None, str | None, bool]:
    """Resolve the execution price of the transacted leg, in dollars.

    Sub-penny markets use tick sizes as small as $0.001, so the ``*_dollars``
    decimal string is the only field that can represent every price; the legacy
    integer-cent field is a fallback and is converted exactly.
    """
    dollars_key = "yes_price_dollars" if side is Side.YES else "no_price_dollars"
    parsed = parse_fixed_point(raw.get(dollars_key), dollars_key)
    if parsed is not None:
        value = _require_price_in_range(parsed.value, dollars_key)
        return value, "price_dollars", parsed.source_type == "number"

    cents_key = "yes_price" if side is Side.YES else "no_price"
    cents = raw.get(cents_key)
    if isinstance(cents, int) and not isinstance(cents, bool):
        if not (MIN_PRICE_CENTS < cents < MAX_PRICE_CENTS):
            raise SchemaError(
                f"field {cents_key!r} was outside the valid contract price range "
                f"(must be greater than 0 and less than 100 cents)"
            )
        # Exact: Decimal / Decimal, never float division.
        return Decimal(cents) / Decimal(100), "price_cents", False
    return None, None, False


def normalize_fill(raw: dict[str, Any]) -> NormalizedFill:
    """Convert one raw fill object into a :class:`NormalizedFill`.

    Fails closed on any required field that is absent or not interpretable:
    fill id, market ticker, action and side.  A fill whose action or side is an
    unrecognized token is rejected rather than defaulted, because defaulting
    would silently corrupt future position accounting.
    """
    if not isinstance(raw, dict):
        raise SchemaError(f"fill was {type(raw).__name__}, expected object")

    fill_id = _first_present(raw, "fill_id", "id")
    if not isinstance(fill_id, str) or not fill_id:
        raise SchemaError("fill is missing a usable 'fill_id'")

    ticker = _first_present(raw, "ticker", "market_ticker")
    if not isinstance(ticker, str) or not ticker:
        raise SchemaError("fill is missing a usable market ticker")

    raw_action = _first_present(raw, "action")
    if not isinstance(raw_action, str):
        raise SchemaError("fill is missing a usable 'action'")
    try:
        action = Action(raw_action.strip().lower())
    except ValueError:
        raise SchemaError(f"fill carried an unrecognized action token; expected one of {[a.value for a in Action]}") from None

    raw_side = _first_present(raw, "side", "outcome_side")
    if not isinstance(raw_side, str):
        raise SchemaError("fill is missing a usable 'side'")
    try:
        side = Side(raw_side.strip().lower())
    except ValueError:
        raise SchemaError(f"fill carried an unrecognized side token; expected one of {[s.value for s in Side]}") from None

    count, count_source, count_was_number = _parse_quantity(raw)
    if count is None:
        raise SchemaError("fill carried neither 'count_fp' nor a legacy 'count'")

    price, price_source, price_was_number = _parse_price(raw, side)

    order_id = _first_present(raw, "order_id")
    trade_id = _first_present(raw, "trade_id")
    is_taker = raw.get("is_taker")

    return NormalizedFill(
        fill_id=fill_id,
        ticker=ticker,
        action=action,
        side=side,
        order_id=order_id if isinstance(order_id, str) else None,
        trade_id=trade_id if isinstance(trade_id, str) else None,
        count=count,
        count_source=count_source,
        price_dollars=price,
        price_source=price_source,
        fixed_point_number_typed=count_was_number or price_was_number,
        is_taker=is_taker if isinstance(is_taker, bool) else None,
        created_time=_first_present(raw, "created_time", "ts"),
    )


@dataclass
class DedupeResult:
    """Outcome of de-duplicating a fill stream."""

    fills: list[NormalizedFill] = field(default_factory=list)
    duplicate_count: int = 0

    @property
    def unique_count(self) -> int:
        return len(self.fills)


def dedupe_fills(fills: Iterable[NormalizedFill]) -> DedupeResult:
    """Drop repeat ``fill_id`` values, preserving first-seen order.

    Kalshi pages can overlap when new fills arrive mid-walk, so the same fill id
    can legitimately appear twice in one audit.  The duplicate count is reported
    as an aggregate so an unexpected value is visible without exposing which
    fills repeated.
    """
    result = DedupeResult()
    seen: set[str] = set()
    for fill in fills:
        if fill.fill_id in seen:
            result.duplicate_count += 1
            continue
        seen.add(fill.fill_id)
        result.fills.append(fill)
    return result


def group_by_order(fills: Iterable[NormalizedFill]) -> dict[str, list[NormalizedFill]]:
    """Group fills by ``order_id``.

    One submitted order can execute against several resting orders and therefore
    produce several fills; ``order_id`` is the grouping key for "these executions
    came from one submission".  Fills without an order id are excluded, since
    they cannot be attributed to a submission.
    """
    groups: dict[str, list[NormalizedFill]] = {}
    for fill in fills:
        if fill.order_id:
            groups.setdefault(fill.order_id, []).append(fill)
    return groups


def count_partial_order_groups(fills: Iterable[NormalizedFill]) -> int:
    """Number of orders that produced more than one fill (i.e. partial fills)."""
    return sum(1 for group in group_by_order(fills).values() if len(group) > 1)
