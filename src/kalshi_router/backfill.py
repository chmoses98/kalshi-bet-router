"""One-time historical catch-up: the gap between the owner's last completed
accounting and the production cutover.

WHAT MAKES THIS DIFFERENT FROM PRODUCTION ROUTING
-------------------------------------------------
Production is future-only by construction: `evaluate_order` refuses anything at
or before ``PRODUCTION_CUTOVER_ISO``. This module deliberately admits
pre-cutover orders -- but only inside an explicit, bounded window, and only
when a caller asks for it by name.

Three properties keep that from becoming "the cutover moved":

1. the window's END is the cutover itself, structurally. It is not a parameter
   a caller can widen, so a backfill can never reach into production's range;
2. the window's START is explicit and must precede the end;
3. nothing here changes ``PRODUCTION_CUTOVER_ISO``, and a test asserts the
   constant is untouched by every code path in this module.

WHY DEDUPE CANNOT USE THE SOURCE KEY
------------------------------------
The destination ledger's 331 IMPORTED_RECEIPT rows were entered by hand from
postmortems, with human-authored keys like
``chatgpt-2026-08-03-001-alvarez-under-15-outs`` and
``2026-09-04|AZ-HOU|HOU_TEAM_TOTAL|OVER_3.5|35.00|48``.

The router's key is ``kalshi:v1:<sha256 of the order>``. It will NEVER equal
one of those, so key equality would report every already-recorded wager as
missing and duplicate the owner's betting history.

So the match is ECONOMIC -- ``(marketTicker, side)`` -- and the source key is
used only to recognise rows THIS backfill previously wrote. That is what makes
the two "already there" verdicts mean different things:

  EXACT_EXISTING   the owner already recorded it, by some other means
  DUPLICATE_NOOP   this backfill already wrote it

and it is what makes the idempotency proof meaningful: a first run turns
MISSING_IMPORTABLE into rows, and a second run must turn those same wagers into
DUPLICATE_NOOP rather than into more rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from .ledger_compare import PRICE_TOLERANCE, STAKE_TOLERANCE, _decimal, _key
from .production import PRODUCTION_CUTOVER_ISO, production_cutover_seconds
from .timeaxis import parse_rfc3339_seconds

#: The durable batch label for this one-time catch-up.
#:
#: Versioned and literal, for the same reason ROUTER_IMPORT_BATCH_ID is: the
#: destination derives a row's identity partly from it, so a value that moved
#: between runs would re-import the entire batch as new every time.
BACKFILL_IMPORT_BATCH_ID = (
    "kalshi-gap-backfill-2026-09-12-through-production-cutover-v1"
)


#: HOW TO READ A DESTINATION'S EXISTING ROWS.
#:
#: Reconciliation compares a reconstructed wager against the rows a destination
#: already holds, and those rows are written in the DESTINATION's vocabulary --
#: the same split ``production.ROW_BUILDERS`` exists for, seen from the other
#: side. MLB writes ``marketTicker`` and ``entryPrice``; NFL writes
#: ``market_ticker`` and ``actual_price``; CFB writes ``market_ticker`` and
#: ``execution_price``.
#:
#: READING ONE LEDGER IN ANOTHER'S WORDS FAILS SILENTLY AND EXPENSIVELY. Every
#: lookup would simply return None: the router's own source key would never be
#: recognised, no economic match would ever be found, and every wager this
#: backfill had ALREADY written would come back MISSING_IMPORTABLE. The second
#: run -- the one whose whole purpose is to prove the first was idempotent --
#: would duplicate the entire batch and report success doing it.
#:
#: So a sport absent from this map is REFUSED. Not read with some other sport's
#: words, and not assumed to be MLB because MLB was first.
LEDGER_VOCABULARIES: dict[str, dict[str, str]] = {
    "MLB": {
        "source_bet_key": "sourceBetKey",
        "market_ticker": "marketTicker",
        "side": "side",
        "stake": "stake",
        "entry_price": "entryPrice",
    },
    "NFL": {
        "source_bet_key": "source_bet_key",
        "market_ticker": "market_ticker",
        "side": "side",
        "stake": "stake",
        "entry_price": "actual_price",
    },
    "CFB": {
        "source_bet_key": "source_bet_key",
        "market_ticker": "market_ticker",
        "side": "side",
        "stake": "stake",
        "entry_price": "execution_price",
    },
}

#: The keys every reconciliation reads, whatever the destination calls them.
CANONICAL_LEDGER_FIELDS = ("source_bet_key", "market_ticker", "side", "stake", "entry_price")


class UnreadableLedger(Exception):
    """This sport's ledger cannot be read, so its verdicts would be fiction."""


