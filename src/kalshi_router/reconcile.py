"""Reconciliation probe: what the exchange says we hold, versus what we replayed.

A fills replay cannot prove its own completeness -- a missing page and an absent
trade look identical from inside it.  ``GET /portfolio/positions`` is the
external check, because it states the net position per ticker without depending
on our reading of history.

This module only **measures** the disagreement.  It changes no accounting and
emits nothing downstream.

Live runs have now corrected this module's own assumptions four times, which is
the point of measuring rather than deciding:

* the settlement result field is ``market_result``, not ``result`` -- 755 of 755
  settlement rows were counted as "no result" purely because of that guess;
* **a settled market is absent from the positions response entirely**, not
  present with a zero quantity.  The account reported 0 position rows against
  155 replayed markets and 755 settlements;
* ``revenue`` and ``value`` are **integer cents**, not dollars: every one of the
  355 paying settlements sits at cents par and none at dollar par, and all 359
  non-trivial ``value`` readings are exactly ``100``;
* ``value`` describes the **market** and ``revenue`` describes the **member**.
  They are not two views of one number, so rows where one is zero and the other
  is not are members holding the NO side, not a data fault.

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

    # --- settlement ECONOMICS semantics
    #
    # Replaying a settlement needs to know what `revenue` and `value` mean, and
    # getting that wrong would corrupt realized P&L on every settled wager. The
    # relationships below are measured rather than assumed. For a binary
    # contract the winning leg pays $1 per contract, so if `revenue` is a gross
    # dollar payout then revenue == the winning leg's count, exactly.
    settlements_revenue_at_binary_par: int = 0
    settlements_revenue_off_binary_par: int = 0
    settlements_revenue_zero: int = 0
    settlements_revenue_unparseable: int = 0
    #: A non-binary result has no binary par to be at, so its revenue is not a
    #: mismatch.  Counting it as one would make every scalar settlement look
    #: like a fault the moment tennis is enabled.
    settlements_revenue_non_binary_result: int = 0
    #: The cents reading: a $1 contract pays 100 cents, so revenue would equal
    #: the winning leg's count times 100.  Live data put ZERO rows at dollar
    #: par, so this is the competing hypothesis and it is tested, not assumed.
    settlements_revenue_at_cents_par: int = 0
    settlements_value_at_one: int = 0
    settlements_value_at_zero: int = 0
    settlements_value_strictly_between: int = 0
    settlements_value_above_one: int = 0
    #: A negative settlement value is a different and more alarming fault than
    #: an unexpectedly large one; they must not share a bucket.
    settlements_value_negative: int = 0
    #: One whole contract expressed in cents.
    settlements_value_at_one_hundred: int = 0
    #: `value` describes the MARKET (what a YES contract settled at); `revenue`
    #: describes the MEMBER (what they were paid).  So these two are not a
    #: disagreement -- they are the signature of holding the NO side, and the
    #: live arithmetic closes exactly on that reading.  Kept as named, because
    #: a row where they diverged for any OTHER reason would still land here.
    settlements_market_yes_member_unpaid: int = 0
    settlements_market_no_member_paid: int = 0
    settlements_cost_and_counts_both_present: int = 0
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
    #: The real settlement signature: replayed OPEN, absent from positions, and
    #: accounted for by a settlement row.
    markets_absent_but_settled: int = 0
    #: Replayed to flat and absent from positions: both sides agree the member
    #: holds nothing.  Counting this as a gap was over-reporting by 423 markets
    #: on the first full-history run.
    markets_absent_and_flat_in_replay: int = 0
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
            "  settlement economics semantics:",
            f"    revenue equals the winning leg count (binary par): "
            f"{self.settlements_revenue_at_binary_par}",
            f"    revenue away from binary par: {self.settlements_revenue_off_binary_par}",
            f"    revenue on a non-binary result (no par applies): "
            f"{self.settlements_revenue_non_binary_result}",
            f"    revenue equals the winning leg count x100 (CENTS par): "
            f"{self.settlements_revenue_at_cents_par}",
            f"    revenue is zero: {self.settlements_revenue_zero}",
            f"    revenue unparseable: {self.settlements_revenue_unparseable}",
            f"    value equals one: {self.settlements_value_at_one}",
            f"    value equals zero: {self.settlements_value_at_zero}",
            f"    value strictly between zero and one (SCALAR): "
            f"{self.settlements_value_strictly_between}",
            f"    value above one: {self.settlements_value_above_one}",
            f"    value NEGATIVE: {self.settlements_value_negative}",
            f"    value equals one hundred (a contract in cents): "
            f"{self.settlements_value_at_one_hundred}",
            f"    market settled YES, member unpaid (held NO and lost): "
            f"{self.settlements_market_yes_member_unpaid}",
            f"    market settled NO, member paid (held NO and won): "
            f"{self.settlements_market_no_member_paid}",
            f"    cost and counts both present: "
            f"{self.settlements_cost_and_counts_both_present}",
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
            f"    absent from positions, replay also flat (agreement): "
            f"{self.markets_absent_and_flat_in_replay}",
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


_ZERO = Decimal(0)
_ONE_DOLLAR = Decimal(1)
#: Kalshi marks dollar-valued fields with a ``_dollars`` suffix
#: (``yes_total_cost_dollars``).  ``revenue`` and ``value`` carry no such
#: suffix, and live data put zero rows at dollar par, so cents is the competing
#: reading -- tested here rather than adopted on the strength of a naming
#: convention alone.
_CENTS_PER_DOLLAR = Decimal(100)


def _quiet_decimal(raw: Any, name: str) -> Decimal | None:
    """Parse for diagnostics only; unparseable is simply not evidence."""
    if raw is None:
        return None
    try:
        parsed = parse_fixed_point(raw, name)
    except SchemaError:
        return None
    return None if parsed is None else parsed.value


def _observe_settlement_economics(
    row: dict[str, Any], result: str | None, report: ReconciliationReport
) -> None:
    """Measure what `revenue` and `value` actually mean.

    Replaying a settlement needs the payout, and taking it from the wrong field
    or the wrong unit would corrupt realized P&L on every settled wager.  A
    binary contract pays $1 per winning contract, so binary par -- revenue equal
    to the winning leg's count -- is a testable prediction rather than an
    assumption.  Only relationships are recorded; no amount is ever emitted.
    """
    yes_count = _quiet_decimal(row.get("yes_count_fp"), "yes_count_fp")
    no_count = _quiet_decimal(row.get("no_count_fp"), "no_count_fp")
    revenue_raw = row.get("revenue")
    revenue = _quiet_decimal(revenue_raw, "revenue")

    if revenue is None:
        if revenue_raw is not None:
            report.settlements_revenue_unparseable += 1
    elif revenue == _ZERO:
        report.settlements_revenue_zero += 1
    elif result not in ("yes", "no"):
        # scalar, void, absent -- there is no $1-per-contract par to compare
        # against, so this is not evidence either way about the unit.
        report.settlements_revenue_non_binary_result += 1
    else:
        winning = yes_count if result == "yes" else no_count
        if winning is not None and revenue == winning:
            report.settlements_revenue_at_binary_par += 1
        elif winning is not None and revenue == winning * _CENTS_PER_DOLLAR:
            report.settlements_revenue_at_cents_par += 1
        else:
            report.settlements_revenue_off_binary_par += 1

    value = _quiet_decimal(row.get("value"), "value")
    if value is not None:
        if value == _ONE_DOLLAR:
            report.settlements_value_at_one += 1
        elif value == _ZERO:
            report.settlements_value_at_zero += 1
        elif _ZERO < value < _ONE_DOLLAR:
            report.settlements_value_strictly_between += 1
        elif value < _ZERO:
            report.settlements_value_negative += 1
        else:
            report.settlements_value_above_one += 1
            if value == _CENTS_PER_DOLLAR:
                report.settlements_value_at_one_hundred += 1

    if revenue is not None and value is not None:
        if revenue == _ZERO and value != _ZERO:
            report.settlements_market_yes_member_unpaid += 1
        elif value == _ZERO and revenue != _ZERO:
            report.settlements_market_no_member_paid += 1

    has_cost = any(
        row.get(k) is not None
        for k in ("yes_total_cost_dollars", "no_total_cost_dollars")
    )
    if has_cost and (yes_count is not None or no_count is not None):
        report.settlements_cost_and_counts_both_present += 1


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

        _observe_settlement_economics(row, result, report)

        ticker = _row_ticker(row)
        if ticker is not None:
            settled_tickers.add(ticker)
    report.settlement_markets = len(settled_tickers)

    report.replayed_markets = len(replayed)
    for ticker, net in replayed.items():
        if ticker not in exchange:
            report.markets_only_in_replay += 1
            # Absence is only a gap when the replay still holds something.
            #
            # Three ways a market is legitimately absent from positions, and
            # only the fourth is evidence of missing history:
            #   * the replay closed it to zero by trading -- both sides agree
            #     the member holds nothing, which is agreement, not a gap;
            #   * a settlement closed it -- the exchange drops settled markets
            #     from the response entirely;
            #   * both of the above.
            # A market the replay still shows OPEN, with no settlement to
            # explain it, is the one that means the history is incomplete.
            if net == 0:
                report.markets_absent_and_flat_in_replay += 1
            elif ticker in settled_tickers:
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
