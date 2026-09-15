"""How long does a real order take to finish filling?

The stabilization window in :mod:`kalshi_router.production` decides when an
order on a still-open market is safe to import. Picking that number by intuition
is exactly the move this project keeps having to unlearn, so it is measured
instead: across every order the account has ever submitted, how long elapsed
between its first fill and its last?

A window set far above the observed maximum describes behaviour this account has
actually exhibited. A window set from a guess describes behaviour that seems
plausible, which is a different and much weaker claim.

Privacy: buckets and counts only. A span in seconds is a property of the
exchange's matching, not of the wager -- it carries no ticker, no size and no
price.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from .accounting.execution import OrderExecution

#: Upper edges, in seconds, of the buckets the span distribution is reported in.
#: Chosen to straddle the decision: if everything lands in the first few buckets
#: a short window is safe, and anything in the last bucket is a direct warning
#: that no window is.
BUCKET_EDGES: tuple[Decimal, ...] = (
    Decimal(0),
    Decimal(1),
    Decimal(10),
    Decimal(60),
    Decimal(300),
    Decimal(900),
    Decimal(3600),
    Decimal(86400),
)


@dataclass
class FinalityEvidence:
    """The observed first-to-last fill span of every order, in buckets."""

    orders: int = 0
    single_fill_orders: int = 0
    multi_fill_orders: int = 0

    #: Bucket upper edge (seconds) -> count. Keys come from BUCKET_EDGES, never
    #: from data, so this stays a fixed-shape counts-only structure.
    spans: dict[str, int] = field(default_factory=dict)
    #: Orders whose span exceeded the largest bucket.
    spans_beyond_largest_bucket: int = 0

    #: The largest span seen, in whole seconds. A single number, and the one the
    #: window has to clear.
    max_span_seconds: int = 0

    def bucket_counts(self) -> list[tuple[str, int]]:
        ordered = [(_label(edge), self.spans.get(_label(edge), 0)) for edge in BUCKET_EDGES]
        ordered.append(("beyond", self.spans_beyond_largest_bucket))
        return ordered

    def as_dict(self) -> dict[str, int]:
        payload: dict[str, int] = {
            "orders": self.orders,
            "single_fill_orders": self.single_fill_orders,
            "multi_fill_orders": self.multi_fill_orders,
            "max_span_seconds": self.max_span_seconds,
            "spans_beyond_largest_bucket": self.spans_beyond_largest_bucket,
        }
        for label, count in self.bucket_counts():
            payload[f"span_within_{label}"] = count
        return payload

    def window_is_supported(self, window_seconds: Decimal) -> bool:
        """Whether every observed order finished well inside ``window_seconds``.

        Strictly less than, not less than or equal: an order that took exactly
        the window would have been declared final at the instant it was still
        filling.
        """
        return self.orders > 0 and Decimal(self.max_span_seconds) < window_seconds

    def render(self) -> str:
        lines = [
            "order finality evidence (how long orders take to finish filling):",
            f"  orders observed: {self.orders}",
            f"    filled in one execution: {self.single_fill_orders}",
            f"    needed several executions: {self.multi_fill_orders}",
            "  first-to-last fill span:",
        ]
        for label, count in self.bucket_counts():
            lines.append(f"    within {label}: {count}")
        lines += [
            f"  largest span observed (seconds): {self.max_span_seconds}",
            "",
            "  NOTE: this is what sets the stabilization window. A window above the",
            "        largest span describes behaviour this account has exhibited; a",
            "        window chosen without it describes behaviour that seems likely.",
        ]
        return "\n".join(lines)


def _label(edge: Decimal) -> str:
    return f"{int(edge)}s"


def measure_finality(orders: Iterable[OrderExecution]) -> FinalityEvidence:
    """Bucket the first-to-last fill span of every order."""
    evidence = FinalityEvidence()
    for order in orders:
        evidence.orders += 1
        if order.fill_count > 1:
            evidence.multi_fill_orders += 1
        else:
            evidence.single_fill_orders += 1

        span = order.last_execution_time - order.first_execution_time
        if span < 0:  # pragma: no cover - ordering guarantees this
            span = Decimal(0)
        evidence.max_span_seconds = max(evidence.max_span_seconds, int(span))

        for edge in BUCKET_EDGES:
            if span <= edge:
                label = _label(edge)
                evidence.spans[label] = evidence.spans.get(label, 0) + 1
                break
        else:
            evidence.spans_beyond_largest_bucket += 1
    return evidence