def normalize_ledger_row(sport: str, row: dict) -> dict:
    """One destination row, restated in the words reconciliation uses.

    Raises rather than guessing. A wrong verdict here does not look wrong: it
    looks like a wager that needs writing, and writing it is exactly what must
    not happen.
    """
    vocabulary = LEDGER_VOCABULARIES.get(sport)
    if vocabulary is None:
        raise UnreadableLedger(
            f"no ledger vocabulary is known for {sport!r}; its rows cannot be "
            "read, and reading them in another sport's words would report every "
            "already-recorded wager as missing"
        )
    return {canonical: row.get(vocabulary[canonical]) for canonical in CANONICAL_LEDGER_FIELDS}


class BackfillVerdict(str, Enum):
    """What reconciliation decided about one reconstructed wager.

    Only MISSING_IMPORTABLE may create a canonical row. Every other verdict is
    a refusal to write, and each names a different reason so that a run's
    output can be acted on rather than merely counted.
    """

    #: The owner already recorded this execution, by some other means, and the
    #: economics agree. Nothing to do.
    EXACT_EXISTING = "exact_existing"
    #: THIS backfill already wrote it -- matched on the router's own source
    #: key. The verdict that makes a second run provably a no-op.
    DUPLICATE_NOOP = "duplicate_noop"
    #: Genuinely absent from the destination. The only verdict that writes.
    MISSING_IMPORTABLE = "missing_importable"
    #: More than one candidate on either side. Never resolved by picking.
    AMBIGUOUS = "ambiguous"
    #: Present, but the economics disagree materially. Never overwritten.
    CONFLICT = "conflict"
    #: The sport has no accounting destination that can hold a wager.
    UNSUPPORTED_DESTINATION = "unsupported_destination"


@dataclass(frozen=True)
class BackfillWindow:
    """A bounded pre-cutover range. The end is the cutover, structurally."""

    start_iso: str

    @property
    def end_iso(self) -> str:
        return PRODUCTION_CUTOVER_ISO

    @property
    def start_seconds(self) -> Decimal:
        parsed = parse_rfc3339_seconds(self.start_iso)
        if parsed is None:
            raise ValueError(f"backfill window start is not a readable timestamp: {self.start_iso!r}")
        return parsed

    @property
    def end_seconds(self) -> Decimal:
        return production_cutover_seconds()

    def __post_init__(self) -> None:
        if self.start_seconds >= self.end_seconds:
            raise ValueError(
                "a backfill window must start before the production cutover; "
                f"{self.start_iso!r} does not precede {self.end_iso!r}"
            )

    def contains(self, order) -> bool:
        """Membership is decided on the order's FIRST execution.

        The same instant the production filter keys on, for the same reason: a
        late fill on an early order does not move the wager into the window.
        """
        return self.start_seconds <= order.first_execution_time < self.end_seconds


@dataclass
class BackfillDiagnostics:
    """Counts only. Every field is an int; a test walks them."""

    orders_in_window: int = 0

    exact_existing: int = 0
    duplicate_noop: int = 0
    missing_importable: int = 0
    ambiguous: int = 0
    conflict: int = 0
    unsupported_destination: int = 0

    #: Wagers that never reached reconciliation because a gate refused them
    #: first. Reported separately so "not imported" and "not reconstructable"
    #: are never the same number.
    refused_before_reconciliation: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(vars(self))

    @property
    def contradicts_itself(self) -> bool:
        """More wagers reconciled than there were orders to reconstruct them
        from. Structurally impossible, so seeing it means a count is wired
        wrong rather than that something surprising happened."""
        return self.reconciled > self.orders_in_window

    @property
    def reconciled(self) -> int:
        return (
            self.exact_existing
            + self.duplicate_noop
            + self.missing_importable
            + self.ambiguous
            + self.conflict
            + self.unsupported_destination
        )

    def render(self) -> str:
        return "\n".join([
            "backfill reconciliation (counts only; only MISSING_IMPORTABLE writes):",
            f"  orders in the window: {self.orders_in_window}"
            + ("   <- INCONSISTENT with the count below" if self.contradicts_itself else ""),
            f"  refused before reconciliation: {self.refused_before_reconciliation}",
            f"  reconciled: {self.reconciled}",
            f"    EXACT_EXISTING (owner already recorded it): {self.exact_existing}",
            f"    DUPLICATE_NOOP (this backfill already wrote it): {self.duplicate_noop}",
            f"    MISSING_IMPORTABLE (will be written): {self.missing_importable}",
            f"    AMBIGUOUS (refused): {self.ambiguous}",
            f"    CONFLICT (refused, never overwritten): {self.conflict}",
            f"    UNSUPPORTED_DESTINATION: {self.unsupported_destination}",
        ])


