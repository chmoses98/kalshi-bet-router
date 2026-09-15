"""The one-time historical catch-up, and the rules that keep it one-time.

The danger here is not failing to import a wager. It is importing one the owner
already recorded, which would corrupt their betting history and their bankroll,
and it is doing that silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from kalshi_router.backfill import (
    BACKFILL_IMPORT_BATCH_ID,
    BackfillDiagnostics,
    BackfillVerdict,
    BackfillWindow,
    reconcile,
    reconcile_one,
)
from kalshi_router.production import PRODUCTION_CUTOVER_ISO, production_cutover_seconds

SUPPORTED = frozenset({"MLB"})
TICKER = "KXMLBF5-26SEP12NYYBOS-NYY"


@dataclass
class FakeWager:
    market_ticker: str = TICKER
    side: str = "YES"
    sport: str = "MLB"
    stake: Decimal = Decimal("25.00")
    vwap_price: Decimal = Decimal("0.53")
    source_key: str = "kalshi:v1:abc123"


def ledger_row(**overrides):
    row = {
        "marketTicker": TICKER,
        "side": "YES",
        "stake": 25.00,
        "entryPrice": 0.53,
        "sourceBetKey": "chatgpt-2026-09-12-001-nyy-f5",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# The window cannot reach into production's range.
# ---------------------------------------------------------------------------


def test_the_window_end_is_the_cutover_and_is_not_a_parameter():
    window = BackfillWindow("2026-09-11T00:00:00Z")

    assert window.end_iso == PRODUCTION_CUTOVER_ISO
    assert window.end_seconds == production_cutover_seconds()


def test_a_window_that_does_not_precede_the_cutover_is_refused():
    with pytest.raises(ValueError, match="precede"):
        BackfillWindow(PRODUCTION_CUTOVER_ISO)
    with pytest.raises(ValueError, match="precede"):
        BackfillWindow("2026-09-20T00:00:00Z")


def test_membership_is_decided_on_the_first_execution():
    """The same instant the production filter keys on. A late fill on an early
    order must not drag the wager into the window."""
    window = BackfillWindow("2026-09-11T00:00:00Z")
    start = window.start_seconds

    @dataclass
    class O:
        first_execution_time: Decimal
        last_execution_time: Decimal

    assert window.contains(O(start + 100, start + 100))
    assert not window.contains(O(start - 1, start + 100))
    # The cutover instant itself belongs to production, not to the backfill.
    assert not window.contains(O(window.end_seconds, window.end_seconds))


def test_nothing_in_this_module_moves_the_cutover():
    import kalshi_router.backfill as backfill

    BackfillWindow("2026-09-11T00:00:00Z")
    reconcile([], {}, SUPPORTED)

    assert backfill.PRODUCTION_CUTOVER_ISO == "2026-09-15T00:00:00Z"
    from kalshi_router import production

    assert production.PRODUCTION_CUTOVER_ISO == "2026-09-15T00:00:00Z"


# ---------------------------------------------------------------------------
# The six verdicts. Only one of them writes.
# ---------------------------------------------------------------------------


def test_a_wager_the_ledger_does_not_have_is_missing_importable():
    assert reconcile_one(FakeWager(), [], SUPPORTED) is BackfillVerdict.MISSING_IMPORTABLE


def test_a_wager_the_owner_already_recorded_by_hand_is_exact_existing():
    """The key is a ChatGPT slug and will never equal the router's digest.

    Matching on the key alone would report this as missing and duplicate the
    owner's row -- the single most damaging thing this module could do.
    """
    verdict = reconcile_one(FakeWager(), [ledger_row()], SUPPORTED)

    assert verdict is BackfillVerdict.EXACT_EXISTING


def test_a_row_carrying_the_routers_own_key_is_a_duplicate_noop():
    """What makes a second run provably a no-op rather than a second import."""
    row = ledger_row(sourceBetKey="kalshi:v1:abc123")

    assert reconcile_one(FakeWager(), [row], SUPPORTED) is BackfillVerdict.DUPLICATE_NOOP


def test_duplicate_noop_is_reported_even_when_the_economics_moved():
    """A row this backfill wrote is already recorded whatever it now says.
    Re-importing it is the thing to avoid; disagreeing with it is a separate
    problem and not one a second import would fix."""
    row = ledger_row(sourceBetKey="kalshi:v1:abc123", stake=999.0)

    assert reconcile_one(FakeWager(), [row], SUPPORTED) is BackfillVerdict.DUPLICATE_NOOP


def test_material_disagreement_is_a_conflict_never_an_overwrite():
    verdict = reconcile_one(FakeWager(), [ledger_row(stake=99.00)], SUPPORTED)

    assert verdict is BackfillVerdict.CONFLICT


def test_a_price_within_tolerance_is_not_a_conflict():
    """Both records round a volume-weighted average independently."""
    verdict = reconcile_one(FakeWager(), [ledger_row(entryPrice=0.5305)], SUPPORTED)

    assert verdict is BackfillVerdict.EXACT_EXISTING


def test_a_field_the_existing_row_lacks_is_not_a_disagreement():
    """Many hand-entered rows carry no contract count and some no price.
    Treating absence as conflict would turn most of the ledger into refusals."""
    verdict = reconcile_one(FakeWager(), [ledger_row(entryPrice=None, stake=None)], SUPPORTED)

    assert verdict is BackfillVerdict.EXACT_EXISTING


def test_two_recorded_wagers_on_one_market_and_side_are_ambiguous():
    """Picking one to compare against would decide, on no evidence, which of
    the owner's rows this execution is."""
    verdict = reconcile_one(FakeWager(), [ledger_row(), ledger_row()], SUPPORTED)

    assert verdict is BackfillVerdict.AMBIGUOUS


