"""Reconciliation probe: what the exchange says we hold, versus what we replayed.

A fills replay cannot prove its own completeness -- a missing page and an absent
trade look identical from inside it.  ``GET /portfolio/positions`` is the
external check, because it states the net position per ticker without depending
on our reading of history.

This module only **measures** the disagreement.  It changes no accounting and
emits nothing downstream.

The first live run answered both of the questions it was built to ask, and
corrected this module's own assumptions in the process:

* the settlement result field is ``market_result``, not ``result`` -- 755 of 755
  settlement rows were counted as "no result" purely because of that guess;
* **a settled market is absent from the positions response entirely**, not
  present with a zero quantity.  The account reported 0 position rows against
  155 replayed markets and 755 settlements.

So "replay says open, exchange says flat" can never fire, and the real settlement
signature is *replayed, absent from positions, present in settlements*.  That is
what is measured now.  Being wrong about this in the other direction -- assuming
a missing ticker meant missing history -- would have condemned an entire, intact
account as unreconcilable.

Everything reported is a count or a public schema name.  No ticker, quantity,
price or balance can reach this output: ticker-keyed comparisons are reduced to
set sizes, and field names pass the same allowlist the taxonomy diagnostic uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from .errors import SchemaError
from .fixedpoint import parse_fixed_point
from .safety import safe_schema_name

#: Settlement/position enum-ish labels we are willing to echo.  Anything else is
#: bucketed, so an unexpected value cannot print itself into a public log.
_KNOWN_RESULTS = frozenset({"yes", "no", "scalar", "void", "all_no", "all_yes", ""})

#: Where the settlement outcome lives.  ``market_result`` is the observed live
#: field; ``result`` is kept as a defensive alias, not as a guess.
_RESULT_KEYS = ("market_result", "result")

#: Settlement economics, verified present on the live rows.  A settlement
#: carries its own per-leg quantities and cost, so it is a complete accounting
#: event rather than a bare notification.
_SETTLEMENT_ECONOMICS = (
    "value",
    "revenue",
    "yes_count_fp",
    "no_count_fp",
    "yes_total_cost_dollars",
    "no_total_cost_dollars",
    "fee_cost",
    "settled_time",
)


@dataclass
class ReconciliationReport:
    """Counts and schema names only.  Safe for a public Actions log."""

    # --- positions endpoint
    position_rows: int = 0
    position_rows_with_quantity: int = 0
    position_rows_unparseable_quantity: int = 0
    position_rows_with_realized_pnl: int = 0
    position_rows_with_fees_paid: int = 0
    position_rows_nonzero: int = 0
    position_rows_zero: int = 0

    # --- settlements endpoint
    settlement_rows: int = 0
    settlement_rows_with_result: int = 0
    settlement_rows_without_result: int = 0
    settlement_markets: int = 0
    #: Per-field presence across settlement rows, so a schema drift is visible.
    settlement_field_coverage: dict[str, int] = field(default_factory=dict)

    # --- comparison against the replay (set sizes, never tickers)
    replayed_markets: int = 0
    markets_in_both: int = 0
    markets_only_in_replay: int = 0
    markets_only_in_positions: int = 0
    markets_agreeing_on_quantity: int = 0
    markets_disagreeing_on_quantity: int = 0
    #: Replayed non-zero, exchange says flat.  Observed to be ZERO on live data:
    #: settled markets leave the positions response rather than going to zero.
    markets_replay_open_exchange_flat: int = 0
    #: The real settlement signature: replayed, absent from positions, and
    #: accounted for by a settlement row.
    markets_absent_but_settled: int = 0
    #: Replayed, absent from positions, and NOT explained by a settlement. This
    #: is the one that would mean missing history.
    markets_absent_and_unexplained: int = 0

    #: Observed schema, allowlisted.
    position_keys: tuple[str, ...] = ()
    settlement_keys: tuple[str, ...] = ()
    settlement_results: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Flatten so every emitted value stays scalar or a name tuple."""
        out = {k: v for k, v in vars(self).items() if k != "settlement_field_coverage"}
        for name, count in sorted(self.settlement_field_coverage.items()):
            out[f"settlement_field_{name}"] = count
        return out

    def render(self) -> str:
        lines = [
            "reconciliation probe (counts only; no routing, no persistence):",
            f"  position rows: {self.position_rows}",
            f"    with a parseable quantity: {self.position_rows_with_quantity}",
            f"    with an unparseable quantity: {self.position_rows_unparseable_quantity}",
            f"    reporting realized pnl: {self.position_rows_with_realized_pnl}",
            f"    reporting fees paid: {self.position_rows_with_fees_paid}",
            f"    non-zero: {self.position_rows_nonzero}",
            f"    flat: {self.position_rows_zero}",
            "",
            f"  settlement rows: {self.settlement_rows}",
            f"    distinct markets settled: {self.settlement_markets}",
            f"    with a result: {self.settlement_rows_with_result}",
            f"    without a result: {self.settlement_rows_without_result}",
            "",
            "  replay vs exchange (market counts, never tickers):",
            f"    markets in the replay: {self.replayed_markets}",
            f"    in both: {self.markets_in_both}",
            f"    only in the replay: {self.markets_only_in_replay}",
            f"    only in the exchange's positions: {self.markets_only_in_positions}",
            f"    agreeing on net quantity: {self.markets_agreeing_on_quantity}",
            f"    DISAGREEING on net quantity: {self.markets_disagreeing_on_quantity}",
            f"    replay says open, exchange says flat: "
            f"{self.markets_replay_open_exchange_flat}",
            f"    absent from positions, EXPLAINED by a settlement: "
            f"{self.markets_absent_but_settled}",
            f"    absent from positions, UNEXPLAINED: "
            f"{self.markets_absent_and_unexplained}",
            "",
            f"  position keys observed: {', '.join(self.position_keys) or '(none)'}",
            f"  settlement keys observed: {', '.join(self.settlement_keys) or '(none)'}",
            f"  settlement results observed: {', '.join(self.settlement_results) or '(none)'}",
            "  settlement economics coverage:",
            *(
                f"    {name}: {self.settlement_field_coverage.get(name, 0)}"
                for name in _SETTLEMENT_ECONOMICS
            ),
            "",
            "  NOTE: this is a MEASUREMENT, not a reconciliation verdict. A",
            "        settled market leaves the positions response entirely, so",
            "        'absent' is only evidence of missing history when NO",
            "        settlement explains it. Nothing downstream may depend on",
            "        these numbers yet.",
        ]
        return "\n".join(lines)


