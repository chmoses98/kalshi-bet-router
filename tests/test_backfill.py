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


# ---------------------------------------------------------------------------
# A report that contradicts itself is not evidence of anything.
# ---------------------------------------------------------------------------


def test_the_window_count_is_passed_in_not_defaulted():
    """reconcile() sees the wagers that survived the gates, not the orders they
    came from. It cannot know the window count, so it must be told."""
    _importable, diagnostics = reconcile(
        [FakeWager()], {}, frozenset(), orders_in_window=76
    )

    assert diagnostics.orders_in_window == 76


def test_reconciling_more_wagers_than_orders_is_flagged_as_inconsistent():
    """The defect this guards: 'orders in the window: 0' printed directly above
    'reconciled: 42'. Structurally impossible, so it means a count is wired
    wrong rather than that something surprising happened."""
    wagers = [FakeWager(market_ticker=str(i), source_key=f"k{i}") for i in range(42)]

    _importable, diagnostics = reconcile(wagers, {}, frozenset(), orders_in_window=0)

    assert diagnostics.reconciled == 42
    assert diagnostics.contradicts_itself
    assert "INCONSISTENT" in diagnostics.render()


def test_a_consistent_report_says_nothing_about_inconsistency():
    wagers = [FakeWager(market_ticker=str(i), source_key=f"k{i}") for i in range(3)]

    _importable, diagnostics = reconcile(wagers, {}, frozenset(), orders_in_window=76)

    assert not diagnostics.contradicts_itself
    assert "INCONSISTENT" not in diagnostics.render()


def test_equal_counts_are_consistent():
    """Every order becoming exactly one wager is the normal case, not a defect."""
    wagers = [FakeWager(market_ticker=str(i), source_key=f"k{i}") for i in range(5)]

    _importable, diagnostics = reconcile(wagers, {}, frozenset(), orders_in_window=5)

    assert not diagnostics.contradicts_itself


# ------------------------------------------------ reading each ledger's words

class TestLedgerVocabulary:
    """Every destination spells the same row differently, and reading one in
    another's words fails SILENTLY.

    Nothing raises. Every lookup simply returns None, so the router's own source
    key is never recognised, no economic match is ever found, and every wager
    this backfill already wrote comes back MISSING_IMPORTABLE. The second run --
    the one whose entire purpose is to prove the first was idempotent -- would
    duplicate the whole batch and report success doing it.
    """

    NFL_TICKER = "KXNFLGAME-26SEP14KCBUF-KC"
    CFB_TICKER = "KXNCAAFGAME-26SEP12ALAUGA-ALA"

    def nfl_row(self, **overrides):
        """Exactly what nfl_edge.handicap.imported_wagers holds."""
        row = {
            "source_bet_key": "kalshi:v1:abc123",
            "market_ticker": self.NFL_TICKER,
            "side": "YES",
            "stake": 24.68,
            "actual_price": 0.61,
        }
        row.update(overrides)
        return row

    def cfb_row(self, **overrides):
        """Exactly what cfb_edge_finder.accounting.wager holds."""
        row = {
            "source_bet_key": "kalshi:v1:abc123",
            "market_ticker": self.CFB_TICKER,
            "side": "YES",
            "stake": 11.94,
            "execution_price": 0.47,
        }
        row.update(overrides)
        return row

    def test_a_wager_this_backfill_already_wrote_to_nfl_is_a_no_op(self):
        """The whole idempotency proof, for the destination that does not speak
        MLB's language."""
        wager = FakeWager(market_ticker=self.NFL_TICKER, sport="NFL",
                          stake=Decimal("24.68"), vwap_price=Decimal("0.61"))

        verdict = reconcile_one(wager, [self.nfl_row()], frozenset({"NFL"}))

        assert verdict is BackfillVerdict.DUPLICATE_NOOP

    def test_a_wager_this_backfill_already_wrote_to_cfb_is_a_no_op(self):
        wager = FakeWager(market_ticker=self.CFB_TICKER, sport="CFB",
                          stake=Decimal("11.94"), vwap_price=Decimal("0.47"))

        verdict = reconcile_one(wager, [self.cfb_row()], frozenset({"CFB"}))

        assert verdict is BackfillVerdict.DUPLICATE_NOOP

    def test_reading_a_cfb_ledger_in_mlbs_words_would_report_it_missing(self):
        """The defect, demonstrated rather than described.

        This is what the code did before the vocabularies existed: the row is
        RIGHT THERE, the sport is supported, and the verdict is 'write it'.
        """
        from kalshi_router.backfill import normalize_ledger_row

        as_mlb = normalize_ledger_row("MLB", self.cfb_row())

        assert as_mlb["source_bet_key"] is None
        assert as_mlb["market_ticker"] is None
        assert as_mlb["entry_price"] is None

    def test_an_owner_recorded_nfl_row_is_matched_economically(self):
        """A row the owner entered by hand carries a key this router will never
        produce, so it must still be recognised on (ticker, side)."""
        wager = FakeWager(market_ticker=self.NFL_TICKER, sport="NFL",
                          stake=Decimal("24.68"), vwap_price=Decimal("0.61"))

        verdict = reconcile_one(
            wager, [self.nfl_row(source_bet_key="hand-entered-week-2-kc")],
            frozenset({"NFL"}),
        )

        assert verdict is BackfillVerdict.EXACT_EXISTING

    def test_a_disagreeing_cfb_price_is_a_conflict_not_an_overwrite(self):
        """Proves the price field is being read at all: with the wrong key it
        would be None, and a missing field is deliberately not a disagreement --
        so this test passing on `execution_price` is what shows the vocabulary
        reached the comparison."""
        wager = FakeWager(market_ticker=self.CFB_TICKER, sport="CFB",
                          stake=Decimal("11.94"), vwap_price=Decimal("0.47"))

        verdict = reconcile_one(
            wager,
            [self.cfb_row(source_bet_key="hand-entered", execution_price=0.80)],
            frozenset({"CFB"}),
        )

        assert verdict is BackfillVerdict.CONFLICT

    def test_a_sport_with_no_known_vocabulary_is_refused_not_guessed(self):
        """Refusing aborts the run. That is the point: a ledger read in the
        wrong words poisons EVERY verdict for that sport, so continuing past it
        produces a batch of confident writes that are all wrong."""
        from kalshi_router.backfill import UnreadableLedger

        wager = FakeWager(sport="TENNIS")

        with pytest.raises(UnreadableLedger, match="no ledger vocabulary"):
            reconcile_one(wager, [ledger_row()], frozenset({"TENNIS"}))

    def test_every_vocabulary_names_every_field_reconciliation_reads(self):
        """A vocabulary missing one key would raise KeyError at reconcile time,
        on a credentialed run, halfway through a backfill."""
        from kalshi_router.backfill import CANONICAL_LEDGER_FIELDS, LEDGER_VOCABULARIES

        for sport, vocabulary in LEDGER_VOCABULARIES.items():
            assert set(vocabulary) == set(CANONICAL_LEDGER_FIELDS), sport

    def test_every_destination_that_can_be_written_can_also_be_read(self):
        """The two halves of the same split.

        A sport this router can BUILD a row for but cannot READ the ledger of is
        the exact shape of the bug above: it would be written on the first run
        and written again on every run after.
        """
        from kalshi_router.backfill import LEDGER_VOCABULARIES
        from kalshi_router.production import ROW_BUILDERS

        assert set(ROW_BUILDERS) == set(LEDGER_VOCABULARIES)
