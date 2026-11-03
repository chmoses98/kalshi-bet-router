"""Execution evidence sidecar: ids appear only as digests, missing evidence is flagged, wager rows are untouched."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from kalshi_router.accounting.execution import OrderExecution
from kalshi_router.execution_evidence import (
    ACTION_NOT_ESTABLISHED,
    FILL_IDS_MISSING,
    SCHEMA_VERSION,
    TAKER_NOT_ESTABLISHED,
    build_execution_evidence,
    id_digest,
)
from kalshi_router.models import Action, OutcomeSide
from kalshi_router.production import ProductionWager, production_source_key, to_cfb_import_row

SECRET_ORDER = "ORDER-SECRET-123"
SECRET_FILLS = ("FILL-SECRET-A", "FILL-SECRET-B")


def order(**kw) -> OrderExecution:
    base = dict(
        order_id=SECRET_ORDER, ticker="KXNCAAFSPREAD-SECRET", outcome_side=OutcomeSide.YES, book_side=None,
        subaccount_number=0, total_quantity=Decimal(10), fill_count=2, fill_ids=SECRET_FILLS,
        first_execution_time=Decimal(1788264060), last_execution_time=Decimal(1788264075),
        vwap_price=Decimal("0.56"), total_fee=Decimal("0.01"), fills_with_fee=2,
        all_taker=True, legacy_action=Action.BUY)
    base.update(kw)
    return OrderExecution(**base)


def wager_for(o: OrderExecution) -> ProductionWager:
    return ProductionWager(
        source_key=production_source_key(o.subaccount_number, o.ticker, o.order_id), market_ticker=o.ticker,
        sport="CFB", game_date="2026-10-31", side="YES", contracts=o.total_quantity, vwap_price=o.vwap_price,
        total_fees=o.total_fee, stake=Decimal("5.61"), first_execution_time=o.first_execution_time,
        last_execution_time=o.last_execution_time, fill_count=o.fill_count, finality=None,
        execution_action="BUY")


def test_row_has_digests_times_and_no_raw_ids():
    o = order()
    row = build_execution_evidence(o, wager_for(o)).to_sidecar_row()
    assert row["schema_version"] == SCHEMA_VERSION
    assert row["source_bet_key"] == production_source_key(0, o.ticker, o.order_id)
    assert row["fill_count"] == 2 and row["execution_action"] == "BUY" and row["all_taker"] is True
    assert row["first_fill_ts"] == 1788264060 and row["last_fill_ts"] == 1788264075 and row["flags"] == []
    blob = json.dumps(row)
    for raw in (SECRET_ORDER, *SECRET_FILLS, "SECRET"):
        assert raw not in blob, "a raw exchange id leaked into the sidecar row"
    assert len(row["order_id_digest"]) == 32 and all(len(d) == 32 for d in row["fill_id_digests"])


def test_digest_is_deterministic_and_domain_separated():
    assert id_digest("order", "abc") == id_digest("order", "abc")
    assert id_digest("order", "abc") != id_digest("fill", "abc")
    assert id_digest("order", "abc") != id_digest("order", "abd")
    with pytest.raises(ValueError):
        id_digest("trade", "abc")


def test_the_owner_can_match_a_ledger_row_by_recomputing_the_digest():
    row = build_execution_evidence(order()).to_sidecar_row()
    assert row["order_id_digest"] == id_digest("order", SECRET_ORDER)
    assert set(row["fill_id_digests"]) == {id_digest("fill", f) for f in SECRET_FILLS}


def test_missing_evidence_is_flagged_never_inferred():
    ev = build_execution_evidence(order(fill_ids=(), fill_count=0, legacy_action=None, all_taker=None))
    assert set(ev.flags) == {FILL_IDS_MISSING, ACTION_NOT_ESTABLISHED, TAKER_NOT_ESTABLISHED}
    assert ev.execution_action is None and ev.all_taker is None and ev.fill_id_digests == ()


def test_a_wager_from_another_order_is_refused():
    with pytest.raises(ValueError):
        build_execution_evidence(order(), wager_for(order(order_id="OTHER-ORDER")))


def test_evidence_is_deterministic_across_runs_so_a_rerun_is_idempotent():
    a = build_execution_evidence(order(fill_ids=SECRET_FILLS[::-1])).to_sidecar_row()
    b = build_execution_evidence(order()).to_sidecar_row()
    assert a == b  # fill order does not matter; same order -> same row, so a re-run cannot create a second one


def test_building_evidence_changes_no_wager_row():
    o = order()
    w = wager_for(o)
    before = to_cfb_import_row(w, "batch-1")
    build_execution_evidence(o, w)
    assert to_cfb_import_row(replace(w), "batch-1") == before
    assert "order_id_digest" not in before and "fill_id_digests" not in before


def test_the_sidecar_is_not_a_routed_wager_row():
    """The contract's closed WAGER_INPUT_FIELDS refuses unknown keys: the sidecar must never be sent as a wager."""
    from edge_finder_contract.routed_ledger import WAGER_INPUT_FIELDS
    row = build_execution_evidence(order()).to_sidecar_row()
    assert {"order_id_digest", "fill_id_digests", "first_fill_ts"} <= set(row) - set(WAGER_INPUT_FIELDS)