#: Field names that may carry the signed position quantity, most current first.
_QUANTITY_KEYS = ("position_fp", "position")
_TICKER_KEYS = ("ticker", "market_ticker")


def _row_ticker(row: dict[str, Any]) -> str | None:
    for key in _TICKER_KEYS:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _row_quantity(row: dict[str, Any]) -> tuple[Decimal | None, bool]:
    """Return ``(quantity, was_present)``.

    A present-but-unparseable quantity is reported as unparseable rather than
    treated as absent: those are different faults and must not merge.
    """
    for key in _QUANTITY_KEYS:
        if key not in row or row[key] is None:
            continue
        try:
            parsed = parse_fixed_point(row[key], key)
        except SchemaError:
            return None, True
        if parsed is not None:
            return parsed.value, True
    return None, False


def _collect_keys(rows: Iterable[dict[str, Any]], limit: int = 40) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for row in rows:
        for key in row:
            name = safe_schema_name(key) if isinstance(key, str) else None
            if name is not None and name not in seen and len(seen) < limit:
                seen[name] = None
    return tuple(sorted(seen))


def probe_reconciliation(
    position_rows: Iterable[dict[str, Any]],
    settlement_rows: Iterable[dict[str, Any]],
    replayed: dict[str, Decimal],
) -> ReconciliationReport:
    """Measure the gap between a fills replay and the exchange's own view.

    ``replayed`` maps ticker -> signed net position derived from fills.
    """
    report = ReconciliationReport()

    positions = [row for row in position_rows if isinstance(row, dict)]
    settlements = [row for row in settlement_rows if isinstance(row, dict)]

    exchange: dict[str, Decimal] = {}
    for row in positions:
        report.position_rows += 1
        quantity, present = _row_quantity(row)
        if quantity is not None:
            report.position_rows_with_quantity += 1
        elif present:
            report.position_rows_unparseable_quantity += 1
        if row.get("realized_pnl_dollars") is not None:
            report.position_rows_with_realized_pnl += 1
        if row.get("fees_paid_dollars") is not None:
            report.position_rows_with_fees_paid += 1
        if quantity is not None:
            if quantity == 0:
                report.position_rows_zero += 1
            else:
                report.position_rows_nonzero += 1
        ticker = _row_ticker(row)
        if ticker is not None and quantity is not None:
            exchange[ticker] = quantity

    results: dict[str, None] = {}
    settled_tickers: set[str] = set()
    for row in settlements:
        report.settlement_rows += 1
        result = None
        for key in _RESULT_KEYS:
            candidate = row.get(key)
            if isinstance(candidate, str) and candidate.strip():
                result = candidate.strip().lower()
                break
        if result is not None:
            report.settlement_rows_with_result += 1
            results[result if result in _KNOWN_RESULTS else "other"] = None
        else:
            report.settlement_rows_without_result += 1

        for name in _SETTLEMENT_ECONOMICS:
            if row.get(name) is not None:
                report.settlement_field_coverage[name] = (
                    report.settlement_field_coverage.get(name, 0) + 1
                )

        ticker = _row_ticker(row)
        if ticker is not None:
            settled_tickers.add(ticker)
    report.settlement_markets = len(settled_tickers)

    report.replayed_markets = len(replayed)
    for ticker, net in replayed.items():
        if ticker not in exchange:
            report.markets_only_in_replay += 1
            # Absent is not automatically "missing history": a settled market
            # leaves the positions response. Only an absence that NO settlement
            # explains is evidence of a gap.
            if ticker in settled_tickers:
                report.markets_absent_but_settled += 1
            else:
                report.markets_absent_and_unexplained += 1
            continue
        report.markets_in_both += 1
        if exchange[ticker] == net:
            report.markets_agreeing_on_quantity += 1
        else:
            report.markets_disagreeing_on_quantity += 1
            if net != 0 and exchange[ticker] == 0:
                report.markets_replay_open_exchange_flat += 1
    report.markets_only_in_positions = sum(1 for t in exchange if t not in replayed)

    report.position_keys = _collect_keys(positions)
    report.settlement_keys = _collect_keys(settlements)
    report.settlement_results = tuple(sorted(results))
    return report
