"""Live schema coverage probe.

Phase 0/1A parse fills **strictly**: a malformed fill raises, because silently
accepting a guessed value would corrupt accounting.  That is right for the
engine, and wrong for a diagnostic audit -- one odd fill would abort the whole
run and we would learn nothing about the shape of the live data.

This module keeps both properties.  It inspects each raw fill's field *shape*
without interpreting it, then asks :func:`normalize_fill` whether the fill is
acceptable.  A fill that fails is **excluded from accounting entirely** and
counted; it is never admitted with invented values.  So the audit observes the
live schema while the fail-closed guarantee is untouched.

Everything reported is a count.  No field here can hold a ticker, an id, a
price, a quantity or a subaccount number.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .accounting.ordering import parse_execution_time
from .errors import SchemaError
from .models import (
    MAX_SUBACCOUNT_NUMBER,
    MIN_SUBACCOUNT_NUMBER,
    NormalizedFill,
    _parse_fee,
    _parse_price,
    _parse_quantity,
    _parse_subaccount,
    _resolve_direction,
    normalize_fill,
)

#: Rejection buckets, derived from the probe's own field inspection rather than
#: from exception message text, so they cannot drift with wording.
REJECTION_CATEGORIES = (
    "direction",
    "quantity",
    "price",
    "fee",
    "subaccount",
    "timestamp",
    "identity",
    "other",
)


@dataclass
class SchemaCoverage:
    """Counts only.  Safe to print in a public Actions log."""

    fills_seen: int = 0
    fills_normalized: int = 0
    fills_rejected: int = 0

    # --- rejection reasons
    rejected_direction: int = 0
    rejected_quantity: int = 0
    rejected_price: int = 0
    rejected_fee: int = 0
    rejected_subaccount: int = 0
    rejected_timestamp: int = 0
    rejected_identity: int = 0
    rejected_other: int = 0

    # --- direction field coverage
    with_outcome_side: int = 0
    with_book_side: int = 0
    with_deprecated_action_side: int = 0
    canonical_only_fills: int = 0
    legacy_only_fills: int = 0
    direction_conflicts: int = 0

    # --- price field coverage
    with_yes_price_dollars: int = 0
    with_no_price_dollars: int = 0
    both_price_fields_present: int = 0
    price_fields_agreed: int = 0
    price_fields_disagreed: int = 0
    with_legacy_integer_price: int = 0
    legacy_price_unproven_fills: int = 0

    # --- fee coverage
    with_fee_cost_string: int = 0
    with_fee_cost_integer: int = 0
    without_any_fee_field: int = 0

    # --- subaccount coverage
    subaccount_present: int = 0
    subaccount_absent: int = 0
    subaccount_malformed: int = 0

    # --- ordering
    with_created_time: int = 0
    with_ts_only: int = 0
    without_any_timestamp: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(vars(self))

    def render(self) -> str:
        lines = [
            "live schema coverage (counts only):",
            f"  fills seen: {self.fills_seen}",
            f"  fills normalized: {self.fills_normalized}",
            f"  fills rejected (excluded from accounting): {self.fills_rejected}",
            f"    direction: {self.rejected_direction}",
            f"    quantity: {self.rejected_quantity}",
            f"    price: {self.rejected_price}",
            f"    fee: {self.rejected_fee}",
            f"    subaccount: {self.rejected_subaccount}",
            f"    timestamp: {self.rejected_timestamp}",
            f"    identity: {self.rejected_identity}",
            f"    other: {self.rejected_other}",
            "",
            "  direction fields:",
            f"    with outcome_side (canonical): {self.with_outcome_side}",
            f"    with book_side: {self.with_book_side}",
            f"    with deprecated action/side: {self.with_deprecated_action_side}",
            f"    canonical-only (no deprecated pair): {self.canonical_only_fills}",
            f"    legacy-only (no canonical fields): {self.legacy_only_fills}",
            f"    canonical-vs-legacy conflicts: {self.direction_conflicts}",
            "",
            "  price fields:",
            f"    with yes_price_dollars: {self.with_yes_price_dollars}",
            f"    with no_price_dollars: {self.with_no_price_dollars}",
            f"    both present: {self.both_price_fields_present}",
            f"    both present and agreeing: {self.price_fields_agreed}",
            f"    both present and DISAGREEING: {self.price_fields_disagreed}",
            f"    with legacy integer price: {self.with_legacy_integer_price}",
            f"    legacy price semantics unproven: {self.legacy_price_unproven_fills}",
            "",
            "  fee fields:",
            f"    fee_cost as decimal string: {self.with_fee_cost_string}",
            f"    fee_cost as integer (legacy cents): {self.with_fee_cost_integer}",
            f"    no fee field at all: {self.without_any_fee_field}",
            "",
            "  subaccount:",
            f"    present and valid: {self.subaccount_present}",
            f"    absent: {self.subaccount_absent}",
            f"    present but malformed: {self.subaccount_malformed}",
            "",
            "  ordering:",
            f"    with created_time: {self.with_created_time}",
            f"    with ts only: {self.with_ts_only}",
            f"    with no usable timestamp: {self.without_any_timestamp}",
        ]
        return "\n".join(lines)


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _decimal_like(value: Any) -> bool:
    return _is_nonempty_str(value) or (
        isinstance(value, (int, float)) and not isinstance(value, bool)
    )


def _classify_rejection(raw: dict[str, Any]) -> str:
    """Bucket a rejected fill by re-running each parser in isolation.

    Attribution comes from which parser actually refuses the value, not from
    exception message text (which would drift with wording) and not from field
    presence alone (which cannot see a domain violation such as a zero
    quantity).  The order mirrors :func:`normalize_fill`.
    """
    if not _is_nonempty_str(raw.get("fill_id")) and not _is_nonempty_str(raw.get("id")):
        return "identity"
    if not _is_nonempty_str(raw.get("ticker")) and not _is_nonempty_str(raw.get("market_ticker")):
        return "identity"

    try:
        _, _, _, _, sources = _resolve_direction(raw)
    except SchemaError:
        return "direction"

    try:
        _parse_subaccount(raw)
    except SchemaError:
        return "subaccount"

    try:
        quantity, _, _ = _parse_quantity(raw)
    except SchemaError:
        return "quantity"
    if quantity is None:
        return "quantity"

    canonical = bool({"outcome_side", "book_side"} & set(sources))
    try:
        _parse_price(raw, canonical)
    except SchemaError:
        return "price"

    try:
        _parse_fee(raw)
    except SchemaError:
        return "fee"

    if not _is_nonempty_str(raw.get("created_time")) and not isinstance(raw.get("ts"), int):
        return "timestamp"
    return "other"


def _observe_shape(raw: dict[str, Any], coverage: SchemaCoverage) -> None:
    """Record field presence.  Never interprets a value's meaning."""
    has_outcome = _is_nonempty_str(raw.get("outcome_side"))
    has_book = _is_nonempty_str(raw.get("book_side"))
    has_legacy = _is_nonempty_str(raw.get("action")) and _is_nonempty_str(raw.get("side"))
    coverage.with_outcome_side += has_outcome
    coverage.with_book_side += has_book
    coverage.with_deprecated_action_side += has_legacy
    if (has_outcome or has_book) and not has_legacy:
        coverage.canonical_only_fills += 1
    if has_legacy and not (has_outcome or has_book):
        coverage.legacy_only_fills += 1

    yes_p, no_p = raw.get("yes_price_dollars"), raw.get("no_price_dollars")
    coverage.with_yes_price_dollars += _decimal_like(yes_p)
    coverage.with_no_price_dollars += _decimal_like(no_p)
    if _decimal_like(yes_p) and _decimal_like(no_p):
        coverage.both_price_fields_present += 1
        if str(yes_p).strip() == str(no_p).strip():
            coverage.price_fields_agreed += 1
        else:
            coverage.price_fields_disagreed += 1
    if isinstance(raw.get("yes_price"), int) or isinstance(raw.get("no_price"), int):
        coverage.with_legacy_integer_price += 1

    fee = raw.get("fee_cost")
    if _is_nonempty_str(fee):
        coverage.with_fee_cost_string += 1
    elif isinstance(fee, int) and not isinstance(fee, bool):
        coverage.with_fee_cost_integer += 1
    elif not any(
        _decimal_like(raw.get(k)) for k in ("fee_cost_dollars", "fee_dollars", "fees_paid_dollars")
    ):
        coverage.without_any_fee_field += 1

    if "subaccount_number" not in raw or raw["subaccount_number"] is None:
        coverage.subaccount_absent += 1
    else:
        value = raw["subaccount_number"]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not (MIN_SUBACCOUNT_NUMBER <= value <= MAX_SUBACCOUNT_NUMBER)
        ):
            coverage.subaccount_malformed += 1
        else:
            coverage.subaccount_present += 1

    if _is_nonempty_str(raw.get("created_time")):
        coverage.with_created_time += 1
    elif isinstance(raw.get("ts"), int) and not isinstance(raw.get("ts"), bool):
        coverage.with_ts_only += 1
    else:
        coverage.without_any_timestamp += 1


