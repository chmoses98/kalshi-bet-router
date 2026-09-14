"""The live schema probe: observes shape without admitting guessed values."""

from __future__ import annotations

import pytest

from kalshi_router.schema_probe import SchemaCoverage, probe_fills

from .test_accounting_schema import official_fill


def probe(*raws):
    return probe_fills(list(raws))


def test_a_clean_official_fill_normalizes_and_is_counted():
    accepted, coverage = probe(official_fill())
    assert len(accepted) == 1
    assert coverage.fills_seen == 1
    assert coverage.fills_normalized == 1
    assert coverage.fills_rejected == 0


def test_direction_field_coverage_is_recorded():
    canonical = official_fill()
    del canonical["action"], canonical["side"]
    legacy = official_fill(fill_id="F2")
    del legacy["outcome_side"], legacy["book_side"]

    _, coverage = probe(canonical, legacy)
    assert coverage.with_outcome_side == 1
    assert coverage.with_book_side == 1
    assert coverage.with_deprecated_action_side == 1
    assert coverage.canonical_only_fills == 1
    assert coverage.legacy_only_fills == 1


def test_price_field_agreement_is_recorded():
    """Raw equality of the two fields, kept as a plain shape observation.

    Equality is no longer what acceptance turns on -- complementarity is -- so
    a complementary pair away from even odds is counted as DISAGREEING here and
    still normalizes.
    """
    at_even_odds = official_fill(yes_price_dollars="0.5000",
                                 no_price_dollars="0.5000")
    complementary = official_fill(fill_id="F2", yes_price_dollars="0.5600",
                                  no_price_dollars="0.4400")
    accepted, coverage = probe(at_even_odds, complementary)
    assert coverage.both_price_fields_present == 2
    assert coverage.price_fields_agreed == 1
    assert coverage.price_fields_disagreed == 1
    assert len(accepted) == 2
    assert coverage.rejected_price == 0


def test_a_non_complementary_pair_is_rejected():
    accepted, coverage = probe(official_fill(yes_price_dollars="0.6000",
                                             no_price_dollars="0.3000"))
    assert accepted == []
    assert coverage.rejected_price == 1


def test_fee_field_coverage_is_recorded():
    as_string = official_fill(fee_cost="0.5600")
    as_integer = official_fill(fill_id="F2", fee_cost=56)
    without = official_fill(fill_id="F3")
    del without["fee_cost"]

    _, coverage = probe(as_string, as_integer, without)
    assert coverage.with_fee_cost_string == 1
    assert coverage.with_fee_cost_integer == 1
    assert coverage.without_any_fee_field == 1


def test_subaccount_coverage_separates_absent_from_malformed():
    present = official_fill(subaccount_number=3)
    absent = official_fill(fill_id="F2")
    del absent["subaccount_number"]
    malformed = official_fill(fill_id="F3", subaccount_number=999)

    accepted, coverage = probe(present, absent, malformed)
    assert coverage.subaccount_present == 1
    assert coverage.subaccount_absent == 1
    assert coverage.subaccount_malformed == 1
    assert coverage.rejected_subaccount == 1
    assert len(accepted) == 2


def test_timestamp_coverage_is_recorded():
    dated = official_fill()
    ts_only = official_fill(fill_id="F2")
    del ts_only["created_time"]
    undated = official_fill(fill_id="F3")
    del undated["created_time"], undated["ts"]

    _, coverage = probe(dated, ts_only, undated)
    assert coverage.with_created_time == 1
    assert coverage.with_ts_only == 1
    assert coverage.without_any_timestamp == 1


def test_a_canonical_vs_legacy_conflict_is_rejected_and_counted():
    conflicting = official_fill(outcome_side="yes", book_side="bid",
                                action="buy", side="no")
    accepted, coverage = probe(conflicting)
    assert accepted == []
    assert coverage.fills_rejected == 1
    assert coverage.direction_conflicts == 1


def test_a_fill_with_no_direction_evidence_is_rejected():
    raw = official_fill()
    for key in ("outcome_side", "book_side", "action", "side"):
        raw.pop(key, None)
    accepted, coverage = probe(raw)
    assert accepted == []
    assert coverage.rejected_direction == 1


def test_a_fill_missing_identity_is_rejected():
    raw = official_fill()
    del raw["fill_id"]
    accepted, coverage = probe(raw)
    assert accepted == []
    assert coverage.rejected_identity == 1


def test_non_object_entries_are_rejected_without_crashing():
    accepted, coverage = probe_fills(["not-a-fill", official_fill()])
    assert len(accepted) == 1
    assert coverage.rejected_other == 1


def test_legacy_price_unproven_fills_are_counted():
    """A lone legacy integer price with no canonical fields is uncheckable."""
    raw = official_fill(yes_price=56)
    del raw["outcome_side"], raw["book_side"]
    del raw["yes_price_dollars"], raw["no_price_dollars"]
    accepted, coverage = probe(raw)
    assert len(accepted) == 1
    assert coverage.legacy_price_unproven_fills == 1


def test_rejected_fills_never_reach_the_accepted_list():
    """The fail-closed guarantee: excluded, not admitted with guessed values."""
    accepted, coverage = probe(
        official_fill(fill_id="GOOD"),
        official_fill(fill_id="BAD", count_fp="0.00"),
    )
    assert [f.fill_id for f in accepted] == ["GOOD"]
    assert coverage.rejected_quantity == 1


