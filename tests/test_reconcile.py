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
        assert isinstance(value, int), f"{name} is not a count"
