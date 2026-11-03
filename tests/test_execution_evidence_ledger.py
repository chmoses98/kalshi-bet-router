"""The vendored execution-evidence ledger: append-only, digest-only, producer-separated, and pinned."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "contract" / "standalone" / "execution_evidence_ledger.py"


def _load():
    spec = importlib.util.spec_from_file_location("execution_evidence_ledger", MODULE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["execution_evidence_ledger"] = mod
    spec.loader.exec_module(mod)
    return mod


L = _load()
KEY = "kalshi:v1:" + "ab" * 32
D1, D2, D3 = "a" * 32, "b" * 32, "c" * 32
NOW = "2026-11-02T23:00:00Z"


def execution(**over):
    row = {"schema_version": L.SCHEMA_VERSION, "kind": "EXECUTION", "producer": "router", "source_bet_key": KEY,
           "recorded_at": NOW, "order_id_digest": D1, "fill_id_digests": [D2, D3], "fill_count": 2,
           "first_fill_ts": 1_790_000_000, "last_fill_ts": 1_790_000_030, "execution_action": "BUY",
           "all_taker": True, "flags": []}
    row.update(over)
    return row


def close(quality="TRUE_CLOSE", **over):
    body = {"quality": quality, "yes_bid": 0.48, "yes_ask": 0.52, "yes_mid": 0.5, "as_of_ts": 1_790_000_000,
            "cutoff_ts": 1_790_000_060, "period_minutes": 1, "source_endpoint": "series_market_candlesticks",
            "flags": []}
    body.update(over)
    return {"schema_version": L.SCHEMA_VERSION, "kind": "CLOSE", "producer": "router", "source_bet_key": KEY,
            "recorded_at": NOW, "market_ticker": "KXNCAAFSPREAD-26OCT31XXXYYY-XXX3", "side": "YES", "close": body}


def link(**over):
    row = {"schema_version": L.SCHEMA_VERSION, "kind": "LINK", "producer": "edge_finder", "source_bet_key": KEY,
           "recorded_at": NOW, "status": "LINKED", "recommendation_ref": D1, "model_run_ref": D2,
           "model_as_of": "2026-11-02T20:00:00Z", "link_basis": {"match": "exact"}}
    row.update(over)
    return row


def test_valid_rows_of_every_kind_are_accepted():
    for row in (execution(), close(), link()):
        assert L.validate_row(row) == []


def test_pinned_copy_matches_the_source():
    # Destinations vendor this file verbatim; the pin is what makes a drifted copy fail CI there and here.
    digest = hashlib.sha256(MODULE.read_bytes()).hexdigest()
    pin = (ROOT / "contract" / "standalone" / "execution_evidence_ledger.sha256").read_text().split()[0]
    assert digest == pin, "re-pin with scripts/vendor_execution_evidence.py --pin after an intentional change"


# ----------------------------------------------------------------------------------------------- privacy

@pytest.mark.parametrize("bad", [
    {"order_id": "x"}, {"fill_ids": ["a"]}, {"trade_id": "t"}, {"client_order_id": "c"}, {"fills": []},
])
def test_raw_id_field_names_are_refused(bad):
    assert L.validate_row({**execution(), **bad})


def test_uuid_shaped_values_are_refused_anywhere():
    uuid = "123e4567-e89b-12d3-a456-426614174000"
    assert L.validate_row(execution(flags=[uuid]))
    assert L.validate_row(link(link_basis={"note": f"order {uuid}"}))
    assert L.validate_row(close(source_endpoint=uuid))


def test_refusal_text_never_echoes_the_offending_value():
    uuid = "123e4567-e89b-12d3-a456-426614174000"
    problems = L.validate_row(execution(flags=[uuid]))
    assert uuid not in " ".join(problems)


def test_digests_must_be_32_lowercase_hex():
    assert L.validate_row(execution(order_id_digest="A" * 32))
    assert L.validate_row(execution(order_id_digest="a" * 31))
    assert L.validate_row(execution(fill_id_digests=["zz"]))
    assert L.validate_row(execution(fill_id_digests=[D2, D2]))


# ---------------------------------------------------------------------------------------- producer split

def test_router_cannot_write_a_recommendation_link():
    assert L.validate_row(link(producer="router"))


def test_edge_finder_cannot_write_execution_facts():
    assert L.validate_row(execution(producer="edge_finder"))


def test_linked_requires_a_reference_and_model_time_and_unlinked_forbids_them():
    assert L.validate_row(link(recommendation_ref=None))
    assert L.validate_row(link(model_as_of=None))
    assert L.validate_row(link(model_as_of="2026-11-02T20:00:00"))  # no zone
    assert L.validate_row(link(status="MISSING"))  # still carries a ref
    assert L.validate_row(link(status="MISSING", recommendation_ref=None, model_run_ref=None, model_as_of=None)) == []


# ------------------------------------------------------------------------------------ close labelling

def test_a_missing_close_carries_a_reason_and_no_price():
    ok = close("MISSING", reason="NO_CANDLE_AT_OR_BEFORE_CUTOFF")
    ok["close"] = {"quality": "MISSING", "reason": "NO_CANDLE_AT_OR_BEFORE_CUTOFF"}
    assert L.validate_row(ok) == []
    priced = close("MISSING", reason="x")
    assert L.validate_row(priced)


def test_a_true_close_cannot_come_from_a_daily_candle():
    assert L.validate_row(close("TRUE_CLOSE", period_minutes=1440))
    assert L.validate_row(close("EARLIER_QUOTE_PROXY", period_minutes=1440)) == []


def test_a_close_after_its_cutoff_is_refused_and_a_crossed_quote_is_refused():
    assert L.validate_row(close(as_of_ts=1_790_000_100, cutoff_ts=1_790_000_060))
    assert L.validate_row(close(yes_bid=0.6, yes_ask=0.4))
    assert L.validate_row(close(yes_bid=None))  # bid and ask travel together


# --------------------------------------------------------------------------------------------- append-only

def test_import_is_idempotent_and_never_rewrites(tmp_path):
    ledger = tmp_path / "data" / "accounting" / "execution_evidence.jsonl"
    first = L.import_rows(ledger, [execution(), close()])
    assert (first.written, first.duplicate, first.refused) == (2, 0, 0)
    before = ledger.read_bytes()
    again = L.import_rows(ledger, [execution(recorded_at="2026-11-03T00:00:00Z"), close()])
    assert (again.written, again.duplicate, again.refused) == (0, 2, 0)  # recorded_at is not identity
    assert ledger.read_bytes() == before


def test_same_key_different_content_is_a_conflict_not_an_overwrite(tmp_path):
    ledger = tmp_path / "e.jsonl"
    L.import_rows(ledger, [execution()])
    before = ledger.read_bytes()
    res = L.import_rows(ledger, [execution(first_fill_ts=1_790_000_001)])
    assert res.refused == 1 and res.receipts[0]["status"] == "CONFLICT"
    assert ledger.read_bytes() == before


def test_a_true_close_and_an_earlier_proxy_coexist_and_neither_replaces_the_other(tmp_path):
    ledger = tmp_path / "e.jsonl"
    proxy = close("EARLIER_QUOTE_PROXY", period_minutes=60, minutes_before_cutoff=300)
    L.import_rows(ledger, [proxy])
    res = L.import_rows(ledger, [close("TRUE_CLOSE")])
    assert res.written == 1
    assert {json.loads(x)["close"]["quality"] for x in ledger.read_text().splitlines()} == {
        "EARLIER_QUOTE_PROXY", "TRUE_CLOSE"}


def test_a_refused_row_does_not_stop_the_good_ones(tmp_path):
    ledger = tmp_path / "e.jsonl"
    res = L.import_rows(ledger, [execution(), {"kind": "EXECUTION", "order_id": "x"}, close()])
    assert [r["status"] for r in res.receipts] == ["NEW", "REFUSED", "NEW"]


def test_validate_ledger_flags_orphans_and_duplicates(tmp_path):
    ledger = tmp_path / "e.jsonl"
    L.import_rows(ledger, [execution()])
    assert L.validate_ledger(ledger, {KEY}) == []
    assert any("orphan" in p for p in L.validate_ledger(ledger, {"kalshi:v1:" + "cd" * 32}))
    with ledger.open("a") as fh:
        fh.write(json.dumps(execution()) + "\n")
    assert any("duplicate key" in p for p in L.validate_ledger(ledger))


# ---------------------------------------------------------------------------------- recommendation join

def candidate(ref, ts, **over):
    c = {"market_ticker": "KXNCAAFSPREAD-26OCT31XXXYYY-XXX3", "side": "YES", "created_at_ts": ts,
         "recommendation_ref": ref, "model_run_ref": D2, "model_as_of": "2026-11-02T20:00:00Z"}
    c.update(over)
    return c


def test_join_links_only_on_an_exact_unique_match():
    ex = execution()
    t, s = "KXNCAAFSPREAD-26OCT31XXXYYY-XXX3", "YES"
    got = L.link_recommendation(ex, [candidate(D1, 1_789_999_000)], window_seconds=7200, market_ticker=t, side=s)
    assert got["status"] == "LINKED" and got["recommendation_ref"] == D1
    assert L.validate_row(got) == []


def test_join_reports_missing_and_never_guesses():
    ex = execution()
    t, s = "KXNCAAFSPREAD-26OCT31XXXYYY-XXX3", "YES"
    for cands in (
        [],
        [candidate(D1, 1_789_999_000, side="NO")],                 # opposite side
        [candidate(D1, 1_789_999_000, market_ticker="OTHER")],     # other market
        [candidate(D1, 1_790_000_500)],                            # created AFTER the fill
        [candidate(D1, 1_789_900_000)],                            # outside the window
    ):
        got = L.link_recommendation(ex, cands, window_seconds=7200, market_ticker=t, side=s)
        assert got["status"] == "MISSING"
        assert L.validate_row(got) == []


def test_join_is_ambiguous_between_two_recommendations_and_not_resolved_by_nearest():
    ex = execution()
    t, s = "KXNCAAFSPREAD-26OCT31XXXYYY-XXX3", "YES"
    got = L.link_recommendation(ex, [candidate(D1, 1_789_999_000), candidate(D3, 1_789_999_900)],
                                window_seconds=7200, market_ticker=t, side=s)
    assert got["status"] == "AMBIGUOUS" and "recommendation_ref" not in got


def test_join_has_no_default_window():
    with pytest.raises(ValueError):
        L.link_recommendation(execution(), [], window_seconds=0, market_ticker="t", side="YES")


# ------------------------------------------------------------------------------------------------- CLI

def test_cli_import_and_validate(tmp_path):
    payload = tmp_path / "p.json"
    payload.write_text(json.dumps({"evidence": [execution(), close()]}))
    ledger = tmp_path / "led" / "e.jsonl"
    run = subprocess.run([sys.executable, str(MODULE), "import", "--payload", str(payload), "--ledger", str(ledger)],
                         capture_output=True, text=True)
    assert run.returncode == 0 and "written=2" in run.stdout
    assert KEY not in run.stdout and D1 not in run.stdout  # counts only
    again = subprocess.run([sys.executable, str(MODULE), "import", "--payload", str(payload), "--ledger", str(ledger)],
                           capture_output=True, text=True)
    assert again.returncode == 0 and "written=0 alreadyPresent=2" in again.stdout
    check = subprocess.run([sys.executable, str(MODULE), "validate", "--ledger", str(ledger)],
                           capture_output=True, text=True)
    assert check.returncode == 0 and "problems=0" in check.stdout


def test_cli_exit_is_nonzero_on_a_refused_row(tmp_path):
    payload = tmp_path / "p.json"
    payload.write_text(json.dumps({"evidence": [{"kind": "EXECUTION", "order_id": "x"}]}))
    run = subprocess.run([sys.executable, str(MODULE), "import", "--payload", str(payload),
                          "--ledger", str(tmp_path / "e.jsonl")], capture_output=True, text=True)
    assert run.returncode == 1


# --------------------------------------------------- the wager ledger is untouched by this feature

def test_wager_contract_is_unchanged_so_no_old_importer_refuses_anything_new():
    sys.path.insert(0, str(ROOT / "contract"))
    from edge_finder_contract import routed_ledger as rl

    assert rl.WAGER_INPUT_FIELDS == ("source_bet_key", "import_batch_id", "entry_method", "game_date", "market_ticker",
                                     "side", "executed_at", "contracts", "execution_price", "actual_price", "stake",
                                     "fees_paid", "fees_are_estimated", "venue", "fee_state", "execution_action")
    for forbidden in ("order_id_digest", "fill_id_digests", "first_fill_ts", "recommendation_ref", "model_run_ref"):
        assert forbidden not in rl.WAGER_INPUT_FIELDS


def test_evidence_file_beside_the_wagers_does_not_disturb_the_wager_importer(tmp_path):
    sys.path.insert(0, str(ROOT / "contract"))
    from edge_finder_contract import routed_ledger as rl

    spec = rl.LedgerSpec("nba", "nba", "nba_accounted_wager.v1", "nba_accounted_settlement.v1")
    row = {"source_bet_key": KEY, "import_batch_id": "b", "entry_method": "IMPORTED_RECEIPT",
           "game_date": "2026-11-01", "market_ticker": "KXNBAGAME-26NOV01AAABBB-AAA", "side": "YES",
           "executed_at": "2026-11-01T20:00:00Z", "contracts": 10.0, "execution_price": 0.5, "stake": 5.1,
           "fees_paid": 0.1, "fees_are_estimated": False, "venue": "kalshi"}
    L.import_rows(tmp_path / "data" / "accounting" / "execution_evidence.jsonl", [execution()])
    res = rl.import_wagers(spec, tmp_path, [row], "b")
    assert (res.written, res.refused) == (1, 0)
    again = rl.import_wagers(spec, tmp_path, [row], "b")
    assert (again.written, again.duplicate) == (0, 1)
