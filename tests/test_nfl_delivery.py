"""NFL production delivery: the gate reads a ledger that stores one record per FILE.

2026 week 2's NFL wagers were never delivered. With no NFL destination profile, every post-cutover NFL order
was refused as NO_DESTINATION_IMPORTER -- a by-design refusal -- so health read `not_routable` and nothing
turned red. Activating NFL needed more than a table entry, because its ledger (`handicap-data`) keeps each
record in its own pretty-printed JSON file:

  * a LINE diff of such a file is not a list of rows, so the gate reads ADDED FILES as rows and any
    modified, deleted or renamed ledger file as a rewrite (append-only, in that ledger's shape);
  * only files of the exact minted shape may merge unattended;
  * the destination names its rows `imported_wager_id` and reports verdicts under `status`.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

from kalshi_router import automerge
from kalshi_router.destination import ROUTER_IMPORT_BATCH_ID
from kalshi_router.production import (
    HealthState,
    ProductionDiagnostics,
    ProductionRefusal,
    evaluate_order,
)
from kalshi_router.receipts import normalise

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("merge_delivery_pr", ROOT / "scripts" / "merge_delivery_pr.py")
merge_delivery_pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(merge_delivery_pr)

KEY = "kalshi:v1:aaaa"
WID = "routed-" + "a" * 24
EXISTING = "data/imported_wagers/2026/week_01/routed-" + "b" * 24 + ".json"
NEW = f"data/imported_wagers/2026/week_02/{WID}.json"


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _write(repo, rel, doc):
    path = Path(repo) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")


def _ledger(tmp_path):
    repo = tmp_path / "hd"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _write(repo, EXISTING, {"imported_wager_id": "routed-" + "b" * 24, "source_bet_key": "kalshi:v1:bbbb",
                            "import_batch_id": "kalshi-gap-backfill"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return repo, base


def _commit(repo):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "delivery")


# ------------------------------------------------------------------------------------------ the per-file diff

def test_an_added_record_file_is_one_whole_row(tmp_path):
    repo, base = _ledger(tmp_path)
    _write(repo, NEW, {"imported_wager_id": WID, "source_bet_key": KEY, "import_batch_id": ROUTER_IMPORT_BATCH_ID})
    _commit(repo)
    added, removed = merge_delivery_pr.ledger_diff_per_file(
        str(repo), base, list(automerge.ledger_pathspecs_for("NFL")))
    assert removed == []
    assert added == [{"imported_wager_id": WID, "source_bet_key": KEY, "import_batch_id": ROUTER_IMPORT_BATCH_ID}]


def test_touching_an_existing_record_is_a_rewrite_not_an_append(tmp_path):
    repo, base = _ledger(tmp_path)
    _write(repo, EXISTING, {"imported_wager_id": "routed-" + "b" * 24, "source_bet_key": "kalshi:v1:bbbb",
                            "import_batch_id": "kalshi-gap-backfill", "stake": 999})
    _commit(repo)
    added, removed = merge_delivery_pr.ledger_diff_per_file(
        str(repo), base, list(automerge.ledger_pathspecs_for("NFL")))
    assert added == [] and removed == [f"M {EXISTING}"]


def test_a_deleted_record_is_a_rewrite(tmp_path):
    repo, base = _ledger(tmp_path)
    (repo / EXISTING).unlink()
    _commit(repo)
    _added, removed = merge_delivery_pr.ledger_diff_per_file(
        str(repo), base, list(automerge.ledger_pathspecs_for("NFL")))
    assert removed == [f"D {EXISTING}"]


def test_an_unparseable_added_record_is_a_row_with_no_identity(tmp_path):
    repo, base = _ledger(tmp_path)
    p = repo / NEW
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    _commit(repo)
    added, _ = merge_delivery_pr.ledger_diff_per_file(str(repo), base, list(automerge.ledger_pathspecs_for("NFL")))
    assert added == [{"_unparseable": True}]


# ------------------------------------------------------------------------------------------ the gate

NFL_RECEIPTS = {"written": 1, "alreadyPresent": 0, "refused": 0, "rows": [
    {"row": 0, "source_bet_key": KEY, "imported_wager_id": WID, "status": "NEW", "success": True}]}
NFL_RERUN = {"written": 0, "alreadyPresent": 1, "refused": 0, "rows": [
    {"row": 0, "source_bet_key": KEY, "imported_wager_id": WID, "status": "DUPLICATE_NOOP", "success": True}]}


def nfl_facts(**overrides):
    base = dict(
        sport="NFL", destination_repo="chmoses98/nfl-edge-finder", pull_number=1, state="open", draft=False,
        head_ref="kalshi-router/NFL", head_sha="abc", head_repo="chmoses98/nfl-edge-finder",
        base_ref="handicap-data", mergeable=True, mergeable_state="clean", verified_sha="abc",
        changed_files=(NEW,),
        added_ledger_rows=({"imported_wager_id": WID, "source_bet_key": KEY,
                            "import_batch_id": ROUTER_IMPORT_BATCH_ID},),
        removed_ledger_rows=(),
        receipts=tuple(r.as_dict() for r in normalise(NFL_RECEIPTS)),
        rerun_receipts=tuple(r.as_dict() for r in normalise(NFL_RERUN)),
        rerun_changed_the_tree=False, check_runs=(), partial_delivery=False,
        destination_validator_passed=True,
    )
    base.update(overrides)
    return automerge.MergeFacts(**base)


def test_the_nfl_importers_receipts_normalise_with_identity_and_verdict():
    [r] = normalise(NFL_RECEIPTS)
    assert (r.source_key, r.identity, r.verdict, r.success) == (KEY, WID, "NEW", True)


def test_a_clean_nfl_delivery_reaches_merge():
    verdict = automerge.evaluate(nfl_facts())
    assert verdict.verdict == automerge.MERGE, verdict.render()


def test_the_nfl_gate_waits_for_the_destinations_own_validator():
    verdict = automerge.evaluate(nfl_facts(destination_validator_passed=None))
    assert verdict.verdict == automerge.WAIT


def test_the_nfl_gate_refuses_a_rewrite_of_an_existing_record():
    verdict = automerge.evaluate(nfl_facts(removed_ledger_rows=(f"M {EXISTING}",)))
    assert verdict.verdict == automerge.REFUSE
    assert "THE_LEDGER_DIFF_IS_APPEND_ONLY" in verdict.failed


def test_the_nfl_gate_refuses_a_file_of_any_other_shape():
    verdict = automerge.evaluate(nfl_facts(changed_files=(NEW, "data/recommendations/2026/week_02/r.json")))
    assert verdict.verdict == automerge.REFUSE
    assert "ONLY_CANONICAL_WAGER_FILES_CHANGED" in verdict.failed


def test_the_nfl_gate_refuses_a_row_carrying_a_foreign_batch():
    verdict = automerge.evaluate(nfl_facts(added_ledger_rows=(
        {"imported_wager_id": WID, "source_bet_key": KEY, "import_batch_id": "someone-else"},)))
    assert verdict.verdict == automerge.REFUSE


def test_the_nfl_gate_refuses_a_conflict_the_destination_reported():
    conflict = {"rows": [{"source_bet_key": KEY, "imported_wager_id": WID, "status": "CONFLICT", "success": False,
                          "conflicting_fields": [{"field": "stake"}]}]}
    verdict = automerge.evaluate(nfl_facts(receipts=tuple(r.as_dict() for r in normalise(conflict)),
                                           partial_delivery=True))
    assert verdict.verdict == automerge.REFUSE


def test_an_nfl_settlement_batch_reaches_merge():
    sid = "stl-" + "a" * 24
    path = f"data/wager_settlements/2026/week_02/{sid}.json"
    rec = {"rows": [{"source_bet_key": KEY, "settlement_id": sid, "status": "NEW", "success": True}]}
    rerun = {"rows": [{"source_bet_key": KEY, "settlement_id": sid, "status": "DUPLICATE_NOOP", "success": True}]}
    verdict = automerge.evaluate(nfl_facts(
        head_ref="kalshi-router/settle-NFL", branch_kind=automerge.SETTLEMENTS, changed_files=(path,),
        added_ledger_rows=({"settlement_id": sid, "source_bet_key": KEY},),
        receipts=tuple(r.as_dict() for r in normalise(rec)),
        rerun_receipts=tuple(r.as_dict() for r in normalise(rerun))))
    assert verdict.verdict == automerge.MERGE, verdict.render()


# ------------------------------------------------------------------------------------------ never silent again

def test_a_routable_sport_with_a_row_shape_but_no_destination_is_blocked_not_by_design():
    """The exact week-2 state: NFL classified, NFL row shape present, NFL not in the destination set."""
    from tests.test_production_filter import NOW, order  # the real fixtures

    _w, refusal, _f = evaluate_order(order(ticker="KXNFLGAME-26SEP20CARATL-CAR"), "NFL", "2026-09-20", "settled",
                                     NOW, frozenset({"MLB", "CFB"}))
    assert refusal is ProductionRefusal.DESTINATION_NOT_ACTIVATED
    report = ProductionDiagnostics(orders_after_cutover=1, refused_destination_not_activated=1)
    assert report.health is HealthState.BLOCKED
    assert report.blocked_orders == 1


def test_an_nfl_settlement_amendment_batch_reaches_merge():
    """v2 corrections of v1 settlements arrive as amendment files; the destination answers CORRECTED."""
    sid, aid = "stl-" + "a" * 24, "amd-" + "b" * 24
    path = f"data/wager_settlement_amendments/2026/week_02/{aid}.json"
    rec = {"rows": [{"source_bet_key": KEY, "settlement_id": sid, "status": "CORRECTED", "success": True}]}
    rerun = {"rows": [{"source_bet_key": KEY, "settlement_id": sid, "status": "DUPLICATE_NOOP", "success": True}]}
    verdict = automerge.evaluate(nfl_facts(
        head_ref="kalshi-router/settle-NFL", branch_kind=automerge.SETTLEMENTS, changed_files=(path,),
        # The REAL amendment record shape (nfl-edge-finder settlement_amendments.build_amendment): no
        # settlement_id of its own. The first version of this test gave it one and so passed against a gate
        # that refused every real amendment batch (settle-wagers run 36018002169).
        added_ledger_rows=({"amendment_id": aid, "schema_version": "nfl_wager_settlement_amendment.v1",
                            "amends": sid, "amends_kind": "wager_settlements", "source_bet_key": KEY,
                            "reason_code": "FEE_DOUBLE_COUNT_CORRECTION",
                            "prior_economics_version": "router-settlement-economics.v1",
                            "amended_economics_version": "router-settlement-economics.v2",
                            "superseded_fields": {"net_profit_loss": {"prior": 4.8, "corrected": 4.9}}},),
        receipts=tuple(r.as_dict() for r in normalise(rec)),
        rerun_receipts=tuple(r.as_dict() for r in normalise(rerun))))
    assert verdict.verdict == automerge.MERGE, verdict.render()