# --- cross-repository agreement ----------------------------------------------------------------------------
# These four vectors are asserted verbatim in every destination's vendored test, with the formula inlined there
# (hashlib only), so a sport repository that computes recommendation_ref / model_run_ref a different way fails
# ITS OWN CI and the join in link_recommendation can never silently stop matching.
GOLDEN_DIGESTS = (
    ("order", "ORDER-X", "70b17cbeb58152c522d8be2a9a30ac34"),
    ("fill", "FILL-X", "b34311e543285f452dd37dc618573dcb"),
    ("recommendation", "rec_0001", "34284d398be9830fd5ca1d76f8a673f1"),
    ("model_run", "run_0001", "6e039e855b3d236b125624c33b87427b"),
)


@pytest.mark.parametrize("kind,value,expected", GOLDEN_DIGESTS)
def test_golden_digest_vectors_are_the_cross_repository_agreement(kind, value, expected):
    assert id_digest(kind, value) == expected


def test_unknown_digest_kinds_are_refused():
    with pytest.raises(ValueError):
        id_digest("trade", "x")


def test_sidecar_carries_execution_price_quantity_fee_and_first_last_fill_times():
    row = build_execution_evidence(order()).to_sidecar_row()
    assert row["schema_version"] == SCHEMA_VERSION == "router_execution_evidence.v2"
    assert (row["vwap_price"], row["total_quantity"], row["total_fee"]) == (0.56, 10, 0.01)
    assert (row["first_fill_ts"], row["last_fill_ts"]) == (1788264060, 1788264075)
    assert "PRICE_NOT_ESTABLISHED" not in row["flags"]


def test_a_missing_price_is_flagged_never_filled_in():
    row = build_execution_evidence(order(vwap_price=None, total_fee=None)).to_sidecar_row()
    assert row["vwap_price"] is None and row["total_fee"] is None
    assert "PRICE_NOT_ESTABLISHED" in row["flags"]


def test_no_raw_identifier_reaches_any_output():
    import re

    uuid = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
    raw_order = "0f8fad5b-d9cb-469f-a165-70867728950e"
    raw_fills = ("7c9e6679-7425-40de-944b-e07fc1f90ae7", "1b4e28ba-2fa1-11d2-883f-0016d3cca427")
    ev = build_execution_evidence(order(order_id=raw_order, fill_ids=raw_fills))
    blob = json.dumps(ev.to_sidecar_row(), sort_keys=True) + repr(ev)
    assert raw_order not in blob and not any(f in blob for f in raw_fills)
    assert not uuid.search(blob)
