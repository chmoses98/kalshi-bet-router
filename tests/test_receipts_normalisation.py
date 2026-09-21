"""One vocabulary for two destinations' receipts, and what the gate reads.

*** THE FAILURE THIS PREVENTS ***
MLB's importer writes a LIST of camelCase rows. CFB's writes an OBJECT of
counts. A gate that understood only the first would read CFB's object, iterate
its KEYS, produce receipts that are strings, find no verdicts and no
identities, and WAIT forever -- which reads exactly like a delivery that is
merely early.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from kalshi_router import automerge
from kalshi_router.destination import ROUTER_IMPORT_BATCH_ID
from kalshi_router.receipts import (
    IDEMPOTENT_RERUN_VERDICTS,
    NO_JUDGEMENT_VERDICTS,
    conflicting_field_names,
    normalise,
    verdict_counts,
)

ROOT = Path(__file__).resolve().parents[1]
REPORTER = ROOT / "scripts/report_receipts.py"

MLB_RECEIPTS = [
    {
        "sourceBetKey": "kalshi:v1:aaa",
        "betId": "bet-1",
        "duplicateStatus": "NEW",
        "success": True,
    },
    {
        "sourceBetKey": "kalshi:v1:bbb",
        "betId": "bet-2",
        "duplicateStatus": "DUPLICATE_NOOP",
        "success": True,
    },
]

CFB_ROW_RECEIPTS = {
    "importBatchId": ROUTER_IMPORT_BATCH_ID,
    "season": 2026,
    "written": 1,
    "alreadyPresent": 1,
    "refused": 0,
    "refusals": [],
    "keysWritten": ["kalshi:v1:aaa"],
    "rows": [
        {
            "source_bet_key": "kalshi:v1:aaa",
            "wager_id": "routed-aaa",
            "duplicate_status": "NEW",
            "success": True,
        },
        {
            "source_bet_key": "kalshi:v1:bbb",
            "wager_id": "routed-bbb",
            "duplicate_status": "DUPLICATE_NOOP",
            "success": True,
        },
    ],
}

CFB_COUNTS_ONLY = {
    "importBatchId": ROUTER_IMPORT_BATCH_ID,
    "season": 2026,
    "written": 1,
    "alreadyPresent": 1,
    "refused": 1,
    "refusals": [{"row": 4, "reason": "source_bet_key is required"}],
    "keysWritten": ["kalshi:v1:aaa"],
}


# ------------------------------------------------------------- shapes


def test_a_list_of_camelcase_rows_normalises():
    receipts = normalise(MLB_RECEIPTS)
    assert [r.source_key for r in receipts] == ["kalshi:v1:aaa", "kalshi:v1:bbb"]
    assert [r.identity for r in receipts] == ["bet-1", "bet-2"]
    assert all(r.success for r in receipts)
    assert not any(r.needs_judgement for r in receipts)


def test_an_object_of_snake_case_rows_normalises_to_the_same_shape():
    receipts = normalise(CFB_ROW_RECEIPTS)
    assert [r.source_key for r in receipts] == ["kalshi:v1:aaa", "kalshi:v1:bbb"]
    assert [r.identity for r in receipts] == ["routed-aaa", "routed-bbb"]
    assert verdict_counts(receipts) == {"DUPLICATE_NOOP": 1, "NEW": 1}


def test_the_counts_only_shape_is_still_readable():
    """Kept readable so the landing order of two repositories' pull requests
    cannot break delivery: a CFB importer that has not yet learned to emit
    per-row receipts still produces something the gate can reason about."""
    receipts = normalise(CFB_COUNTS_ONLY)
    assert verdict_counts(receipts) == {"DUPLICATE_NOOP": 1, "NEW": 1, "REFUSED": 1}
    assert sum(1 for r in receipts if not r.success) == 1


def test_the_counts_only_shape_reports_unknown_identity_rather_than_inventing_one():
    """The gate's identity condition then fails closed, which is correct for a
    destination that will not name its rows."""
    receipts = normalise(CFB_COUNTS_ONLY)
    assert all(r.identity is None for r in receipts)


@pytest.mark.parametrize("payload", [None, 7, "text", {}, {"rows": "not a list"}])
def test_an_unreadable_payload_normalises_to_nothing(payload):
    """The gate WAITS on empty receipts rather than merging, so an unreadable
    shape costs a cycle instead of a wrong merge."""
    assert normalise(payload) == ()


def test_an_unrecognised_verdict_is_kept_and_needs_judgement():
    """Mapping an unknown word onto the nearest known one is how a refusal
    becomes a merge."""
    receipts = normalise([{"sourceBetKey": "k", "betId": "b", "duplicateStatus": "WEIRD"}])
    assert receipts[0].verdict == "WEIRD"
    assert receipts[0].needs_judgement


def test_a_receipt_with_no_verdict_at_all_is_not_a_success():
    """An unreadable receipt must not read as a written row."""
    receipts = normalise([{"sourceBetKey": "k"}])
    assert not receipts[0].success


def test_conflicting_fields_surface_names_and_never_values():
    receipts = normalise([
        {
            "sourceBetKey": "k",
            "betId": "b",
            "duplicateStatus": "CONFLICT",
            "success": False,
            "conflictingFields": [{"field": "stake", "existing": 5.37, "incoming": 1.23}],
        }
    ])
    assert conflicting_field_names(receipts) == ("stake",)


def test_the_verdict_vocabularies_are_shared_not_duplicated():
    assert NO_JUDGEMENT_VERDICTS == {"NEW", "DUPLICATE_NOOP", "CORRECTED"}
    assert IDEMPOTENT_RERUN_VERDICTS == {"DUPLICATE_NOOP"}


# --------------------------------------------------- through the gate


def facts(**overrides):
    base = dict(
        sport="CFB",
        destination_repo="chmoses98/cfb-edge-finder",
        pull_number=1,
        state="open",
        draft=False,
        head_ref="kalshi-router/CFB",
        head_sha="abc",
        head_repo="chmoses98/cfb-edge-finder",
        base_ref="accounting-data",
        mergeable=True,
        mergeable_state="clean",
        verified_sha="abc",
        changed_files=("wagers/2026.jsonl",),
        added_ledger_rows=(
            {
                "source_bet_key": "kalshi:v1:aaa",
                "wager_id": "routed-aaa",
                "import_batch_id": ROUTER_IMPORT_BATCH_ID,
            },
        ),
        removed_ledger_rows=(),
        receipts=tuple(
            r.as_dict()
            for r in normalise(
                {
                    "rows": [
                        {
                            "source_bet_key": "kalshi:v1:aaa",
                            "wager_id": "routed-aaa",
                            "duplicate_status": "NEW",
                            "success": True,
                        }
                    ]
                }
            )
        ),
        rerun_receipts=tuple(
            r.as_dict()
            for r in normalise(
                {
                    "rows": [
                        {
                            "source_bet_key": "kalshi:v1:aaa",
                            "wager_id": "routed-aaa",
                            "duplicate_status": "DUPLICATE_NOOP",
                            "success": True,
                        }
                    ]
                }
            )
        ),
        rerun_changed_the_tree=False,
        check_runs=(("CI", "completed", "success"),),
        partial_delivery=False,
        # CFB's ledger branch is an orphan with no `.github/`, so no check run
        # ever reports there and its own validator supplies the verdict.
        destination_validator_passed=True,
    )
    base.update(overrides)
    return automerge.MergeFacts(**base)


def test_a_clean_cfb_delivery_reaches_merge():
    """The whole point of the activation: the gate can now evaluate a CFB
    delivery at all. Before the profiles it refused on the base branch alone --
    `accounting-data` is not `main` -- and would have done so forever."""
    verdict = automerge.evaluate(facts())
    assert verdict.verdict == automerge.MERGE, verdict.render()


def test_the_gate_refuses_a_cfb_pull_request_aimed_at_main():
    verdict = automerge.evaluate(facts(base_ref="main"))
    assert verdict.verdict == automerge.REFUSE
    assert "BRANCH_ORIGINATED_FROM_THE_ROUTER_WORKFLOW" in verdict.failed


def test_the_gate_refuses_a_file_outside_the_destinations_own_ledger():
    verdict = automerge.evaluate(facts(changed_files=("wagers/2026.jsonl", "README.md")))
    assert verdict.verdict == automerge.REFUSE
    assert "ONLY_CANONICAL_WAGER_FILES_CHANGED" in verdict.failed


def test_the_gate_reads_snake_case_row_identity():
    """Reading only MLB's spelling would make every CFB row look foreign and
    unidentified, and refuse a correct delivery on every run forever."""
    verdict = automerge.evaluate(facts())
    assert "EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY" in verdict.passed


def test_the_gate_refuses_a_row_from_another_import_batch():
    verdict = automerge.evaluate(
        facts(
            added_ledger_rows=(
                {
                    "source_bet_key": "kalshi:v1:aaa",
                    "wager_id": "routed-aaa",
                    "import_batch_id": "somebody-elses-batch",
                },
            )
        )
    )
    assert verdict.verdict == automerge.REFUSE
    assert "EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY" in verdict.failed


def test_the_gate_refuses_a_second_import_that_wrote_again():
    """The idempotency proof, re-run every delivery rather than measured once."""
    verdict = automerge.evaluate(
        facts(
            rerun_receipts=tuple(
                r.as_dict()
                for r in normalise(
                    {
                        "rows": [
                            {
                                "source_bet_key": "kalshi:v1:aaa",
                                "wager_id": "routed-aaa",
                                "duplicate_status": "NEW",
                                "success": True,
                            }
                        ]
                    }
                )
            )
        )
    )
    assert verdict.verdict == automerge.REFUSE
    assert "THE_IMPORT_IS_IDEMPOTENT" in verdict.failed


def test_the_gate_refuses_a_second_import_that_changed_the_tree():
    verdict = automerge.evaluate(facts(rerun_changed_the_tree=True))
    assert verdict.verdict == automerge.REFUSE
    assert "THE_IMPORT_IS_IDEMPOTENT" in verdict.failed


def test_the_gate_refuses_a_row_whose_identity_drifted_on_re_import():
    verdict = automerge.evaluate(
        facts(
            rerun_receipts=tuple(
                r.as_dict()
                for r in normalise(
                    {
                        "rows": [
                            {
                                "source_bet_key": "kalshi:v1:aaa",
                                "wager_id": "routed-SOMETHING-ELSE",
                                "duplicate_status": "DUPLICATE_NOOP",
                                "success": True,
                            }
                        ]
                    }
                )
            )
        )
    )
    assert verdict.verdict == automerge.REFUSE
    assert "THE_IMPORT_IS_IDEMPOTENT" in verdict.failed


def test_the_gate_refuses_an_importer_refusal():
    verdict = automerge.evaluate(
        facts(
            receipts=tuple(
                r.as_dict()
                for r in normalise(
                    {
                        "rows": [
                            {
                                "source_bet_key": "kalshi:v1:aaa",
                                "wager_id": "routed-aaa",
                                "duplicate_status": "NEW",
                                "success": True,
                            },
                            {
                                "source_bet_key": "kalshi:v1:ccc",
                                "duplicate_status": "REFUSED",
                                "success": False,
                                "reason": "unknown field",
                            },
                        ]
                    }
                )
            )
        )
    )
    assert verdict.verdict == automerge.REFUSE
    assert "IMPORTER_REFUSED_NOTHING" in verdict.failed


def test_the_gate_waits_on_unreadable_receipts_rather_than_merging():
    verdict = automerge.evaluate(facts(receipts=(), rerun_receipts=()))
    assert verdict.verdict != automerge.MERGE
    assert "IMPORTER_REFUSED_NOTHING" in verdict.waiting_on


def test_a_destination_with_no_ci_waits_until_its_own_validator_reports():
    """A gate that only knew about CI would wait forever for a signal that
    cannot arrive on an orphan data branch. A gate that ignored the absence
    would merge with no verdict at all. It waits for the validator instead."""
    verdict = automerge.evaluate(facts(destination_validator_passed=None))
    assert verdict.verdict == automerge.WAIT
    assert "CONTINUOUS_INTEGRATION_IS_GREEN" in verdict.waiting_on


def test_a_destinations_own_validator_refusing_stops_the_merge():
    verdict = automerge.evaluate(facts(destination_validator_passed=False))
    assert verdict.verdict == automerge.REFUSE
    assert "CONTINUOUS_INTEGRATION_IS_GREEN" in verdict.failed


def test_green_check_runs_do_not_substitute_for_that_validator():
    """The CFB ledger branch runs no CI, so a check run reported against it is
    not the destination's verdict on its data. Merging on one would be merging
    on a signal from somewhere else."""
    verdict = automerge.evaluate(
        facts(
            destination_validator_passed=None,
            check_runs=(("something", "completed", "success"),),
        )
    )
    assert verdict.verdict == automerge.WAIT


def test_a_destination_whose_branch_does_run_ci_still_reads_its_checks():
    from kalshi_router.automerge import ledger_branch_runs_ci

    assert ledger_branch_runs_ci("MLB")
    assert not ledger_branch_runs_ci("CFB")
    # An unknown sport answers TRUE, which makes the gate demand a check run it
    # will never see: the fail-closed direction.
    assert ledger_branch_runs_ci("NFL")


def test_a_row_the_destination_will_not_name_does_not_merge_unread():
    verdict = automerge.evaluate(
        facts(
            receipts=tuple(r.as_dict() for r in normalise(CFB_COUNTS_ONLY)),
            rerun_receipts=tuple(r.as_dict() for r in normalise(CFB_COUNTS_ONLY)),
        )
    )
    assert verdict.verdict == automerge.REFUSE


# ----------------------------------------------------- the log reporter


def report(payload, tmp_path):
    path = tmp_path / "receipts.json"
    path.write_text(json.dumps(payload))
    return subprocess.run(
        [sys.executable, str(REPORTER), "--receipts", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_the_reporter_reads_both_destinations_shapes(tmp_path):
    assert "rows: 2" in report(MLB_RECEIPTS, tmp_path)
    assert "rows: 2" in report(CFB_ROW_RECEIPTS, tmp_path)
    assert "rows: 3" in report(CFB_COUNTS_ONLY, tmp_path)


def test_the_reporter_never_prints_a_source_key(tmp_path):
    for payload in (MLB_RECEIPTS, CFB_ROW_RECEIPTS, CFB_COUNTS_ONLY):
        out = report(payload, tmp_path)
        assert "kalshi:v1" not in out
        assert "aaa" not in out
        assert "bbb" not in out


def test_the_reporter_does_not_crash_on_a_missing_file(tmp_path):
    result = subprocess.run(
        [sys.executable, str(REPORTER), "--receipts", str(tmp_path / "nope.json")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "could not be read" in result.stdout