def probe_fills(
    raw_fills: Iterable[dict[str, Any]]
) -> tuple[list[NormalizedFill], SchemaCoverage]:
    """Normalize what can be normalized; count everything.

    Returns the accepted fills and a privacy-safe coverage report.  A rejected
    fill is **excluded from accounting** -- this observes the live schema without
    admitting a single guessed value.
    """
    coverage = SchemaCoverage()
    accepted: list[NormalizedFill] = []

    for raw in raw_fills:
        coverage.fills_seen += 1
        if not isinstance(raw, dict):
            coverage.fills_rejected += 1
            coverage.rejected_other += 1
            continue

        _observe_shape(raw, coverage)
        try:
            fill = normalize_fill(raw)
        except SchemaError:
            coverage.fills_rejected += 1
            category = _classify_rejection(raw)
            setattr(
                coverage,
                f"rejected_{category}",
                getattr(coverage, f"rejected_{category}") + 1,
            )
            # A direction rejection carrying both representations is specifically
            # a canonical-vs-legacy conflict, worth seeing on its own.
            if category == "direction" and (
                _is_nonempty_str(raw.get("outcome_side"))
                and _is_nonempty_str(raw.get("action"))
                and _is_nonempty_str(raw.get("side"))
            ):
                coverage.direction_conflicts += 1
            continue

        # A fill with no usable execution timestamp cannot be placed in a
        # path-dependent replay.  Rejecting it here keeps one undated fill from
        # taking down the entire accounting pass.
        try:
            parse_execution_time(fill)
        except SchemaError:
            coverage.fills_rejected += 1
            coverage.rejected_timestamp += 1
            continue

        coverage.fills_normalized += 1
        if fill.legacy_price_semantics_unproven:
            coverage.legacy_price_unproven_fills += 1
        accepted.append(fill)

    return accepted, coverage
