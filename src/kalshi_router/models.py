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

#: Fees are a non-negative cost.  Kalshi's schedule is
#: ``ceil(0.07 * P * (1-P) * contracts)`` for takers and about a quarter of that
#: for makers, so a fee is never negative and may legitimately be zero.
MIN_FEE_DOLLARS = Decimal("0")


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


def _require_non_negative_fee(value: Decimal, field: str) -> Decimal:
    """A fee is a cost; a negative one means the field is not what we think."""
    if value < MIN_FEE_DOLLARS:
        raise SchemaError(f"field {field!r} was a negative fee")
    return value


def _require_price_in_range(value: Decimal, field: str) -> Decimal:
    """A contract price must sit strictly between $0 and $1."""
    if not (MIN_PRICE_DOLLARS < value < MAX_PRICE_DOLLARS):
        raise SchemaError(
            f"field {field!r} was outside the valid contract price range "
            f"(must be greater than $0 and less than $1)"
        )
    return value


class OutcomeSide(str, Enum):
    """**Canonical** direction: which outcome the member ended up positioned for.

    Kalshi's ``order_direction`` documentation states that ``outcome_side`` and
    ``book_side`` "carry the same bit in two vocabularies", and that new
    integrations should read only those two fields.  ``outcome_side`` has already
    absorbed the buy/sell distinction, so it alone fixes the sign:

    * ``yes`` -> long YES exposure  (positive on the signed axis)
    * ``no``  -> long NO exposure   (negative on the signed axis)

    Hence the documented equivalences: *buy yes* and *sell no* both report
    ``yes``; *buy no* and *sell yes* both report ``no``.
    """

    YES = "yes"
    NO = "no"


class BookSide(str, Enum):
    """The same direction bit in order-book vocabulary.

    ``bid`` pairs with ``outcome_side=yes`` and ``ask`` with ``outcome_side=no``.
    It corroborates the canonical direction and never independently overrides it.
    """

    BID = "bid"
    ASK = "ask"


#: The documented pairing between the two canonical vocabularies.
BOOK_SIDE_TO_OUTCOME: dict[BookSide, OutcomeSide] = {
    BookSide.BID: OutcomeSide.YES,
    BookSide.ASK: OutcomeSide.NO,
}


class Action(str, Enum):
    """DEPRECATED legacy execution verb.

    Kalshi deprecated ``action`` and ``side`` on the Fill schema (2026-05-14),
    with removal not before 2026-05-28.  Retained only so historical payloads
    still parse and so a legacy representation can be cross-checked against the
    canonical fields; never used to derive signed inventory.
    """

    BUY = "buy"
    SELL = "sell"


class Side(str, Enum):
    """DEPRECATED legacy leg of the binary contract.  See :class:`Action`."""

    YES = "yes"
    NO = "no"


def sources_to_fields(sources: tuple[str, ...]) -> tuple[str, ...]:
    """Identity helper: the direction sources are already field names."""
    return sources


def outcome_from_legacy(action: Action, side: Side) -> OutcomeSide:
    """Project the deprecated ``(action, side)`` pair onto canonical direction.

    Documented equivalences: buying YES and selling NO both leave the member
    positioned for YES; buying NO and selling YES both leave them positioned
    for NO.
    """
    positioned_for_yes = (action is Action.BUY) == (side is Side.YES)
    return OutcomeSide.YES if positioned_for_yes else OutcomeSide.NO


