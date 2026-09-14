"""Reconciliation probe: what the exchange says we hold, versus what we replayed.

A fills replay cannot prove its own completeness -- a missing page and an absent
trade look identical from inside it.  ``GET /portfolio/positions`` is the
external check, because it states the net position per ticker without depending
on our reading of history.

This module only **measures** the disagreement.  It changes no accounting and
emits nothing downstream, because two of its inputs are still unverified:

* the settlement schema, and
* whether settled markets remain in the positions response at all.

Guessing either would produce a confident-looking reconciliation built on an
assumption, which is worse than a measured gap.  So the shapes are observed
first, exactly as the fill schema was.

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

    # --- comparison against the replay (set sizes, never tickers)
    replayed_markets: int = 0
    markets_in_both: int = 0
    markets_only_in_replay: int = 0
    markets_only_in_positions: int = 0
    markets_agreeing_on_quantity: int = 0
    markets_disagreeing_on_quantity: int = 0
    #: Replayed non-zero, exchange says flat: the settlement signature.
    markets_replay_open_exchange_flat: int = 0

    #: Observed schema, allowlisted.
    position_keys: tuple[str, ...] = ()
    settlement_keys: tuple[str, ...] = ()
    settlement_results: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self))

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
            "",
            f"  position keys observed: {', '.join(self.position_keys) or '(none)'}",
            f"  settlement keys observed: {', '.join(self.settlement_keys) or '(none)'}",
            f"  settlement results observed: {', '.join(self.settlement_results) or '(none)'}",
            "",
            "  NOTE: this is a MEASUREMENT, not a reconciliation verdict. The",
            "        settlement schema is not yet verified, so a disagreement here",
            "        does not yet distinguish missing history from a settled",
            "        market. Nothing downstream may depend on these numbers.",
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
    for row in settlements:
        report.settlement_rows += 1
        result = row.get("result")
        if isinstance(result, str) and result.strip():
            report.settlement_rows_with_result += 1
            label = result.strip().lower()
            results[label if label in _KNOWN_RESULTS else "other"] = None
        else:
            report.settlement_rows_without_result += 1

    report.replayed_markets = len(replayed)
    for ticker, net in replayed.items():
        if ticker not in exchange:
            report.markets_only_in_replay += 1
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