def test_a_sport_with_no_destination_is_reported_not_dropped():
    verdict = reconcile_one(FakeWager(sport="NFL"), [], SUPPORTED)

    assert verdict is BackfillVerdict.UNSUPPORTED_DESTINATION


def test_side_is_part_of_the_match_so_the_opposite_leg_is_not_the_same_wager():
    verdict = reconcile_one(FakeWager(side="YES"), [ledger_row(side="NO")], SUPPORTED)

    assert verdict is BackfillVerdict.MISSING_IMPORTABLE


# ---------------------------------------------------------------------------
# Only MISSING_IMPORTABLE may create a row.
# ---------------------------------------------------------------------------


def test_reconcile_returns_only_the_importable_wagers():
    """A caller must not have to filter correctly in order to stay safe."""
    wagers = [
        FakeWager(market_ticker="A", source_key="k1"),                      # missing
        FakeWager(market_ticker="B", source_key="k2"),                      # existing
        FakeWager(market_ticker="C", source_key="k3"),                      # duplicate
        FakeWager(market_ticker="D", source_key="k4"),                      # conflict
        FakeWager(market_ticker="E", source_key="k5", sport="TENNIS"),      # unsupported
    ]
    rows = {
        "MLB": [
            ledger_row(marketTicker="B"),
            ledger_row(marketTicker="C", sourceBetKey="k3"),
            ledger_row(marketTicker="D", stake=999.0),
        ],
    }

    importable, diagnostics = reconcile(wagers, rows, SUPPORTED)

    assert [w.market_ticker for w in importable] == ["A"]
    assert diagnostics.missing_importable == 1
    assert diagnostics.exact_existing == 1
    assert diagnostics.duplicate_noop == 1
    assert diagnostics.conflict == 1
    assert diagnostics.unsupported_destination == 1


def test_every_reconciled_wager_lands_in_exactly_one_verdict():
    wagers = [FakeWager(market_ticker=str(i), source_key=f"k{i}") for i in range(7)]

    _importable, diagnostics = reconcile(wagers, {"MLB": []}, SUPPORTED)

    assert diagnostics.reconciled == len(wagers)


def test_a_second_identical_run_creates_nothing():
    """The idempotency proof, in miniature: what the first run wrote comes back
    as DUPLICATE_NOOP, not as more rows."""
    wagers = [FakeWager(market_ticker="A", source_key="k1")]

    first, _ = reconcile(wagers, {"MLB": []}, SUPPORTED)
    assert len(first) == 1

    # The destination now holds what the first run wrote.
    written = [ledger_row(marketTicker="A", sourceBetKey="k1")]
    second, diagnostics = reconcile(wagers, {"MLB": written}, SUPPORTED)

    assert second == []
    assert diagnostics.duplicate_noop == 1
    assert diagnostics.missing_importable == 0


# ---------------------------------------------------------------------------
# Structure.
# ---------------------------------------------------------------------------


def test_every_verdict_has_a_counter():
    diagnostics = BackfillDiagnostics()
    from kalshi_router.backfill import _COUNTERS

    for verdict in BackfillVerdict:
        assert verdict in _COUNTERS
        assert hasattr(diagnostics, _COUNTERS[verdict])
    assert len(set(_COUNTERS.values())) == len(_COUNTERS)


def test_the_diagnostics_are_structurally_counts_only():
    for name, value in BackfillDiagnostics().as_dict().items():
        assert isinstance(value, int), f"{name} is {type(value).__name__}"


def test_the_batch_id_is_a_durable_constant():
    """Part of the destination's primary key. A value that moved between runs
    would re-import the whole batch as new every time."""
    assert BACKFILL_IMPORT_BATCH_ID == (
        "kalshi-gap-backfill-2026-09-12-through-production-cutover-v1"
    )
