"""Phase 6: compare the router's historical view against the ledger that exists.

THIS IS VALIDATION, NOT BACKFILL. Nothing in this module writes, sends, or
proposes a row. It answers one question about the past:

    If the router had been running, would it have produced what the ledger
    already says?

That question is worth asking because the two records were produced by
completely independent means. The MLB ledger's existing rows are
``entryMethod: LEGACY_BACKFILL`` -- assembled from the owner's own records --
while the router assembles wagers by replaying Kalshi's fills. Where they agree,
each corroborates the other. Where they disagree, exactly one of them is wrong,
and knowing which rows those are is worth more than any aggregate accuracy
number.

WHY THE COMPARISON HAPPENS IN MEMORY
------------------------------------
A shadow wager carries a ticker, a stake, a price and a payout. So the
comparison is computed here, inside the process that already holds those
values, and only :class:`LedgerComparison` -- which is structurally incapable of
holding one -- ever leaves. The alternative (emit both sides, diff them
elsewhere) would put the owner's betting history in a file that something
eventually prints.

WHAT IS AND IS NOT COMPARED
---------------------------
The match key is ``(marketTicker, side)``. Stake and price are deliberately NOT
part of the key: they are the thing being tested, and a key that included them
would report a disagreement as a non-match and quietly hide it in the
"router only" bucket.

Money is compared to the cent and price to a tenth of a cent, because both
records store dollars and the ledger's prices are volume-weighted averages that
can legitimately land between Kalshi's one-cent ticks.

MULTIPLICITY IS NOT FLATTENED
-----------------------------
If either side holds more than one row for a key, that key is counted as
ambiguous and is NOT scored for agreement. Summing two ledger rows to make them
match one router row would manufacture the agreement this module exists to
measure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

#: Dollar tolerance for stake agreement. Both records store dollars; a cent is
#: the smallest unit either can mean.
STAKE_TOLERANCE = Decimal("0.01")

#: Dollar tolerance for entry price. Kalshi ticks in whole cents, but a
#: multi-fill order has a volume-weighted average price that lands between
#: ticks, and both records round it independently.
PRICE_TOLERANCE = Decimal("0.005")


@dataclass
class LedgerComparison:
    """Counts only. Safe to print in a public Actions log.

    No field here can hold a ticker, a date, a stake or a price. That is a
    structural property, not a convention: every field is an int.
    """

    #: Rows read from the destination ledger for this sport.
    ledger_rows_read: int = 0
    #: ...of which were skipped before comparison, with the reason.
    ledger_rows_not_active: int = 0
    ledger_rows_other_platform: int = 0
    ledger_rows_other_sport: int = 0
    ledger_rows_without_ticker: int = 0
    #: Ledger rows that reached the comparison.
    ledger_rows_compared: int = 0

    #: Wagers the router built from Kalshi's own fills.
    router_wagers_built: int = 0
    #: ...of which carried no ticker, so they form no key. Structurally
    #: impossible for a ShadowWager (the field is required), counted anyway so
    #: that the key buckets below stay a partition of the real keys rather than
    #: quietly absorbing a malformed value.
    router_wagers_without_ticker: int = 0

    #: Keys present on both sides, exactly once each.
    matched_keys: int = 0
    #: Present only in the router's view: a wager the ledger does not record.
    router_only_keys: int = 0
    #: Present only in the ledger: a recorded bet the router cannot corroborate
    #: from Kalshi fills.
    ledger_only_keys: int = 0
    #: Present on both sides but more than once somewhere. Not scored.
    ambiguous_multiplicity_keys: int = 0

    #: Of the matched keys, how the money compares.
    stake_agrees: int = 0
    stake_disagrees: int = 0
    stake_not_comparable: int = 0
    price_agrees: int = 0
    price_disagrees: int = 0
    price_not_comparable: int = 0
    result_agrees: int = 0
    result_disagrees: int = 0
    result_not_comparable: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(vars(self))

    @property
    def keys_accounted(self) -> int:
        """Every key falls into exactly one bucket; this is the sum."""
        return (
            self.matched_keys
            + self.router_only_keys
            + self.ledger_only_keys
            + self.ambiguous_multiplicity_keys
        )

    def render(self) -> str:
        lines = [
            "HISTORICAL SHADOW COMPARISON (validation only -- nothing was written)",
            f"  ledger rows read: {self.ledger_rows_read}",
            f"    not ACTIVE: {self.ledger_rows_not_active}",
            f"    other platform: {self.ledger_rows_other_platform}",
            f"    other sport: {self.ledger_rows_other_sport}",
            f"    no market ticker: {self.ledger_rows_without_ticker}",
            f"    compared: {self.ledger_rows_compared}",
            f"  router wagers built: {self.router_wagers_built}",
            f"    without a ticker (no key): {self.router_wagers_without_ticker}",
            "  keys:",
            f"    matched: {self.matched_keys}",
            f"    router only: {self.router_only_keys}",
            f"    ledger only: {self.ledger_only_keys}",
            f"    ambiguous multiplicity (not scored): {self.ambiguous_multiplicity_keys}",
            "  agreement on matched keys:",
            f"    stake: {self.stake_agrees} agree / {self.stake_disagrees} disagree"
            f" / {self.stake_not_comparable} not comparable",
            f"    entry price: {self.price_agrees} agree / {self.price_disagrees} disagree"
            f" / {self.price_not_comparable} not comparable",
            f"    result: {self.result_agrees} agree / {self.result_disagrees} disagree"
            f" / {self.result_not_comparable} not comparable",
        ]
        return "\n".join(lines)


def _decimal(value: Any) -> Decimal | None:
    """Parse a money field, or refuse.

    ``None`` means NOT COMPARABLE, which is reported separately from a
    disagreement. A ledger row with a null stake does not disagree with the
    router; it says nothing.

    ``bool`` is rejected explicitly because ``isinstance(True, int)`` is true in
    Python and ``Decimal(True)`` is 1, which would silently score a boolean
    field as a dollar amount.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float, str)):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    return None


