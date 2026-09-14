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
    agreeing = official_fill()
    disagreeing = official_fill(fill_id="F2", yes_price_dollars="0.5600",
                                no_price_dollars="0.4400")
    accepted, coverage = probe(agreeing, disagreeing)
    assert coverage.both_price_fields_present == 2
    assert coverage.price_fields_agreed == 1
    assert coverage.price_fields_disagreed == 1
    # The disagreeing one is rejected, not silently resolved.
    assert len(accepted) == 1
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
    raw = official_fill()
    del raw["outcome_side"], raw["book_side"]
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
    _, coverage = probe(official_fill())
    for name, value in vars(coverage).items():
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
