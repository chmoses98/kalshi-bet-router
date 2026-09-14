"""Exact decimal parsing for Kalshi's fixed-point fields.

Kalshi's Q1-2026 fixed-point migration replaced the legacy integer fields with
decimal strings:

* ``count_fp``            -- contract quantity, e.g. ``"10.00"`` == **10 contracts**.
                            Fractional contracts are representable.
* ``yes_price_dollars`` / ``no_price_dollars``
                         -- price in dollars, e.g. ``"0.6500"``. Some markets use
                            sub-penny ticks as small as $0.001, so an integer-cent
                            field cannot represent them.

The legacy integer ``count`` / ``yes_price`` / ``no_price`` fields were removed in
that migration, which is why the first live Phase 0 audit found *every* fill
carrying ``count_fp`` and no ``count``.

**Binary floating point is never used here.** Every quantity and price is parsed
into :class:`decimal.Decimal` from its string form, so ``"0.6500"`` round-trips
exactly and reconciliation against Kalshi's own accumulator arithmetic stays
possible. A JSON number is tolerated but routed through ``repr`` and counted, so
a schema drift toward floats is visible rather than silent.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple

from .errors import SchemaError

#: Kalshi's documented shape: an optionally signed decimal, no exponent notation.
FIXED_POINT_PATTERN = re.compile(r"^-?\d+(?:\.\d+)?$")


class ParsedDecimal(NamedTuple):
    """A parsed value plus how it arrived, for privacy-safe schema diagnostics."""

    value: Decimal
    #: ``"string"`` for the documented form, ``"number"`` when JSON sent a number.
    source_type: str


def parse_fixed_point(raw: Any, field: str) -> ParsedDecimal | None:
    """Parse one fixed-point field exactly, or return ``None`` when absent.

    Fails closed on a present-but-uninterpretable value: a malformed quantity is
    never silently treated as missing, because that would understate an account.
    """
    if raw is None or raw == "":
        return None

    if isinstance(raw, bool):
        raise SchemaError(f"field {field!r} was a boolean, expected a decimal string")

    if isinstance(raw, str):
        text = raw.strip()
        if not FIXED_POINT_PATTERN.match(text):
            raise SchemaError(
                f"field {field!r} was not a valid fixed-point decimal string"
            )
        try:
            return ParsedDecimal(Decimal(text), "string")
        except InvalidOperation:
            raise SchemaError(f"field {field!r} could not be parsed as a decimal") from None

    if isinstance(raw, int):
        return ParsedDecimal(Decimal(raw), "number")

    if isinstance(raw, float):
        # Tolerated but flagged: routed through repr so no binary rounding is
        # baked in, and counted so a drift toward JSON numbers is observable.
        try:
            return ParsedDecimal(Decimal(repr(raw)), "number")
        except InvalidOperation:
            raise SchemaError(f"field {field!r} could not be parsed as a decimal") from None

    raise SchemaError(
        f"field {field!r} was {type(raw).__name__}, expected a decimal string"
    )