def _key(ticker: Any, side: Any) -> tuple[str, str] | None:
    if not isinstance(ticker, str) or not ticker.strip():
        return None
    if not isinstance(side, str) or not side.strip():
        return None
    return (ticker.strip().upper(), side.strip().upper())


def read_ledger(path: str) -> list[dict]:
    """Read a JSONL ledger. A malformed line raises rather than being skipped.

    Skipping would understate the ledger and flatter the router's agreement
    rate, which is the one direction this measurement must not be biased in.
    """
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"ledger line {number} is not valid JSON: {exc.msg}") from None
            if not isinstance(row, dict):
                raise ValueError(f"ledger line {number} is not an object")
            rows.append(row)
    return rows


def compare_to_ledger(
    wagers: Iterable[Any],
    ledger_rows: Iterable[dict],
    sport: str = "MLB",
    platform: str = "KALSHI",
) -> LedgerComparison:
    """Compare built wagers against ledger rows. Returns counts only.

    ``wagers`` are :class:`~kalshi_router.wager.ShadowWager` values -- or
    anything with the same ``market_ticker`` / ``side`` / ``stake`` /
    ``entry_price`` / ``result`` attributes.
    """
    comparison = LedgerComparison()

    ledger_by_key: dict[tuple[str, str], list[dict]] = {}
    for row in ledger_rows:
        comparison.ledger_rows_read += 1
        status = row.get("recordStatus")
        if status is not None and str(status).upper() != "ACTIVE":
            comparison.ledger_rows_not_active += 1
            continue
        row_platform = row.get("platform")
        if row_platform is not None and str(row_platform).upper() != platform.upper():
            comparison.ledger_rows_other_platform += 1
            continue
        row_sport = row.get("sport")
        if row_sport is not None and str(row_sport).upper() != sport.upper():
            comparison.ledger_rows_other_sport += 1
            continue
        key = _key(row.get("marketTicker"), row.get("side"))
        if key is None:
            comparison.ledger_rows_without_ticker += 1
            continue
        comparison.ledger_rows_compared += 1
        ledger_by_key.setdefault(key, []).append(row)

    router_by_key: dict[tuple[str, str], list[Any]] = {}
    for wager in wagers:
        comparison.router_wagers_built += 1
        key = _key(getattr(wager, "market_ticker", None), getattr(wager, "side", None))
        if key is None:
            # A wager with no ticker forms no key, so it belongs in none of
            # the key buckets. Counted on its own line instead of being folded
            # into "router only", which would be a claim about a key there
            # isn't one of.
            comparison.router_wagers_without_ticker += 1
            continue
        router_by_key.setdefault(key, []).append(wager)

    for key in sorted(set(ledger_by_key) | set(router_by_key)):
        ledger_hits = ledger_by_key.get(key, [])
        router_hits = router_by_key.get(key, [])
        if len(ledger_hits) > 1 or len(router_hits) > 1:
            comparison.ambiguous_multiplicity_keys += 1
            continue
        if not ledger_hits:
            comparison.router_only_keys += 1
            continue
        if not router_hits:
            comparison.ledger_only_keys += 1
            continue

        comparison.matched_keys += 1
        _score(comparison, router_hits[0], ledger_hits[0])

    return comparison


def _score(comparison: LedgerComparison, wager: Any, row: dict) -> None:
    _score_money(
        comparison,
        _decimal(getattr(wager, "stake", None)),
        _decimal(row.get("stake")),
        STAKE_TOLERANCE,
        "stake",
    )
    _score_money(
        comparison,
        _decimal(getattr(wager, "entry_price", None)),
        _decimal(row.get("entryPrice")),
        PRICE_TOLERANCE,
        "price",
    )

    router_result = getattr(wager, "result", None)
    ledger_result = row.get("result")
    if not isinstance(router_result, str) or not isinstance(ledger_result, str):
        comparison.result_not_comparable += 1
    elif router_result.strip().upper() == ledger_result.strip().upper():
        comparison.result_agrees += 1
    else:
        comparison.result_disagrees += 1


def _score_money(
    comparison: LedgerComparison,
    router_value: Decimal | None,
    ledger_value: Decimal | None,
    tolerance: Decimal,
    field: str,
) -> None:
    if router_value is None or ledger_value is None:
        setattr(comparison, f"{field}_not_comparable", getattr(comparison, f"{field}_not_comparable") + 1)
    elif abs(router_value - ledger_value) <= tolerance:
        setattr(comparison, f"{field}_agrees", getattr(comparison, f"{field}_agrees") + 1)
    else:
        setattr(comparison, f"{field}_disagrees", getattr(comparison, f"{field}_disagrees") + 1)