@dataclass(frozen=True)
class NormalizedFill:
    """One execution, normalized and kept in memory only.

    Direction comes from :class:`OutcomeSide`, Kalshi's canonical field.  The
    deprecated ``action``/``side`` pair is retained as metadata for historical
    analysis but never drives signed inventory.

    Quantities, prices and fees are :class:`~decimal.Decimal`, parsed exactly
    from Kalshi's fixed-point decimal strings.  Binary floating point is never
    used for a financial quantity.
    """

    fill_id: str
    ticker: str
    #: Canonical direction. Positive exposure when ``YES``, negative when ``NO``.
    outcome_side: OutcomeSide
    #: Corroborating book vocabulary, when the payload carried it.
    book_side: BookSide | None = None
    #: DEPRECATED legacy fields, preserved only as evidence of the original
    #: execution representation.
    legacy_action: Action | None = None
    legacy_side: Side | None = None
    #: Which fields the direction was proven from, for schema diagnostics.
    direction_sources: tuple[str, ...] = ()

    order_id: str | None = None
    trade_id: str | None = None
    #: Subaccount this execution belongs to. ``None`` when the payload omitted it;
    #: a ``None`` subaccount is never merged with a numbered one.
    subaccount_number: int | None = None

    count: Decimal | None = None
    count_source: str | None = None
    #: The fill's **unified execution price**, in dollars.  Direction lives in
    #: ``outcome_side``; the price is never complemented because of it.
    price_dollars: Decimal | None = None
    price_source: str | None = None
    #: True for a legacy-only payload, where the historic price semantics are
    #: not established, so economics are marked incomplete rather than guessed.
    legacy_price_semantics_unproven: bool = False
    fixed_point_number_typed: bool = False
    #: Exchange-reported fee for this execution, in dollars.  ``None`` when the
    #: fill carried no fee field -- never reconstructed from the fee schedule.
    fee_dollars: Decimal | None = None
    fee_source: str | None = None
    is_taker: bool | None = None
    created_time: str | None = None
    ts: int | None = None

    @property
    def has_fee(self) -> bool:
        return self.fee_dollars is not None

    @property
    def position_key(self) -> tuple[int | None, str]:
        """Accounting identity: positions never net across subaccounts."""
        return (self.subaccount_number, self.ticker)


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


def _parse_price(
    raw: dict[str, Any], canonical: bool
) -> tuple[Decimal | None, str | None, bool, bool]:
    """Resolve the **unified execution price**, in dollars.

    Kalshi's ``order_direction`` documentation is explicit: *"outcome_side
    describes directional exposure only; it does not change the order's price.
    An order at price p with outcome_side=no is matched by an order at the same
    price p with outcome_side=yes: both parties trade at the same price, just on
    opposite directions."*

    So a fill has **one** execution price on a single axis.  Direction lives
    entirely in ``outcome_side``; the price is never complemented because of it.
    Complementing here and then applying the signed position factor in the
    ledger would transform the same value twice.

    Returns ``(price, source, number_typed, legacy_semantics_unproven)``.
    """
    if not canonical:
        # Legacy-only payload (no outcome_side/book_side).  Whether the historic
        # yes_price/no_price pair was a unified price or complementary leg
        # prices is not established by current documentation, so the economics
        # are marked unproven rather than guessed.  Direction still resolves
        # from the deprecated action/side pair.
        return None, None, False, True

    quoted: list[tuple[Decimal, str, bool]] = []
    for key in ("yes_price_dollars", "no_price_dollars"):
        parsed = parse_fixed_point(raw.get(key), key)
        if parsed is not None:
            quoted.append((
                _require_price_in_range(parsed.value, key),
                "unified_price_dollars",
                parsed.source_type == "number",
            ))

    if not quoted:
        for key in ("yes_price", "no_price"):
            cents = raw.get(key)
            if isinstance(cents, int) and not isinstance(cents, bool):
                if not (MIN_PRICE_CENTS < cents < MAX_PRICE_CENTS):
                    raise SchemaError(
                        f"field {key!r} was outside the valid contract price range "
                        f"(must be greater than 0 and less than 100 cents)"
                    )
                quoted.append((Decimal(cents) / Decimal(100), "legacy_price_cents", False))

    if not quoted:
        return None, None, False, False

    values = {value for value, _, _ in quoted}
    if len(values) > 1:
        # Under the current contract both fields carry the same unified price.
        # Disagreement means this payload is not what the contract describes.
        raise SchemaError(
            "canonical fill reported conflicting execution prices in "
            "'yes_price_dollars' and 'no_price_dollars'; the current contract "
            "requires one unified price"
        )

    value, source, number_typed = quoted[0]
    return value, source, number_typed, False