# ------------------------------------------------------------------ privacy

def test_coverage_can_only_hold_counts():
    """Every leaf is an int, and matrix keys are bounded enum labels.

    The direction matrix is the only non-scalar field, so it carries the whole
    risk of a value reaching a public log.  Pinning its key alphabet here is
    what keeps "counts only" true of the nested field as well.
    """
    _, coverage = probe(official_fill())
    allowed_components = {"yes", "no", "bid", "ask", "buy", "sell", "?", "-"}
    for name, value in vars(coverage).items():
        if name == "direction_matrix":
            assert isinstance(value, dict)
            for key, count in value.items():
                assert isinstance(count, int), f"{name}[{key}] is not a count"
                components = key.split("|")
                assert len(components) == 4, f"unexpected matrix key shape: {key}"
                assert set(components) <= allowed_components, (
                    f"matrix key {key} carries a non-label component"
                )
            continue
        assert isinstance(value, int), f"{name} is not a count"


def test_flattened_coverage_is_entirely_scalar():
    _, coverage = probe(official_fill())
    for name, value in coverage.as_dict().items():
        assert isinstance(value, int), f"{name} is not a count"


def test_rendered_coverage_leaks_no_identifier_or_value():
    _, coverage = probe(official_fill())
    rendered = coverage.render()
    for token in ("SYNTHFILL", "SYNTHORDER", "KXSYNTH", "0.5600", "$"):
        assert token not in rendered
    assert "fills seen: 1" in rendered


def test_empty_input_renders_safely():
    accepted, coverage = probe_fills([])
    assert accepted == []
    assert "fills seen: 0" in coverage.render()


def test_an_undated_fill_is_rejected_so_replay_stays_orderable():
    """One undated fill must not take down the whole accounting pass."""
    undated = official_fill(fill_id="UNDATED")
    del undated["created_time"], undated["ts"]
    accepted, coverage = probe(official_fill(fill_id="GOOD"), undated)
    assert [f.fill_id for f in accepted] == ["GOOD"]
    assert coverage.rejected_timestamp == 1
    assert coverage.without_any_timestamp == 1


def test_price_model_counters_separate_complementary_from_unified():
    """The counters must tell the two candidate price models apart.

    This is the instrument the price-semantics decision rests on, so each
    signature is exercised explicitly rather than inferred from the live run.
    """
    complementary = official_fill(yes_price_dollars="0.5600",
                                  no_price_dollars="0.4400")
    even_odds = official_fill(fill_id="F2", yes_price_dollars="0.5000",
                              no_price_dollars="0.5000")
    unified_only = official_fill(fill_id="F3", yes_price_dollars="0.6000",
                                 no_price_dollars="0.6000")
    neither = official_fill(fill_id="F4", yes_price_dollars="0.6000",
                            no_price_dollars="0.3000")

    _, coverage = probe(complementary, even_odds, unified_only, neither)
    assert coverage.price_pairs_complementary == 2  # 0.56/0.44 and 0.50/0.50
    assert coverage.price_pairs_equal_at_half == 1
    assert coverage.price_pairs_equal_off_half == 1
    assert coverage.price_pairs_unexplained == 1


def test_even_odds_pair_is_counted_under_both_models():
    """An equal-at-even-odds pair proves nothing on its own; it must not be
    read as evidence for the unified model alone."""
    _, coverage = probe(official_fill(yes_price_dollars="0.5000",
                                      no_price_dollars="0.5000"))
    assert coverage.price_pairs_complementary == 1
    assert coverage.price_pairs_equal_at_half == 1
    assert coverage.price_pairs_equal_off_half == 0


def test_direction_matrix_counts_vocabulary_co_occurrence():
    a = official_fill(outcome_side="yes", book_side="bid", action="buy", side="yes")
    b = official_fill(fill_id="F2", outcome_side="yes", book_side="bid",
                      action="buy", side="yes")
    c = official_fill(fill_id="F3", outcome_side="no", book_side="ask",
                      action="sell", side="yes")

    _, coverage = probe(a, b, c)
    assert coverage.direction_matrix["yes|bid|buy|yes"] == 2
    assert coverage.direction_matrix["no|ask|sell|yes"] == 1


def test_direction_matrix_never_echoes_an_unexpected_value():
    """An unrecognised label is bucketed, so a hostile or novel payload cannot
    print its own text into a public log."""
    weird = official_fill(outcome_side="MAYBE", book_side="", action="buy",
                          side="yes")
    _, coverage = probe(weird)
    assert "?|-|buy|yes" in coverage.direction_matrix
    assert "MAYBE" not in coverage.render()


def test_rejected_fills_still_contribute_price_model_evidence():
    """Evidence must survive rejection -- otherwise a schema we got wrong would
    hide the very data proving it wrong."""
    unexplained = official_fill(yes_price_dollars="0.6000",
                                no_price_dollars="0.3000")
    accepted, coverage = probe(unexplained)
    assert accepted == []
    assert coverage.rejected_price == 1
    assert coverage.price_pairs_unexplained == 1
