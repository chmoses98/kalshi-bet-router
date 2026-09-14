"""The reconciliation probe: measure the gap, never guess past it."""

from __future__ import annotations

from decimal import Decimal

from kalshi_router.reconcile import ReconciliationReport, probe_reconciliation


def pos(ticker, quantity, **extra):
    return {"ticker": ticker, "position_fp": quantity, **extra}


def probe(positions=(), settlements=(), replayed=None):
    return probe_reconciliation(list(positions), list(settlements), replayed or {})


def test_agreement_is_counted_per_market():
    report = probe(
        positions=[pos("A", "10.00"), pos("B", "-5.00")],
        replayed={"A": Decimal("10.00"), "B": Decimal("-5.00")},
    )
    assert report.markets_in_both == 2
    assert report.markets_agreeing_on_quantity == 2
    assert report.markets_disagreeing_on_quantity == 0


def test_a_settled_market_shows_as_replay_open_exchange_flat():
    """The signature this probe exists to measure.

    A settlement closes a position without producing a fill, so a fills-only
    replay still shows it open while the exchange reports flat.
    """
    report = probe(positions=[pos("A", "0.00")], replayed={"A": Decimal("10.00")})
    assert report.markets_disagreeing_on_quantity == 1
    assert report.markets_replay_open_exchange_flat == 1


def test_a_plain_disagreement_is_not_counted_as_a_settlement():
    """Different quantities are a different fault from open-versus-flat."""
    report = probe(positions=[pos("A", "7.00")], replayed={"A": Decimal("10.00")})
    assert report.markets_disagreeing_on_quantity == 1
    assert report.markets_replay_open_exchange_flat == 0


def test_markets_missing_from_each_side_are_counted_separately():
    report = probe(
        positions=[pos("A", "1.00"), pos("C", "3.00")],
        replayed={"A": Decimal("1.00"), "B": Decimal("2.00")},
    )
    assert report.markets_only_in_replay == 1      # B
    assert report.markets_only_in_positions == 1   # C
    assert report.markets_in_both == 1


def test_an_unparseable_quantity_is_not_treated_as_absent():
    """Present-but-broken and absent are different faults and must not merge."""
    report = probe(positions=[{"ticker": "A", "position_fp": "not a number"}])
    assert report.position_rows_unparseable_quantity == 1
    assert report.position_rows_with_quantity == 0


def test_a_row_with_no_quantity_field_is_neither_parsed_nor_flagged():
    report = probe(positions=[{"ticker": "A"}])
    assert report.position_rows == 1
    assert report.position_rows_with_quantity == 0
    assert report.position_rows_unparseable_quantity == 0


def test_zero_and_nonzero_positions_are_split():
    report = probe(positions=[pos("A", "0.00"), pos("B", "4.00"), pos("C", "-1.00")])
    assert report.position_rows_zero == 1
    assert report.position_rows_nonzero == 2


def test_legacy_position_field_is_also_read():
    report = probe(positions=[{"ticker": "A", "position": 10}],
                   replayed={"A": Decimal(10)})
    assert report.markets_agreeing_on_quantity == 1


def test_settlement_results_are_bucketed_not_echoed():
    report = probe(settlements=[
        {"ticker": "A", "result": "yes"},
        {"ticker": "B", "result": "scalar"},
        {"ticker": "C", "result": "SOMETHING NEW"},
    ])
    assert report.settlement_rows == 3
    assert report.settlement_rows_with_result == 3
    assert "yes" in report.settlement_results
    assert "scalar" in report.settlement_results
    assert "other" in report.settlement_results
    assert "SOMETHING NEW" not in report.render()


def test_a_settlement_without_a_result_is_counted_separately():
    report = probe(settlements=[{"ticker": "A"}])
    assert report.settlement_rows_without_result == 1


def test_schema_names_pass_the_allowlist_and_tickers_never_do():
    report = probe(
        positions=[{"ticker": "KXMLBGAME-26SEP01-NYY", "position_fp": "1.00",
                    "realized_pnl_dollars": "1.00", "NotALowercaseKey": 1}],
    )
    assert "ticker" in report.position_keys
    assert "realized_pnl_dollars" in report.position_keys
    assert "NotALowercaseKey" not in report.position_keys
    rendered = report.render()
    assert "KXMLBGAME" not in rendered
    assert "NYY" not in rendered