def _parse_fee(raw: dict[str, Any]) -> tuple[Decimal | None, str | None]:
    """Resolve the exchange-reported fee, in dollars.

    ``fee_cost`` is the field Kalshi currently publishes on Get Fills, Get
    Settlements and the user-fills websocket, as a **fixed-point decimal dollar
    string** (``"0.5600"``, ``"0.010000"``).  It carries sub-cent precision
    because Kalshi's fee rounding math runs to six decimal places.

    Unit disambiguation is by JSON type, not by guesswork:

    * ``fee_cost`` as a **string** -> current schema -> **dollars**.
    * ``fee_cost`` as an **integer** -> pre-fixed-point legacy schema ->
      **integer cents**, converted exactly.

    The same field name is never read as both units for the same value shape.
    The ``*_dollars`` aliases are accepted only as defensive spellings.

    Kalshi's published schedule (``ceil(0.07 * P * (1-P) * contracts)`` for
    takers, about a quarter for makers) is deliberately **not** implemented: a
    reconstruction is an estimate wearing the costume of a fact, and the
    multiplier varies by market category.
    """
    raw_fee = raw.get("fee_cost")
    if isinstance(raw_fee, str) and raw_fee.strip():
        parsed = parse_fixed_point(raw_fee, "fee_cost")
        if parsed is not None:
            return _require_non_negative_fee(parsed.value, "fee_cost"), "fee_cost"
    elif isinstance(raw_fee, int) and not isinstance(raw_fee, bool):
        return (
            _require_non_negative_fee(Decimal(raw_fee), "fee_cost") / Decimal(100),
            "fee_cost_legacy_cents",
        )

    for key in ("fee_cost_dollars", "fee_dollars", "fees_paid_dollars"):
        parsed = parse_fixed_point(raw.get(key), key)
        if parsed is not None:
            return _require_non_negative_fee(parsed.value, key), key
    return None, None


def _resolve_direction(
    raw: dict[str, Any]
) -> tuple[OutcomeSide, BookSide | None, Action | None, Side | None, tuple[str, ...]]:
    """Determine canonical direction, failing closed on any disagreement.

    Precedence is ``outcome_side`` first, because Kalshi documents it as
    canonical.  ``book_side`` carries the same bit in book vocabulary and
    corroborates it.  The deprecated ``action``/``side`` pair is validated
    against the canonical answer and kept only as metadata.

    Legacy-only payloads are still accepted: the deprecated fields are not
    removed before 2026-05-28, and historical fills fetched from
    ``GET /historical/fills`` may predate the canonical fields entirely.  Losing
    the ability to replay history would be a worse failure than reading a
    documented deprecated field.
    """
    sources: list[str] = []
    candidates: dict[str, OutcomeSide] = {}

    raw_outcome = raw.get("outcome_side")
    if isinstance(raw_outcome, str) and raw_outcome.strip():
        try:
            candidates["outcome_side"] = OutcomeSide(raw_outcome.strip().lower())
        except ValueError:
            raise SchemaError(
                f"fill carried an unrecognized outcome_side; expected one of "
                f"{[o.value for o in OutcomeSide]}"
            ) from None

    book_side: BookSide | None = None
    raw_book = raw.get("book_side")
    if isinstance(raw_book, str) and raw_book.strip():
        try:
            book_side = BookSide(raw_book.strip().lower())
        except ValueError:
            raise SchemaError(
                f"fill carried an unrecognized book_side; expected one of "
                f"{[b.value for b in BookSide]}"
            ) from None
        candidates["book_side"] = BOOK_SIDE_TO_OUTCOME[book_side]

    action: Action | None = None
    side: Side | None = None
    raw_action = _first_present(raw, "action")
    raw_side = _first_present(raw, "side")
    if isinstance(raw_action, str) and isinstance(raw_side, str):
        try:
            action = Action(raw_action.strip().lower())
        except ValueError:
            raise SchemaError(
                f"fill carried an unrecognized action; expected one of "
                f"{[a.value for a in Action]}"
            ) from None
        try:
            side = Side(raw_side.strip().lower())
        except ValueError:
            raise SchemaError(
                f"fill carried an unrecognized side; expected one of "
                f"{[s.value for s in Side]}"
            ) from None
        candidates["legacy_action_side"] = outcome_from_legacy(action, side)

    if not candidates:
        raise SchemaError(
            "fill carried no direction evidence; expected 'outcome_side' "
            "(canonical), 'book_side', or the deprecated 'action'/'side' pair"
        )

    distinct = set(candidates.values())
    if len(distinct) > 1:
        disagreeing = ", ".join(sorted(candidates))
        raise SchemaError(
            f"fill direction fields disagree ({disagreeing}); refusing to guess "
            f"which representation is correct"
        )

    for name in ("outcome_side", "book_side", "legacy_action_side"):
        if name in candidates:
            sources.append(name)
    return next(iter(distinct)), book_side, action, side, tuple(sources)


