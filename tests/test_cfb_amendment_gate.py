"""The auto-merge gate on a CFB settlement proposal that carries AMENDMENT rows.

CFB moved to economics v2 on 2026-09-28. For a wager already settled under v1,
cfb-edge-finder's importer answers the router's v2 row with an append-only
amendment in `settlement_amendments/<season>.jsonl` and a `CORRECTED` receipt
that names the settlement and the amendment. The gate must land that exactly as
it lands a settlement -- and must refuse the same things: an amendment nobody
receipted, a correction the importer refused, a file outside the ledger, a
removed row, a non-idempotent re-import, a validator that said no.

Every test here changes ONE thing about a proposal that otherwise passes.
"""

from kalshi_router import automerge
from tests.test_automerge_gate import facts

AMENDMENT = {
    "amendment_id": "amd-0123456789abcdef01234567",
    "schema_version": "cfb_settlement_amendment.v1",
    "source_bet_key": "kalshi:v1:k1",
    "amends_settlement_id": "stl-0123456789abcdef01234567",
    "economics_version": "router-settlement-economics.v2",
    "supersedes_economics_version": "router-settlement-economics.v1",
}
CORRECTED_RECEIPT = {
    "source_bet_key": "kalshi:v1:k1", "settlement_id": "stl-0123456789abcdef01234567",
    "amendment_id": "amd-0123456789abcdef01234567", "duplicate_status": "CORRECTED", "success": True,
}
NOOP_RECEIPT = {**CORRECTED_RECEIPT, "duplicate_status": "DUPLICATE_NOOP"}


def cfb_amendment_facts(**overrides):
    base = dict(
        sport="CFB",
        destination_repo="chmoses98/cfb-edge-finder",
        pull_number=57,
        head_ref="kalshi-router/settle-CFB",
        head_repo="chmoses98/cfb-edge-finder",
        base_ref="accounting-data",
        branch_kind=automerge.SETTLEMENTS,
        changed_files=("settlement_amendments/2026.jsonl",),
        added_ledger_rows=(dict(AMENDMENT),),
        removed_ledger_rows=(),
        receipts=(dict(CORRECTED_RECEIPT),),
        rerun_receipts=(dict(NOOP_RECEIPT),),
        rerun_changed_the_tree=False,
        # accounting-data runs no CI; the destination's own validator answers.
        check_runs=(),
        destination_validator_passed=True,
        partial_delivery=False,
    )
    base.update(overrides)
    return facts(**base)


def test_a_receipted_corrected_amendment_reaches_merge():
    verdict = automerge.evaluate(cfb_amendment_facts())
    assert verdict.verdict == automerge.MERGE, verdict.reasons
    assert "EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY" in verdict.passed
    assert "THE_IMPORT_IS_IDEMPOTENT" in verdict.passed


def test_a_settlement_and_an_amendment_in_one_proposal_both_land():
    settlement_row = {"settlement_id": "stl-fedcba9876543210fedcba98", "source_bet_key": "kalshi:v1:k2"}
    verdict = automerge.evaluate(cfb_amendment_facts(
        changed_files=("settlements/2026.jsonl", "settlement_amendments/2026.jsonl"),
        added_ledger_rows=(dict(AMENDMENT), settlement_row),
        receipts=(dict(CORRECTED_RECEIPT),
                  {"source_bet_key": "kalshi:v1:k2", "settlement_id": settlement_row["settlement_id"],
                   "duplicate_status": "NEW", "success": True}),
        rerun_receipts=(dict(NOOP_RECEIPT),
                        {"source_bet_key": "kalshi:v1:k2", "settlement_id": settlement_row["settlement_id"],
                         "duplicate_status": "DUPLICATE_NOOP", "success": True}),
    ))
    assert verdict.verdict == automerge.MERGE, verdict.reasons


