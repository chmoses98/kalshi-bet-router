"""The shared destination ledger: idempotent, conflict-refusing, orphan-refusing, append-only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from edge_finder_contract import routed_ledger as rl

SPEC = rl.LedgerSpec(sport="NBA", id_prefix="nba", wager_schema="nba_accounted_wager.v1",
                     settlement_schema="nba_wager_settlement.v1")
KEY = "kalshi:v1:" + "ab" * 32


def row(**over):
    base = {"source_bet_key": KEY, "import_batch_id": "kalshi-router-v1", "entry_method": "IMPORTED_RECEIPT",
            "game_date": "2026-10-03", "market_ticker": "KXNBAGAME-26OCT03MIATOR-TOR", "side": "YES",
            "executed_at": "2026-10-03T20:00:00Z", "contracts": 10.0, "execution_price": 0.46, "stake": 4.7,
            "fees_paid": 0.1, "fees_are_estimated": False, "venue": "kalshi"}
    base.update(over)
    return base


def stl(**over):
    base = {"source_bet_key": KEY, "market_ticker": "KXNBAGAME-26OCT03MIATOR-TOR", "side": "YES",
            "settlement_status": "SETTLED", "settled_at": "2026-10-04T03:00:00Z", "result": "WON",
            "gross_return": 10.0, "net_profit_loss": 5.3, "refusals": [], "venue": "kalshi",
            "economics_version": rl.ECONOMICS_V2}
    base.update(over)
    return base


def test_new_then_duplicate_noop_is_byte_identical(tmp_path):
    r1 = rl.import_wagers(SPEC, tmp_path, [row()], "kalshi-router-v1")
    assert r1.written == 1 and r1.rows[0]["status"] == "NEW"
    assert r1.rows[0]["wager_id"] == SPEC.mint_wager_id(KEY) == "nbaw-" + r1.rows[0]["wager_id"][5:]
    before = SPEC.wagers_path(tmp_path).read_bytes()
    r2 = rl.import_wagers(SPEC, tmp_path, [row()], "kalshi-router-v1")
    assert r2.duplicate == 1 and r2.written == 0 and r2.rows[0]["status"] == "DUPLICATE_NOOP"
    assert SPEC.wagers_path(tmp_path).read_bytes() == before


def test_conflict_never_rewrites_and_names_fields_only(tmp_path):
    rl.import_wagers(SPEC, tmp_path, [row()], "kalshi-router-v1")
    before = SPEC.wagers_path(tmp_path).read_bytes()
    r = rl.import_wagers(SPEC, tmp_path, [row(contracts=11.0, stake=5.16)], "kalshi-router-v1")
    assert r.refused == 1 and r.rows[0]["status"] == "CONFLICT"
    assert r.rows[0]["conflicting_fields"] == ["contracts", "stake"]
    assert "11" not in json.dumps(r.rows[0])
    assert SPEC.wagers_path(tmp_path).read_bytes() == before


def test_nfl_dialect_actual_price_is_accepted_and_stored_as_execution_price(tmp_path):
    r = rl.import_wagers(SPEC, tmp_path, [{**{k: v for k, v in row().items() if k != "execution_price"},
                                           "actual_price": 0.46, "fee_state": "ACTUAL_API_FILL", "execution_action": "BUY"}],
                         "kalshi-router-v1")
    assert r.written == 1
    stored = rl.read_jsonl(SPEC.wagers_path(tmp_path))[0]
    assert stored["execution_price"] == 0.46 and "actual_price" not in stored and stored["execution_action"] == "BUY"


@pytest.mark.parametrize("bad", [
    {"recommendation_id": "rec_x"}, {"model_probability": 0.5}, {"fair_probability": 0.6}, {"edge_v2": 0.1},
    {"unknown_field": 1}, {"fees_are_estimated": True}, {"side": "yes"}, {"executed_at": "2026-10-03T20:00:00"},
    {"stake": 99.0}, {"venue": "polymarket"},
])
def test_refusals(tmp_path, bad):
    r = rl.import_wagers(SPEC, tmp_path, [row(**bad)], "kalshi-router-v1")
    assert r.refused == 1 and r.rows[0]["status"] == "REFUSED" and not SPEC.wagers_path(tmp_path).exists()


def test_batch_id_disagreement_is_refused(tmp_path):
    r = rl.import_wagers(SPEC, tmp_path, [row(import_batch_id="other")], "kalshi-router-v1")
    assert r.refused == 1


def test_settlement_requires_its_wager_and_is_idempotent(tmp_path):
    orphan = rl.import_settlements(SPEC, tmp_path, [stl()])
    assert orphan.refused == 1 and "ORPHAN" in orphan.rows[0]["reason"]
    rl.import_wagers(SPEC, tmp_path, [row()], "kalshi-router-v1")
    s1 = rl.import_settlements(SPEC, tmp_path, [stl()])
    assert s1.written == 1 and s1.rows[0]["settlement_id"] == SPEC.mint_settlement_id(KEY)
    before = SPEC.settlements_path(tmp_path).read_bytes()
    s2 = rl.import_settlements(SPEC, tmp_path, [stl()])
    assert s2.duplicate == 1 and SPEC.settlements_path(tmp_path).read_bytes() == before
    s3 = rl.import_settlements(SPEC, tmp_path, [stl(net_profit_loss=1.0)])
    assert s3.rows[0]["status"] == "CONFLICT"
    bad_side = rl.import_settlements(SPEC, tmp_path, [stl(side="NO")])
    assert bad_side.refused == 1
    unestablished = rl.import_settlements(SPEC, tmp_path, [stl(source_bet_key="kalshi:v1:" + "cd" * 32)])
    assert unestablished.refused == 1  # orphan again
    assert rl.validate_settlement(SPEC, {**stl(), "settlement_id": SPEC.mint_settlement_id(KEY), "schema_version": SPEC.settlement_schema,
                                         "gross_return": None, "net_profit_loss": None, "refusals": []})


def test_validator_proves_append_only_and_orphans(tmp_path):
    rl.import_wagers(SPEC, tmp_path, [row()], "kalshi-router-v1")
    rl.import_settlements(SPEC, tmp_path, [stl()])
    ok = rl.validate_ledger(SPEC, tmp_path)
    assert ok["ok"] and ok["counts"] == {"wagers": 1, "settlements": 1}
    base = {"data/accounting/wagers.jsonl": SPEC.wagers_path(tmp_path).read_text()}
    SPEC.wagers_path(tmp_path).write_text("")
    rewritten = rl.validate_ledger(SPEC, tmp_path, base)
    assert not rewritten["ok"] and any("append-only" in f for f in rewritten["failures"])
    assert any("ORPHAN" in f for f in rewritten["failures"])


def test_cli_prints_counts_only_and_writes_receipts(tmp_path, capsys):
    payload = tmp_path / "p.json"
    payload.write_text(json.dumps({"importBatchId": "kalshi-router-v1", "rows": [row(), row(source_bet_key="kalshi:v1:" + "ef" * 32, fees_are_estimated=True)]}))
    receipts = tmp_path / "r.json"
    code = rl.run_import_cli(SPEC, "wagers", ["--payload", str(payload), "--base-dir", str(tmp_path / "ledger"), "--receipts-out", str(receipts)])
    out = capsys.readouterr().out
    assert code == rl.EXIT_REFUSED  # one refusal
    assert "KXNBAGAME" not in out and "4.7" not in out and KEY not in out
    rec = json.loads(receipts.read_text())
    assert rec["written"] == 1 and rec["refused"] == 1 and rec["importBatchId"] == "kalshi-router-v1"
    assert rec["rows"][0]["source_bet_key"] == KEY and rec["rows"][0]["status"] == "NEW"
    code = rl.run_validate_cli(SPEC, ["--base-dir", str(tmp_path / "ledger"), "--result-out", str(tmp_path / "v.json")])
    assert code == 0 and json.loads((tmp_path / "v.json").read_text())["passed"] is True