#: Kalshi numbers the primary account 0 and named subaccounts 1-63.
MIN_SUBACCOUNT_NUMBER = 0
MAX_SUBACCOUNT_NUMBER = 63


def _parse_subaccount(raw: dict[str, Any]) -> int | None:
    """Read ``subaccount_number``, distinguishing **absent** from **malformed**.

    * absent                -> ``None`` (unknown bucket)
    * integer 0..63         -> that subaccount (0 is the primary account)
    * anything else present -> :class:`SchemaError`

    An **absent** field is legitimately unknown and is kept as its own bucket
    rather than assumed to be the primary account, because mapping it onto 0
    would silently merge genuinely separate subaccounts.

    A **present but malformed** value is a different thing entirely and must not
    collapse into that same ``None`` bucket: two corrupted values would then net
    together as if they were one account.  Malformed is never coerced.
    """
    if "subaccount_number" not in raw:
        return None
    value = raw["subaccount_number"]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchemaError(
            f"field 'subaccount_number' was {type(value).__name__}, expected an integer"
        )
    if not (MIN_SUBACCOUNT_NUMBER <= value <= MAX_SUBACCOUNT_NUMBER):
        raise SchemaError(
            f"field 'subaccount_number' was outside the valid range "
            f"{MIN_SUBACCOUNT_NUMBER}-{MAX_SUBACCOUNT_NUMBER}"
        )
    return value


def normalize_fill(raw: dict[str, Any]) -> NormalizedFill:
    """Convert one raw fill object into a :class:`NormalizedFill`.

    Fails closed on any required field that is absent or uninterpretable: fill
    id, market ticker, direction and quantity.  Direction comes from Kalshi's
    canonical ``outcome_side``/``book_side``; the deprecated ``action``/``side``
    pair is validated against it and kept only as metadata.
    """
    if not isinstance(raw, dict):
        raise SchemaError(f"fill was {type(raw).__name__}, expected object")

    fill_id = _first_present(raw, "fill_id", "id")
    if not isinstance(fill_id, str) or not fill_id:
        raise SchemaError("fill is missing a usable 'fill_id'")

    ticker = _first_present(raw, "ticker", "market_ticker")
    if not isinstance(ticker, str) or not ticker:
        raise SchemaError("fill is missing a usable market ticker")

    outcome, book_side, action, side, sources = _resolve_direction(raw)

    count, count_source, count_was_number = _parse_quantity(raw)
    if count is None:
        raise SchemaError("fill carried neither 'count_fp' nor a legacy 'count'")

    canonical = bool({"outcome_side", "book_side"} & set(sources_to_fields(sources)))
    price, price_source, price_was_number, price_unproven = _parse_price(raw, canonical)
    fee, fee_source = _parse_fee(raw)

    order_id = _first_present(raw, "order_id")
    trade_id = _first_present(raw, "trade_id")
    is_taker = raw.get("is_taker")
    ts = raw.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, int):
        ts = None

    return NormalizedFill(
        fill_id=fill_id,
        ticker=ticker,
        outcome_side=outcome,
        book_side=book_side,
        legacy_action=action,
        legacy_side=side,
        direction_sources=sources,
        order_id=order_id if isinstance(order_id, str) else None,
        trade_id=trade_id if isinstance(trade_id, str) else None,
        subaccount_number=_parse_subaccount(raw),
        count=count,
        count_source=count_source,
        price_dollars=price,
        price_source=price_source,
        legacy_price_semantics_unproven=price_unproven,
        fixed_point_number_typed=count_was_number or price_was_number,
        fee_dollars=fee,
        fee_source=fee_source,
        is_taker=is_taker if isinstance(is_taker, bool) else None,
        created_time=raw.get("created_time") if isinstance(raw.get("created_time"), str) else None,
        ts=ts,
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