def test_render_carries_no_quantity_or_price_values():
    report = probe(
        positions=[pos("A", "12.00", realized_pnl_dollars="34.56",
                       fees_paid_dollars="0.78")],
        replayed={"A": Decimal("12.00")},
    )
    rendered = report.render()
    for value in ("12.00", "34.56", "0.78"):
        assert value not in rendered


def test_the_report_says_it_is_a_measurement_not_a_verdict():
    assert "not a reconciliation verdict" in probe().render()


def test_an_empty_account_probes_cleanly():
    report = probe()
    assert report.position_rows == 0
    assert report.settlement_rows == 0
    assert report.markets_in_both == 0


def test_non_object_rows_are_ignored_rather_than_crashing():
    report = probe_reconciliation(["nope", 3, None], [()], {})
    assert report.position_rows == 0
    assert report.settlement_rows == 0


def test_counts_are_all_integers_or_schema_name_tuples():
    report = probe(positions=[pos("A", "1.00")], replayed={"A": Decimal(1)})
    for name, value in vars(report).items():
        if name.endswith(("_keys", "_results")):
            assert isinstance(value, tuple)
            assert all(isinstance(v, str) for v in value)
            continue
        if name == "settlement_field_coverage":
            assert isinstance(value, dict)
            assert all(isinstance(v, int) for v in value.values())
            continue
        assert isinstance(value, int), f"{name} is not a count"


def test_the_flattened_report_is_entirely_scalar_or_name_tuples():
    report = probe(settlements=[{"ticker": "A", "value": "1.00", "revenue": "2.00"}])
    for name, value in report.as_dict().items():
        assert isinstance(value, (int, tuple)), f"{name} is neither a count nor names"


# ============ corrections forced by the first live reconciliation run ========
#
#   position rows: 0        settlement rows: 755 (all "without a result")
#   markets only in the replay: 155
#   settlement keys observed: ..., market_result, ..., value, yes_count_fp, ...
#
# Two assumptions in this module were wrong, and the measurement is what caught
# them.

def test_the_result_field_is_market_result():
    """755 of 755 rows read as "no result" purely because of a field guess."""
    report = probe(settlements=[{"ticker": "A", "market_result": "yes"}])
    assert report.settlement_rows_with_result == 1
    assert "yes" in report.settlement_results


def test_the_legacy_result_spelling_is_still_accepted():
    report = probe(settlements=[{"ticker": "A", "result": "no"}])
    assert report.settlement_rows_with_result == 1


def test_a_settled_market_is_absent_from_positions_not_flat():
    """The live account reported 0 position rows against 155 replayed markets.

    A settled market leaves the positions response; it does not appear with a
    zero quantity. So absence explained by a settlement is expected, not a gap.
    """
    report = probe(
        positions=[],
        settlements=[{"ticker": "A", "market_result": "yes"}],
        replayed={"A": Decimal("10.00")},
    )
    assert report.markets_only_in_replay == 1
    assert report.markets_absent_but_settled == 1
    assert report.markets_absent_and_unexplained == 0
    assert report.markets_replay_open_exchange_flat == 0


def test_an_absence_no_settlement_explains_is_the_one_that_means_missing_history():
    report = probe(positions=[], settlements=[], replayed={"A": Decimal("10.00")})
    assert report.markets_absent_but_settled == 0
    assert report.markets_absent_and_unexplained == 1


def test_settled_and_unexplained_absences_are_counted_separately():
    report = probe(
        positions=[],
        settlements=[{"ticker": "A", "market_result": "yes"}],
        replayed={"A": Decimal("1.00"), "B": Decimal("2.00")},
    )
    assert report.markets_absent_but_settled == 1
    assert report.markets_absent_and_unexplained == 1


def test_settlement_economics_coverage_is_reported_per_field():
    """A settlement carries its own quantities and cost, so it is a complete
    accounting event rather than a bare notification."""
    report = probe(settlements=[{
        "ticker": "A", "market_result": "yes", "value": "1.00",
        "revenue": "10.00", "yes_count_fp": "10.00", "no_count_fp": "0.00",
        "yes_total_cost_dollars": "5.60", "no_total_cost_dollars": "0.00",
        "fee_cost": "0.07", "settled_time": "2026-09-01T12:00:00Z",
    }])
    coverage = report.settlement_field_coverage
    for name in ("value", "revenue", "yes_count_fp", "yes_total_cost_dollars",
                 "fee_cost", "settled_time"):
        assert coverage[name] == 1


