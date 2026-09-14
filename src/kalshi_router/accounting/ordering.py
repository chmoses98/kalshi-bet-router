"""Deterministic fill ordering.

Position accounting is **path dependent**: the same fills applied in a different
order can produce a different final state, so the ordering rule is part of the
correctness argument, not an implementation detail.

Ordering rule
-------------
1. **Execution time**, from ``created_time`` (RFC3339, as Kalshi reports it),
   falling back to ``ts`` (Unix seconds) when ``created_time`` is absent.
2. **``fill_id`` ascending** as the tie-breaker when two fills share a timestamp.

Why not API response order?  The fills endpoint is cursor-paginated and ordered
newest-first, and a page boundary can be crossed while new fills arrive.  Relying
on arrival order would make the accounting depend on when the audit ran.  A fill
with no usable timestamp at all **fails closed** rather than being appended
arbitrarily, because an arbitrary position in a path-dependent replay is a wrong
answer that looks like a right one.

``created_time`` and ``ts`` are compared on a single normalized axis (seconds
since the epoch as an exact :class:`~decimal.Decimal`) so that a mixed stream
still sorts coherently.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from ..errors import SchemaError
from ..models import NormalizedFill

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def parse_execution_time(fill: NormalizedFill) -> Decimal:
    """Return the fill's execution time as exact seconds since the epoch.

    Fails closed when neither timestamp field is usable.
    """
    raw = fill.created_time
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        # ``fromisoformat`` accepts a trailing 'Z' from Python 3.11 onward, but
        # normalize it anyway so the parse does not depend on the interpreter.
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                # Kalshi reports UTC; an offset-naive value is treated as UTC
                # rather than as local time, which would vary by machine.
                parsed = parsed.replace(tzinfo=timezone.utc)
            return Decimal(str((parsed - EPOCH).total_seconds()))

    if fill.ts is not None:
        return Decimal(fill.ts)

    raise SchemaError(
        "fill carried no usable execution timestamp; refusing to order it "
        "arbitrarily in a path-dependent replay"
    )


def canonical_fill_sort_key(fill: NormalizedFill) -> tuple[Decimal, str]:
    """``(execution_time, fill_id)`` -- the total order used for every replay."""
    return (parse_execution_time(fill), fill.fill_id)


def sort_fills(fills: Iterable[NormalizedFill]) -> list[NormalizedFill]:
    """Return the fills in canonical replay order.

    Sorting is total (the ``fill_id`` tie-break has no duplicates once fills are
    de-duplicated), so shuffled input produces identical output.
    """
    return sorted(fills, key=canonical_fill_sort_key)
