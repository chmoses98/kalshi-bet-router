"""Reconciliation by identity: every wager the router built is on a ledger, on a proposal, or refused.

2026 week 2 had a fourth state -- NFL wagers that existed on the exchange and nowhere else, with no receipt and no
red run. The production-filter half of that is now BLOCKED health; this is the delivery half. Identity, never
row counts: a payload of three wagers against a ledger holding three OTHER wagers must not read as reconciled.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("reconcile_delivery", ROOT / "scripts" / "reconcile_delivery.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)


def _repo(tmp_path, files: dict):
    repo = tmp_path / "ledger"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "x"], check=True)
    return repo


def _nfl_record(key, wid):
    return json.dumps({"imported_wager_id": wid, "source_bet_key": key, "note": "é"}, indent=1)


def _write(tmp_path, name, doc):
    p = tmp_path / name
    p.write_text(json.dumps(doc))
    return str(p)


def _run(tmp_path, repo, sport, payload, receipts, kind="wagers"):
    return R.main(["--sport", sport, "--work", str(repo), "--payload", _write(tmp_path, "p.json", payload),
                   "--receipts", _write(tmp_path, "r.json", receipts), "--ref", "HEAD", "--kind", kind])


def test_nfl_per_file_ledger_reads_every_key_including_non_ascii(tmp_path):
    repo = _repo(tmp_path, {
        "data/imported_wagers/2026/week_01/routed-a.json": _nfl_record("k1", "routed-a"),
        "data/imported_wagers/2026/week_02/routed-b.json": _nfl_record("k2", "routed-b"),
        "data/wager_settlements/2026/week_01/stl-a.json": json.dumps({"settlement_id": "stl-a", "source_bet_key": "k1"}),
    })
    from kalshi_router.destinations import profile_for
    assert R.ledger_keys(str(repo), "HEAD", profile_for("NFL")) == {"k1", "k2"}
    assert R.ledger_keys(str(repo), "HEAD", profile_for("NFL"), "settlements") == {"k1"}


def test_everything_accounted_for_passes(tmp_path, capsys):
    repo = _repo(tmp_path, {"data/imported_wagers/2026/week_01/routed-a.json": _nfl_record("k1", "routed-a")})
    payload = {"rows": [{"source_bet_key": "k1"}, {"source_bet_key": "k2"}, {"source_bet_key": "k3"}]}
    receipts = {"rows": [{"source_bet_key": "k2", "imported_wager_id": "routed-b", "status": "NEW", "success": True},
                         {"source_bet_key": "k3", "status": "REFUSED", "success": False, "reason": "no week"}]}
    assert _run(tmp_path, repo, "NFL", payload, receipts) == 0
    out = capsys.readouterr().out
    assert "on ledger 1, proposed not merged 1, refused 1" in out and "UNACCOUNTED 0" in out
    for secret in ("k1", "k2", "k3"):
        assert f"'{secret}'" not in out and f" {secret} " not in out


def test_a_wager_on_no_ledger_no_proposal_and_refused_by_nobody_fails(tmp_path, capsys):
    """Identity, not counts: the ledger holds three wagers, the payload three OTHERS."""
    repo = _repo(tmp_path, {f"data/imported_wagers/2026/week_01/routed-{c}.json": _nfl_record(f"old{c}", f"routed-{c}")
                            for c in "abc"})
    payload = {"rows": [{"source_bet_key": "n1"}, {"source_bet_key": "n2"}, {"source_bet_key": "n3"}]}
    assert _run(tmp_path, repo, "NFL", payload, {"rows": []}) == 1
    assert "UNACCOUNTED 3" in capsys.readouterr().out


def test_mlb_jsonl_ledger_and_camelcase_keys(tmp_path):
    repo = _repo(tmp_path, {"data/edgelab/bets/bets.jsonl": json.dumps({"sourceBetKey": "m1"}) + "\n"})
    payload = {"importBatchId": "kalshi-router-v1", "rows": [{"sourceBetKey": "m1"}, {"sourceBetKey": "m2"}]}
    receipts = [{"sourceBetKey": "m2", "betId": "b2", "duplicateStatus": "CONFLICT", "success": False}]
    assert _run(tmp_path, repo, "MLB", payload, receipts) == 0


def test_settlement_rows_reconcile_against_the_settlement_ledger(tmp_path, capsys):
    repo = _repo(tmp_path, {"data/wager_settlements/2026/week_01/stl-a.json":
                            json.dumps({"settlement_id": "stl-a", "source_bet_key": "k1"})})
    payload = {"settlements": [{"source_bet_key": "k1"}, {"source_bet_key": "k9"}]}
    assert _run(tmp_path, repo, "NFL", payload, {"rows": []}, kind="settlements") == 1
    assert "on ledger 1" in capsys.readouterr().out


def test_both_delivery_workflows_reconcile_every_destination():
    for name in ("deliver-wagers.yml", "settle-wagers.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text()
        assert "scripts/reconcile_delivery.py" in text and "reconcile()" in text
        # called on the no-op path as well as after a delivery
        assert text.count("\n                reconcile\n") + text.count("\n              reconcile\n") >= 1
        assert "\n            reconcile\n          done" in text


def test_a_conflict_on_a_key_already_on_the_ledger_is_refused_not_delivered(tmp_path, capsys):
    """The live MLB case of 2026-09-24: the ledger holds the order with different economics."""
    repo = _repo(tmp_path, {"data/edgelab/bets/bets.jsonl": json.dumps({"sourceBetKey": "m1"}) + "\n"})
    payload = {"rows": [{"sourceBetKey": "m1"}]}
    receipts = [{"sourceBetKey": "m1", "betId": "b1", "duplicateStatus": "CONFLICT", "success": False}]
    assert _run(tmp_path, repo, "MLB", payload, receipts) == 0
    out = capsys.readouterr().out
    assert "on ledger 0" in out and "refused 1" in out and "CONFLICT" in out


def test_a_settlement_refused_because_its_wager_is_not_on_the_ledger_yet_is_named_as_awaiting_it(tmp_path, capsys):
    """2026-09-26: a wager on an open, unmerged proposal whose market already settled. The importer refuses the
    settlement per row; reconciliation keeps it REFUSED -- never proposed, never on the ledger, never UNACCOUNTED --
    and says it awaits its wager, so an operator looks at the wager proposal rather than for corrupt data."""
    repo = _repo(tmp_path, {"wagers/2026.jsonl": "".join(json.dumps({"source_bet_key": k}) + "\n"
                                                         for k in ("w1", "w2", "w3"))})
    payload = {"settlements": [{"source_bet_key": k} for k in ("w1", "w2", "w3", "pending", "conflicted")]}
    receipts = {"rows": [
        {"source_bet_key": "w1", "settlement_id": "stl-1", "duplicate_status": "NEW", "success": True},
        {"source_bet_key": "w2", "settlement_id": "stl-2", "duplicate_status": "NEW", "success": True},
        # Refused for a reason that is NOT a missing wager: its wager is on the ledger.
        {"source_bet_key": "w3", "settlement_id": None, "duplicate_status": "REFUSED", "success": False},
        {"source_bet_key": "pending", "settlement_id": None, "duplicate_status": "REFUSED", "success": False},
    ]}
    # "conflicted" has no receipt at all: that is still UNACCOUNTED, and still fails.
    assert _run(tmp_path, repo, "CFB", payload, receipts, kind="settlements") == 1
    out = capsys.readouterr().out
    assert "proposed not merged 2, refused 2 {'REFUSED': 2} (1 of them await their wager" in out
    assert "UNACCOUNTED 1" in out


def test_the_awaiting_sub_count_never_moves_a_row_out_of_refused():
    from kalshi_router.receipts import normalise
    receipts = normalise({"rows": [{"source_bet_key": "p", "duplicate_status": "REFUSED", "success": False}]})
    with_parents = R.classify(["p"], set(), receipts, parents_on_ledger=set())
    without = R.classify(["p"], set(), receipts)
    assert with_parents["counts"] == without["counts"] == {"ON_LEDGER": 0, "PROPOSED_NOT_MERGED": 0,
                                                           "REFUSED": 1, "UNACCOUNTED": 0}
    assert with_parents["refused_awaiting_parent"] == 1
    assert "refused_awaiting_parent" not in without