def test_distinct_settled_markets_are_counted():
    report = probe(settlements=[
        {"ticker": "A"}, {"ticker": "A"}, {"ticker": "B"},
    ])
    assert report.settlement_rows == 3
    assert report.settlement_markets == 2


def test_settlement_economics_values_never_reach_the_output():
    report = probe(settlements=[{
        "ticker": "KXMLBGAME-Z", "market_result": "yes",
        "revenue": "1234.56", "yes_total_cost_dollars": "98.76",
    }])
    rendered = report.render()
    assert "1234.56" not in rendered
    assert "98.76" not in rendered
    assert "KXMLBGAME" not in rendered


# ================ settlement economics semantics (Phase C.4) =================
#
# Replaying a settlement needs the payout. Taking it from the wrong field or the
# wrong unit would corrupt realized P&L on every settled wager, so the meaning of
# `revenue` and `value` is measured rather than assumed. A binary contract pays
# $1 per winning contract, which makes "binary par" a testable prediction.

def settlement(**kw):
    return {"ticker": "A", **kw}


def test_revenue_matching_the_winning_leg_count_is_binary_par():
    report = probe(settlements=[settlement(
        market_result="yes", yes_count_fp="10.00", no_count_fp="0.00",
        revenue="10.00",
    )])
    assert report.settlements_revenue_at_binary_par == 1
    assert report.settlements_revenue_off_binary_par == 0


def test_a_no_result_pays_the_no_leg_count():
    report = probe(settlements=[settlement(
        market_result="no", yes_count_fp="0.00", no_count_fp="7.00",
        revenue="7.00",
    )])
    assert report.settlements_revenue_at_binary_par == 1


def test_revenue_in_cents_would_show_as_off_par_not_silently_accepted():
    """If revenue were integer cents, 10 contracts would report 1000, not 10."""
    report = probe(settlements=[settlement(
        market_result="yes", yes_count_fp="10.00", no_count_fp="0.00",
        revenue="1000.00",
    )])
    assert report.settlements_revenue_at_binary_par == 0
    assert report.settlements_revenue_off_binary_par == 1


def test_a_losing_settlement_pays_zero_and_is_counted_separately():
    report = probe(settlements=[settlement(
        market_result="no", yes_count_fp="10.00", no_count_fp="0.00", revenue="0.00",
    )])
    assert report.settlements_revenue_zero == 1
    assert report.settlements_revenue_off_binary_par == 0


def test_an_unparseable_revenue_is_flagged_not_ignored():
    report = probe(settlements=[settlement(market_result="yes", revenue="???")])
    assert report.settlements_revenue_unparseable == 1


def test_an_absent_revenue_is_not_flagged_as_unparseable():
    report = probe(settlements=[settlement(market_result="yes")])
    assert report.settlements_revenue_unparseable == 0


def test_a_scalar_settlement_value_is_detected():
    """Tennis verified 1,836 settlements strictly between 0 and 1.

    A binary-only router would be correct on every yes/no row and wrong on these,
    so the scalar case is counted on its own rather than folded into "other".
    """
    report = probe(settlements=[settlement(market_result="scalar", value="0.4200")])
    assert report.settlements_value_strictly_between == 1
    assert report.settlements_value_at_one == 0
    assert report.settlements_value_at_zero == 0


def test_binary_settlement_values_are_split_from_scalar_ones():
    report = probe(settlements=[
        settlement(market_result="yes", value="1.0000"),
        settlement(market_result="no", value="0.0000"),
        settlement(market_result="scalar", value="0.5000"),
    ])
    assert report.settlements_value_at_one == 1
    assert report.settlements_value_at_zero == 1
    assert report.settlements_value_strictly_between == 1


def test_a_value_above_one_is_counted_rather_than_assumed_impossible():
    """If `value` were a total payout rather than per contract, it would land here."""
    report = probe(settlements=[settlement(market_result="yes", value="10.00")])
    assert report.settlements_value_above_one == 1


def test_cost_and_count_co_presence_is_recorded():
    report = probe(settlements=[settlement(
        yes_count_fp="10.00", yes_total_cost_dollars="5.60",
    )])
    assert report.settlements_cost_and_counts_both_present == 1


def test_economics_semantics_emit_no_amounts():
    report = probe(settlements=[settlement(
        market_result="yes", yes_count_fp="13.00", no_count_fp="0.00",
        revenue="13.00", value="1.0000", yes_total_cost_dollars="7.28",
    )])
    rendered = report.render()
    for amount in ("13.00", "7.28", "1.0000"):
        assert amount not in rendered