def test_an_amendment_nobody_receipted_refuses():
    smuggled = {**AMENDMENT, "amendment_id": "amd-ffffffffffffffffffffffff", "source_bet_key": "kalshi:v1:k9"}
    verdict = automerge.evaluate(cfb_amendment_facts(added_ledger_rows=(dict(AMENDMENT), smuggled)))
    assert verdict.verdict == automerge.REFUSE
    assert "EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY" in verdict.failed


def test_an_amendment_row_with_no_identity_refuses():
    verdict = automerge.evaluate(cfb_amendment_facts(
        added_ledger_rows=({"source_bet_key": "kalshi:v1:k1", "economics_version": "router-settlement-economics.v2"},),
    ))
    assert verdict.verdict == automerge.REFUSE
    assert "EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY" in verdict.failed


def test_a_correction_the_importer_refused_refuses_the_merge():
    """Two derivations disagreeing about the money is exactly the case for a person."""
    verdict = automerge.evaluate(cfb_amendment_facts(
        added_ledger_rows=(),
        changed_files=(),
        receipts=({**CORRECTED_RECEIPT, "duplicate_status": "REFUSED", "success": False,
                   "amendment_id": None, "settlement_id": None,
                   "reason": "a DIFFERENT correction is already filed"},),
        rerun_receipts=({**CORRECTED_RECEIPT, "duplicate_status": "REFUSED", "success": False},),
        partial_delivery=True,
    ))
    assert verdict.verdict == automerge.REFUSE
    assert "IMPORTER_REFUSED_NOTHING" in verdict.failed


def test_a_file_beside_the_amendment_ledger_refuses():
    verdict = automerge.evaluate(cfb_amendment_facts(
        changed_files=("settlement_amendments/2026.jsonl", "settlement_amendments/README.md"),
    ))
    assert verdict.verdict == automerge.REFUSE
    assert "ONLY_CANONICAL_WAGER_FILES_CHANGED" in verdict.failed


def test_a_rewritten_settlement_row_beside_a_correct_amendment_refuses():
    """The amendment is the ONLY admissible way to change a settlement's money."""
    verdict = automerge.evaluate(cfb_amendment_facts(
        changed_files=("settlements/2026.jsonl", "settlement_amendments/2026.jsonl"),
        removed_ledger_rows=('{"settlement_id": "stl-0123456789abcdef01234567", "net_profit_loss": 29.9495}',),
    ))
    assert verdict.verdict == automerge.REFUSE
    assert "THE_LEDGER_DIFF_IS_APPEND_ONLY" in verdict.failed


def test_a_correction_that_writes_again_on_re_import_refuses():
    verdict = automerge.evaluate(cfb_amendment_facts(rerun_receipts=(dict(CORRECTED_RECEIPT),)))
    assert verdict.verdict == automerge.REFUSE
    assert "THE_IMPORT_IS_IDEMPOTENT" in verdict.failed


def test_a_re_import_that_changes_the_tree_refuses():
    verdict = automerge.evaluate(cfb_amendment_facts(rerun_changed_the_tree=True))
    assert verdict.verdict == automerge.REFUSE
    assert "THE_IMPORT_IS_IDEMPOTENT" in verdict.failed


def test_the_destinations_validator_saying_no_refuses():
    verdict = automerge.evaluate(cfb_amendment_facts(destination_validator_passed=False))
    assert verdict.verdict == automerge.REFUSE


def test_a_wager_shaped_branch_carrying_an_amendment_refuses():
    """An amendment belongs on the settlement branch. On the wager branch the
    row has no import batch id and is foreign."""
    verdict = automerge.evaluate(cfb_amendment_facts(
        head_ref="kalshi-router/CFB", branch_kind=automerge.WAGERS,
    ))
    assert verdict.verdict == automerge.REFUSE


def test_the_gate_prints_no_money_for_an_amendment():
    verdict = automerge.evaluate(cfb_amendment_facts(
        added_ledger_rows=({**AMENDMENT, "net_profit_loss": 31.0525, "original_net_profit_loss": 29.9495},),
    ))
    text = " ".join(verdict.reasons + verdict.passed + verdict.failed)
    assert "31.0525" not in text and "29.9495" not in text