_COUNTERS = {
    BackfillVerdict.EXACT_EXISTING: "exact_existing",
    BackfillVerdict.DUPLICATE_NOOP: "duplicate_noop",
    BackfillVerdict.MISSING_IMPORTABLE: "missing_importable",
    BackfillVerdict.AMBIGUOUS: "ambiguous",
    BackfillVerdict.CONFLICT: "conflict",
    BackfillVerdict.UNSUPPORTED_DESTINATION: "unsupported_destination",
}


def reconcile_one(wager, ledger_rows, supported_sports) -> BackfillVerdict:
    """Decide one reconstructed wager against the destination's existing rows.

    ``ledger_rows`` are the destination's rows for this wager's sport.
    """
    if wager.sport not in supported_sports:
        return BackfillVerdict.UNSUPPORTED_DESTINATION

    key = _key(wager.market_ticker, wager.side)
    if key is None:
        return BackfillVerdict.AMBIGUOUS

    # Read in the DESTINATION's own vocabulary before anything is compared.
    rows = [normalize_ledger_row(wager.sport, row) for row in ledger_rows]

    # The router's own key first: a row carrying it was written by this
    # backfill, which is a different fact from the owner having recorded the
    # same wager by hand.
    if any(row.get("source_bet_key") == wager.source_key for row in rows):
        return BackfillVerdict.DUPLICATE_NOOP

    matches = [row for row in rows if _key(row.get("market_ticker"), row.get("side")) == key]
    if not matches:
        return BackfillVerdict.MISSING_IMPORTABLE
    if len(matches) > 1:
        # Two recorded wagers on one market and side. Picking one to compare
        # against would decide, on no evidence, which of the owner's rows this
        # execution is.
        return BackfillVerdict.AMBIGUOUS

    existing = matches[0]
    if _materially_disagrees(wager, existing):
        return BackfillVerdict.CONFLICT
    return BackfillVerdict.EXACT_EXISTING


def _materially_disagrees(wager, row) -> bool:
    """Stake or entry price differing by more than the recording tolerance.

    ``row`` is already NORMALIZED -- canonical keys, not the destination's own.

    A field the existing row does not carry is NOT a disagreement: many hand-
    entered rows have no contract count at all, and treating absence as
    conflict would turn most of the ledger into refusals.
    """
    for wager_value, row_value, tolerance in (
        (_decimal(getattr(wager, "stake", None)), _decimal(row.get("stake")), STAKE_TOLERANCE),
        (_decimal(getattr(wager, "vwap_price", None)), _decimal(row.get("entry_price")), PRICE_TOLERANCE),
    ):
        if wager_value is None or row_value is None:
            continue
        if abs(wager_value - row_value) > tolerance:
            return True
    return False


def reconcile(wagers, ledger_rows_by_sport, supported_sports, orders_in_window=0):
    """Reconcile every reconstructed wager. Returns (importable, diagnostics).

    ``importable`` holds ONLY the MISSING_IMPORTABLE wagers, because that is
    the only verdict permitted to create a canonical row and a caller should
    not have to filter correctly to stay safe.

    ``orders_in_window`` is passed in because this function cannot know it --
    it sees the wagers that survived the gates, not the orders they came from.
    Defaulting the field to 0 and rendering it anyway printed "orders in the
    window: 0" directly above "reconciled: 42", which is not a small
    cosmetic problem: a report that contradicts itself on its own first line
    is not evidence of anything.
    """
    diagnostics = BackfillDiagnostics(orders_in_window=orders_in_window)
    importable = []
    for wager in wagers:
        rows = ledger_rows_by_sport.get(wager.sport, [])
        verdict = reconcile_one(wager, rows, supported_sports)
        counter = _COUNTERS[verdict]
        setattr(diagnostics, counter, getattr(diagnostics, counter) + 1)
        if verdict is BackfillVerdict.MISSING_IMPORTABLE:
            importable.append(wager)
    return importable, diagnostics
