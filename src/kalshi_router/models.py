"""Pure normalization primitives for Kalshi fills.

Phase 0 does **not** build canonical wagers.  It builds the smallest faithful
in-memory representation of a fill that a Phase 1 position-accounting layer will
need, and it records honestly when a field cannot be interpreted without further
verification rather than guessing a value.

Everything in this module is pure and side-effect free: no I/O, no logging.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from .errors import SchemaError


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
    """One execution, normalized and stripped of nothing (kept in memory only).

    Instances are never serialized to disk, never logged, and never emitted in
    aggregate output.  They exist so classification and Phase 1 research can be
    performed on a stable shape.
    """

    fill_id: str
    ticker: str
    action: Action
    side: Side
    order_id: str | None = None
    trade_id: str | None = None
    count: int | None = None
    count_fp_raw: str | None = None
    price_cents: int | None = None
    is_taker: bool | None = None
    created_time: str | None = None

    @property
    def count_needs_verification(self) -> bool:
        """True when only an unverified fixed-point count was available.

        The ``*_fp`` fixed-point encoding is not documented with a scale factor
        we were able to verify, so no integer contract count is inferred from it.
        """
        return self.count is None and self.count_fp_raw is not None


def _first_present(raw: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = raw.get(key)
        if value is not None and value != "":
            return value
    return None


def _parse_price_cents(raw: dict[str, Any], side: Side) -> int | None:
    """Resolve the execution price of the transacted leg, in integer cents.

    Kalshi has exposed both integer-cent fields (``yes_price``/``no_price``) and
    decimal-dollar fields (``yes_price_dollars``/``no_price_dollars``).  Either is
    accepted; neither is required, because Phase 0 never reports monetary values.
    """
    cents_key = "yes_price" if side is Side.YES else "no_price"
    dollars_key = "yes_price_dollars" if side is Side.YES else "no_price_dollars"

    cents = raw.get(cents_key)
    if isinstance(cents, int) and not isinstance(cents, bool):
        return cents

    dollars = raw.get(dollars_key)
    if isinstance(dollars, (str, float, int)) and not isinstance(dollars, bool):
        try:
            return round(float(dollars) * 100)
        except (TypeError, ValueError):
            return None
    return None


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

    count = raw.get("count")
    count_fp_raw = raw.get("count_fp")
    if isinstance(count, bool) or not isinstance(count, int):
        count = None
    if count_fp_raw is not None and not isinstance(count_fp_raw, str):
        count_fp_raw = str(count_fp_raw)
    if count is None and count_fp_raw is None:
        raise SchemaError("fill carried neither 'count' nor 'count_fp'")

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
        count_fp_raw=count_fp_raw,
        price_cents=_parse_price_cents(raw, side),
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
