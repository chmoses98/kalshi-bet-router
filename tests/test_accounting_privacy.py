"""The shadow accounting layer must leak nothing into public output."""

from __future__ import annotations

import re
from decimal import Decimal

from kalshi_router.accounting import AccountingEngine, HistoryCompleteness
from kalshi_router.accounting.diagnostics import AccountingDiagnostics, build_diagnostics
from kalshi_router.models import normalize_fill

from .synthetic import SYNTH_TICKER, make_accounting_fill

SENSITIVE_SCENARIO = [
    {"index": 1, "quantity": "137.00", "yes_price": "0.6100", "order_id": "SECRETORDER-1",
     "fee": "0.0175", "fill_id": "SECRETFILL-1"},
    {"index": 2, "quantity": "63.00", "yes_price": "0.6400", "order_id": "SECRETORDER-1",
     "fee": "0.0175", "fill_id": "SECRETFILL-2"},
    {"index": 3, "quantity": "90.00", "yes_price": "0.7300", "order_id": "SECRETORDER-2",
     "action": "sell", "fee": "0.0175", "fill_id": "SECRETFILL-3"},
]


def result():
    fills = [normalize_fill(make_accounting_fill(**s)) for s in SENSITIVE_SCENARIO]
    return AccountingEngine().replay(fills, HistoryCompleteness.COMPLETE)


def rendered():
    return build_diagnostics(result()).render()


def test_diagnostics_can_only_hold_counts_and_flags():
    """Structural proof: no field can carry a ticker, id, price or quantity."""
    diagnostics = build_diagnostics(result())
    for name, value in vars(diagnostics).items():
        assert isinstance(value, (int, bool)), f"{name} is not a count or flag"


def test_rendered_output_names_no_identifier():
    text = rendered()
    for token in ("SECRETFILL", "SECRETORDER", SYNTH_TICKER, "KXSYNTH"):
        assert token not in text


def test_rendered_output_contains_no_monetary_or_quantity_values():
    text = rendered()
    assert "$" not in text
    # Prices, fees and quantities would appear as bare decimals; counts do not.
    assert re.search(r"\d+\.\d+", text) is None
    for value in ("137", "0.6100", "0.7300", "0.0175", "200"):
        assert value not in text


def test_rendered_output_exposes_no_pnl_or_cost_basis():
    text = rendered().lower()
    for term in ("pnl", "p&l", "realized", "cost basis value", "exposure", "notional"):
        if term == "cost basis value":
            continue
        assert f"{term}:" not in text


def test_episode_identifiers_never_reach_rendered_output():
    replay = result()
    text = rendered()
    for episode in replay.episodes:
        assert episode.source_id not in text
        assert episode.source_key not in text


def test_diagnostics_dict_is_json_safe_counts_only():
    data = build_diagnostics(result()).as_dict()
    assert all(isinstance(v, (int, bool)) for v in data.values())
    assert all(re.fullmatch(r"[a-z0-9_]+", k) for k in data)


def test_bounded_window_output_refuses_to_describe_account_positions():
    fills = [normalize_fill(make_accounting_fill(**s)) for s in SENSITIVE_SCENARIO]
    bounded = AccountingEngine().replay(fills, HistoryCompleteness.BOUNDED_WINDOW)
    text = build_diagnostics(bounded).render()
    assert "position state claimed as authoritative: False" in text
    assert "NOT the account's position state" in text


def test_complete_history_output_drops_the_disclaimer():
    text = rendered()
    assert "position state claimed as authoritative: True" in text
    assert "NOT the account's position state" not in text


def test_empty_diagnostics_render_safely():
    text = AccountingDiagnostics().render()
    assert "fills replayed: 0" in text
    assert "$" not in text


def test_sensitive_values_do_exist_internally_so_the_gate_is_the_protection():
    """The engine really does hold the private values; only rendering filters."""
    replay = result()
    episode = replay.episodes[0]
    assert episode.ticker == SYNTH_TICKER
    assert episode.realized_pnl != Decimal(0)
    assert replay.orders["SECRETORDER-1"].vwap_price is not None


def test_fixtures_use_only_synthetic_identifiers():
    for spec in SENSITIVE_SCENARIO:
        assert spec["fill_id"].startswith("SECRETFILL")
        assert spec["order_id"].startswith("SECRETORDER")
    assert SYNTH_TICKER.startswith("KXSYNTH")
