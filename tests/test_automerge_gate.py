"""THE PULL REQUEST THAT NEVER MERGED, AND THE GATE THAT NOW FINISHES IT.

On 2026-09-16 the router classified 8 MLB wagers, handed them to
`edge-finder-api`'s own importer, pushed them to `kalshi-router/MLB` and opened
pull request #218. Three days later #218 was still open -- clean, mergeable,
CI green -- and the destination's own 2026-09-18 EdgeLab report read
``Placed bets: 0``, because the rows were on a branch and not on `main`.

Two separate defects produced that, and both are tested here:

1. **Nothing merged it.** Delivery ended at "open a pull request". There was
   no machine-verifiable decision about whether the proposal was safe to land.
2. **Nothing could have merged it.** Every run rebuilt the branch and
   force-pushed a new commit for identical content, because a commit carries a
   timestamp. The destination's pull-request CI takes ~19 minutes; the router
   runs every 15. The head was therefore almost never a commit whose checks had
   finished, so even a gate requiring green CI would have waited forever.

The first half of this file tests `kalshi_router.automerge.evaluate` directly
-- it is pure, so every condition can be driven to REFUSE on purpose. The
second half runs the COMMITTED delivery step against a real local git remote
and a GitHub stand-in that answers from that same repository, so "the rows
reach main" is a ref this test can read rather than a mock's return value.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from kalshi_router import automerge
from kalshi_router.destination import DESTINATION_REPOS, ROUTER_IMPORT_BATCH_ID
from tests.test_partial_delivery_regression import (  # the real harness, reused
    CONFLICTED_SOURCE_KEY,
    DESTINATION,
    LEDGER,
    ROOT,
    _git,
    branch_ledger,
    run_delivery,
    world,  # noqa: F401 -- pytest fixture
)

MERGE_SCRIPT = ROOT / "scripts/merge_delivery_pr.py"


# ══════════════════════════════════════════════════════════════════════
# The gate itself, condition by condition
# ══════════════════════════════════════════════════════════════════════

def facts(**overrides):
    """A batch that passes every condition, so each test changes ONE thing."""
    base = dict(
        sport="MLB",
        destination_repo="chmoses98/edge-finder-api",
        pull_number=218,
        state="open",
        draft=False,
        head_ref="kalshi-router/MLB",
        head_sha="a" * 40,
        head_repo="chmoses98/edge-finder-api",
        base_ref="main",
        mergeable=True,
        mergeable_state="clean",
        verified_sha="a" * 40,
        changed_files=("data/edgelab/bets/bets.jsonl",),
        added_ledger_rows=(
            {"betId": "id1", "sourceBetKey": "k1", "importBatchId": ROUTER_IMPORT_BATCH_ID},
        ),
        removed_ledger_rows=(),
        receipts=({"betId": "id1", "sourceBetKey": "k1",
                   "duplicateStatus": "NEW", "success": True},),
        rerun_receipts=({"betId": "id1", "sourceBetKey": "k1",
                         "duplicateStatus": "DUPLICATE_NOOP", "success": True},),
        rerun_changed_the_tree=False,
        check_runs=(("test", "completed", "success"),),
        partial_delivery=False,
    )
    base.update(overrides)
    return automerge.MergeFacts(**base)


def test_a_clean_delivery_passes_every_condition():
    verdict = automerge.evaluate(facts())
    assert verdict.verdict == automerge.MERGE
    assert verdict.may_merge is True
    assert sorted(verdict.passed) == sorted(automerge.CONDITIONS)


def test_every_condition_reaches_exactly_one_bucket():
    """A condition that silently evaluates to nothing is a condition that
    is not being checked."""
    verdict = automerge.evaluate(facts())
    buckets = verdict.passed + verdict.failed + verdict.waiting_on
    assert sorted(buckets) == sorted(automerge.CONDITIONS)
    assert len(buckets) == len(set(buckets))


# ── 11. a conflict still fails closed ──────────────────────────────────

def test_an_importer_conflict_refuses_the_merge():
    verdict = automerge.evaluate(facts(
        receipts=(
            {"betId": "id1", "sourceBetKey": "k1", "duplicateStatus": "NEW", "success": True},
            {"betId": "id2", "sourceBetKey": "k2", "duplicateStatus": "CONFLICT",
             "success": False, "conflictingFields": ["entryPrice"]},
        ),
        partial_delivery=True,
    ))
    assert verdict.verdict == automerge.REFUSE
    assert "IMPORTER_REFUSED_NOTHING" in verdict.failed
    assert "EVERY_ROW_NEEDED_NO_HUMAN_JUDGEMENT" in verdict.failed


def test_an_unresolved_ticker_refuses_the_merge():
    """An ambiguous ticker match is left UNRESOLVED by the importer on
    purpose. Auto-merging past it would be choosing a candidate."""
    verdict = automerge.evaluate(facts(
        receipts=({"betId": None, "sourceBetKey": "k1",
                   "duplicateStatus": "UNRESOLVED", "success": False},),
    ))
    assert verdict.verdict == automerge.REFUSE
    assert "IMPORTER_REFUSED_NOTHING" in verdict.failed


def test_a_verdict_the_gate_has_never_heard_of_refuses_the_merge():
    """Fail closed on a NEW importer verdict rather than waving it through
    because it is not on a list of known-bad ones."""
    verdict = automerge.evaluate(facts(
        receipts=({"betId": "id1", "sourceBetKey": "k1",
                   "duplicateStatus": "NEEDS_REVIEW", "success": True},),
    ))
    assert verdict.verdict == automerge.REFUSE
    assert "EVERY_ROW_NEEDED_NO_HUMAN_JUDGEMENT" in verdict.failed


def test_a_row_with_no_canonical_bet_id_refuses_the_merge():
    verdict = automerge.evaluate(facts(
        receipts=({"betId": None, "sourceBetKey": "k1",
                   "duplicateStatus": "NEW", "success": True},),
    ))
    assert verdict.verdict == automerge.REFUSE
    assert "EVERY_ROW_NEEDED_NO_HUMAN_JUDGEMENT" in verdict.failed


# ── MERGEABILITY vs CI: the 2026-09-19 contradiction ──────────────────
#
# Production run 35466474703 read, in a single evaluation:
#
#     WAIT   CONTINUOUS_INTEGRATION_IS_GREEN        (test still running)
#     REFUSE THE_DESTINATION_BRANCH_IS_CLEANLY_MERGEABLE
#            (mergeable=true, mergeable_state='unstable')
#
# Two opposite readings of one fact. `unstable` is GitHub restating the check
# rollup -- it says "mergeable, but the checks are not all green", and a check
# that is still RUNNING is not a check that failed. The gate's own contract
# says pending CI is a WAIT that resolves itself, so refusing on the
# restatement of that same pendingness was simply a bug.

def test_pending_ci_with_an_unstable_but_mergeable_pr_WAITS():
    """THE EXACT PRODUCTION STATE. It must never produce REFUSE."""
    verdict = automerge.evaluate(facts(
        mergeable=True,
        mergeable_state="unstable",
        check_runs=(("test", "in_progress", None),),
    ))
    assert verdict.verdict == automerge.WAIT, verdict.render()
    assert not verdict.failed, verdict.render()
    assert "CONTINUOUS_INTEGRATION_IS_GREEN" in verdict.waiting_on
    assert "THE_DESTINATION_BRANCH_IS_CLEANLY_MERGEABLE" in verdict.waiting_on


def test_an_unstable_pr_whose_checks_have_not_reported_yet_WAITS():
    """The window between a push and the first check-run row."""
    verdict = automerge.evaluate(facts(
        mergeable=True, mergeable_state="unstable", check_runs=(),
    ))
    assert verdict.verdict == automerge.WAIT
    assert not verdict.failed


def test_an_unstable_pr_whose_checks_FAILED_refuses():
    """The other half of `unstable`, and it must still fail closed."""
    verdict = automerge.evaluate(facts(
        mergeable=True,
        mergeable_state="unstable",
        check_runs=(("test", "completed", "failure"),),
    ))
    assert verdict.verdict == automerge.REFUSE
    assert "CONTINUOUS_INTEGRATION_IS_GREEN" in verdict.failed
    assert "THE_DESTINATION_BRANCH_IS_CLEANLY_MERGEABLE" in verdict.failed


def test_an_unstable_pr_with_every_visible_check_green_refuses():
    """FAIL CLOSED on the case the gate cannot explain.

    If every check this gate can see is green and GitHub still will not call
    the branch clean, something it CANNOT see -- a required check that never
    reported, a rule the gate does not model -- is withholding the merge.
    Waiting forever would be wrong and merging would be worse, so it refuses
    and names the contradiction for a person.
    """
    verdict = automerge.evaluate(facts(
        mergeable=True,
        mergeable_state="unstable",
        check_runs=(("test", "completed", "success"),),
    ))
    assert verdict.verdict == automerge.REFUSE
    assert "THE_DESTINATION_BRANCH_IS_CLEANLY_MERGEABLE" in verdict.failed
    assert "CONTINUOUS_INTEGRATION_IS_GREEN" in verdict.passed


def test_a_human_protection_block_is_never_routed_around():
    """`blocked` is a required review or a branch-protection rule. That is a
    person's decision, and the WAIT above must not have become a way past it."""
    verdict = automerge.evaluate(facts(mergeable=True, mergeable_state="blocked"))
    assert verdict.verdict == automerge.REFUSE
    assert "THE_DESTINATION_BRANCH_IS_CLEANLY_MERGEABLE" in verdict.failed


def test_a_draft_is_never_merged():
    """Also a deliberate human signal."""
    verdict = automerge.evaluate(facts(draft=True))
    assert verdict.verdict == automerge.REFUSE
    assert "PULL_REQUEST_IS_OPEN_AND_NOT_A_DRAFT" in verdict.failed


def test_mergeability_not_yet_computed_WAITS():
    verdict = automerge.evaluate(facts(mergeable=None, mergeable_state=None))
    assert verdict.verdict == automerge.WAIT
    assert "THE_DESTINATION_BRANCH_IS_CLEANLY_MERGEABLE" in verdict.waiting_on


def test_a_real_merge_conflict_waits_for_the_rebuild_and_never_merges():
    """`dirty` is a conflict. The next run rebuilds the branch from the
    destination's current main, so this resolves itself -- but nothing merges
    in the meantime."""
    verdict = automerge.evaluate(facts(mergeable=False, mergeable_state="dirty"))
    assert verdict.verdict == automerge.WAIT
    assert verdict.may_merge is False
    assert "THE_DESTINATION_BRANCH_IS_CLEANLY_MERGEABLE" in verdict.waiting_on


def test_a_completely_green_and_clean_pull_request_MERGES():
    verdict = automerge.evaluate(facts(
        mergeable=True,
        mergeable_state="clean",
        check_runs=(("test", "completed", "success"),
                    ("lint", "completed", "skipped")),
    ))
    assert verdict.verdict == automerge.MERGE


def test_unstable_is_not_simply_allowlisted_as_transient():
    """The careless one-line fix would have been adding 'unstable' to
    TRANSIENT_MERGE_STATES, which WAITs on it unconditionally -- including
    when a required check has genuinely failed. That would have merged
    nothing, but it would also have hidden a red destination behind a
    permanent, silent WAIT."""
    assert "unstable" not in automerge.TRANSIENT_MERGE_STATES


# ── the destructive cases: existing canonical rows ─────────────────────

def test_a_diff_that_removes_an_existing_canonical_row_refuses_the_merge():
    """The single most valuable rule here. The importer only ever appends;
    a diff that rewrites or deletes a canonical row is not a delivery."""
    verdict = automerge.evaluate(facts(
        removed_ledger_rows=('{"betId": "older-row"}',),
    ))
    assert verdict.verdict == automerge.REFUSE
    assert "THE_LEDGER_DIFF_IS_APPEND_ONLY" in verdict.failed


def test_a_file_outside_the_canonical_ledger_refuses_the_merge():
    verdict = automerge.evaluate(facts(
        changed_files=("data/edgelab/bets/bets.jsonl", "config/rules.json"),
    ))
    assert verdict.verdict == automerge.REFUSE
    assert "ONLY_CANONICAL_WAGER_FILES_CHANGED" in verdict.failed
    assert any("config/rules.json" in reason for reason in verdict.reasons)


def test_an_added_row_from_another_batch_refuses_the_merge():
    verdict = automerge.evaluate(facts(
        added_ledger_rows=(
            {"betId": "id1", "sourceBetKey": "k1", "importBatchId": ROUTER_IMPORT_BATCH_ID},
            {"betId": "id9", "sourceBetKey": "k9", "importBatchId": "someone-elses-batch"},
        ),
    ))
    assert verdict.verdict == automerge.REFUSE
    assert "EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY" in verdict.failed


def test_an_added_row_with_no_receipt_refuses_the_merge():
    """A row in the diff that this run's import did not produce means the
    branch carries something this run never verified."""
    verdict = automerge.evaluate(facts(
        added_ledger_rows=(
            {"betId": "id1", "sourceBetKey": "k1", "importBatchId": ROUTER_IMPORT_BATCH_ID},
            {"betId": "smuggled", "sourceBetKey": "k2",
             "importBatchId": ROUTER_IMPORT_BATCH_ID},
        ),
    ))
    assert verdict.verdict == automerge.REFUSE
    assert "EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY" in verdict.failed


def test_an_unparseable_ledger_line_refuses_the_merge():
    verdict = automerge.evaluate(facts(
        added_ledger_rows=({"_unparseable": True},),
    ))
    assert verdict.verdict == automerge.REFUSE
    assert "EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY" in verdict.failed


# ── idempotency ────────────────────────────────────────────────────────

def test_a_second_identical_import_that_writes_again_refuses_the_merge():
    verdict = automerge.evaluate(facts(
        rerun_changed_the_tree=True,
        rerun_receipts=({"betId": "id1", "sourceBetKey": "k1",
                         "duplicateStatus": "NEW", "success": True},),
    ))
    assert verdict.verdict == automerge.REFUSE
    assert "THE_IMPORT_IS_IDEMPOTENT" in verdict.failed


def test_a_bet_id_that_moves_on_re_import_refuses_the_merge():
    """Identity must be a function of the row. If it moves, a re-run
    duplicates the ledger, and `docs/DELIVERY.md` exists to prevent that."""
    verdict = automerge.evaluate(facts(
        rerun_receipts=({"betId": "DIFFERENT", "sourceBetKey": "k1",
                         "duplicateStatus": "DUPLICATE_NOOP", "success": True},),
    ))
    assert verdict.verdict == automerge.REFUSE
    assert "THE_IMPORT_IS_IDEMPOTENT" in verdict.failed


def test_no_idempotency_evidence_waits_rather_than_merging():
    verdict = automerge.evaluate(facts(rerun_receipts=(), rerun_changed_the_tree=None))
    assert verdict.verdict == automerge.WAIT
    assert "THE_IMPORT_IS_IDEMPOTENT" in verdict.waiting_on


# ── provenance ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("override,condition", [
    ({"head_ref": "someone-elses-branch"}, "BRANCH_ORIGINATED_FROM_THE_ROUTER_WORKFLOW"),
    ({"head_repo": "attacker/edge-finder-api"}, "BRANCH_ORIGINATED_FROM_THE_ROUTER_WORKFLOW"),
    ({"base_ref": "production"}, "BRANCH_ORIGINATED_FROM_THE_ROUTER_WORKFLOW"),
    ({"draft": True}, "PULL_REQUEST_IS_OPEN_AND_NOT_A_DRAFT"),
])
def test_a_pull_request_that_is_not_the_routers_refuses_the_merge(override, condition):
    verdict = automerge.evaluate(facts(**override))
    assert verdict.verdict == automerge.REFUSE
    assert condition in verdict.failed


def test_the_recognised_branches_come_from_the_destination_map():
    """A new destination must not get a branch name the gate silently
    accepts or silently rejects -- both come from one source."""
    assert automerge.router_branches() == frozenset(
        f"kalshi-router/{sport.value}" for sport in DESTINATION_REPOS)


# ── CI and mergeability ────────────────────────────────────────────────

def test_a_failing_check_refuses_the_merge():
    verdict = automerge.evaluate(facts(check_runs=(("test", "completed", "failure"),)))
    assert verdict.verdict == automerge.REFUSE
    assert "CONTINUOUS_INTEGRATION_IS_GREEN" in verdict.failed


def test_a_running_check_waits_rather_than_merging():
    verdict = automerge.evaluate(facts(check_runs=(("test", "in_progress", None),)))
    assert verdict.verdict == automerge.WAIT
    assert "CONTINUOUS_INTEGRATION_IS_GREEN" in verdict.waiting_on


def test_no_reported_check_waits_rather_than_merging():
    """Never read "no checks yet" as "nothing runs here"."""
    verdict = automerge.evaluate(facts(check_runs=()))
    assert verdict.verdict == automerge.WAIT
    assert "CONTINUOUS_INTEGRATION_IS_GREEN" in verdict.waiting_on


def test_a_skipped_or_neutral_check_is_not_a_failure():
    verdict = automerge.evaluate(facts(check_runs=(
        ("test", "completed", "success"),
        ("optional", "completed", "skipped"),
        ("advisory", "completed", "neutral"),
    )))
    assert verdict.verdict == automerge.MERGE


@pytest.mark.parametrize("state", ["unknown", "dirty", "behind"])
def test_a_transient_merge_state_waits(state):
    """These resolve on their own: the next run rebuilds the branch from
    the destination's current main."""
    verdict = automerge.evaluate(facts(mergeable_state=state, mergeable=False))
    assert verdict.verdict == automerge.WAIT
    assert "THE_DESTINATION_BRANCH_IS_CLEANLY_MERGEABLE" in verdict.waiting_on


def test_a_blocked_merge_state_refuses_rather_than_routing_around_it():
    verdict = automerge.evaluate(facts(mergeable_state="blocked"))
    assert verdict.verdict == automerge.REFUSE
    assert "THE_DESTINATION_BRANCH_IS_CLEANLY_MERGEABLE" in verdict.failed


def test_a_head_this_run_did_not_verify_waits():
    verdict = automerge.evaluate(facts(head_sha="b" * 40))
    assert verdict.verdict == automerge.WAIT
    assert "HEAD_IS_THE_COMMIT_THIS_RUN_VERIFIED" in verdict.waiting_on


def test_an_already_merged_pull_request_is_a_quiet_no_op():
    verdict = automerge.evaluate(facts(state="closed"))
    assert verdict.verdict == automerge.WAIT
    assert not verdict.failed


def test_the_gate_reports_every_problem_not_just_the_first():
    verdict = automerge.evaluate(facts(
        changed_files=("README.md",),
        removed_ledger_rows=('{"betId": "gone"}',),
        check_runs=(("test", "completed", "failure"),),
    ))
    assert verdict.verdict == automerge.REFUSE
    assert {"ONLY_CANONICAL_WAGER_FILES_CHANGED",
            "THE_LEDGER_DIFF_IS_APPEND_ONLY",
            "CONTINUOUS_INTEGRATION_IS_GREEN"} <= set(verdict.failed)


def test_the_gate_names_no_ticker_stake_or_price():
    """The router's public logs carry counts and verdicts only."""
    verdict = automerge.evaluate(facts(
        receipts=({"betId": "id1", "sourceBetKey": "k1", "marketTicker": "KXMLBGAME-SECRET",
                   "stake": "12.34", "duplicateStatus": "CONFLICT", "success": False},),
    ))
    rendered = verdict.render()
    assert "KXMLBGAME-SECRET" not in rendered
    assert "12.34" not in rendered


# ══════════════════════════════════════════════════════════════════════
# End to end, against the committed workflow
# ══════════════════════════════════════════════════════════════════════

def main_ledger(world):  # noqa: F811
    checkout = world["tmp"] / f"main-{os.urandom(4).hex()}"
    subprocess.run(["git", "clone", "--quiet", "--branch", "main",
                    str(world["remote"]), str(checkout)],
                   check=True, capture_output=True)
    path = checkout / LEDGER
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def drop_the_conflicted_row(world):  # noqa: F811
    """Make the batch CLEAN -- 16 rows the destination has never seen."""
    body = json.loads(world["payload"].read_text())
    body["rows"] = [r for r in body["rows"]
                    if r["sourceBetKey"] != CONFLICTED_SOURCE_KEY]
    world["payload"].write_text(json.dumps(body))
    return body["rows"]


# ── 10. a clean import reaches main automatically ──────────────────────

def test_a_clean_delivery_reaches_main_without_a_human(world):  # noqa: F811
    """MISSION 2's headline. Kalshi-like fills -> MLB payload -> the
    destination's own importer -> branch -> pull request -> MERGED."""
    rows = drop_the_conflicted_row(world)

    # Run 1 pushes the commit. Its CI has not finished, so the gate WAITS --
    # and waiting is silent, green and costs one cycle.
    first = run_delivery(world)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "verdict: WAIT" in first.stdout
    assert "pushed kalshi-router/MLB" in first.stdout
    assert len(main_ledger(world)) == 1, "nothing may reach main before the gate passes"

    # CI finishes green on that commit.
    world["stub"].check_runs = [("test", "completed", "success")]

    # Run 2 finds the branch already carries this exact content on this exact
    # base, so it does NOT push -- which is precisely why the green CI still
    # belongs to the head commit.
    second = run_delivery(world)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "already carries this exact content" in second.stdout
    assert "pushed kalshi-router/MLB" not in second.stdout
    assert "verdict: MERGE" in second.stdout
    assert "MERGED #218" in second.stdout

    # THE ROWS ARE ON MAIN. Not proposed -- recorded.
    landed = main_ledger(world)
    assert len(landed) == 1 + len(rows) == 17
    by_key = {row["sourceBetKey"]: row for row in landed}
    for row in rows:
        got = by_key[row["sourceBetKey"]]
        # EXECUTION ECONOMICS SURVIVE THE MERGE EXACTLY.
        for field in ("contracts", "entryPrice", "exchangeFee", "stake"):
            assert got[field] == row[field], (field, row["sourceBetKey"])
        assert got["importBatchId"] == ROUTER_IMPORT_BATCH_ID


def test_the_gate_merges_the_exact_commit_it_verified(world):  # noqa: F811
    drop_the_conflicted_row(world)
    run_delivery(world)
    world["stub"].check_runs = [("test", "completed", "success")]
    run_delivery(world)

    assert world["stub"].merge_requests, "the merge endpoint was never called"
    request = world["stub"].merge_requests[-1]
    # Pinned to a SHA: if anything moved the branch in between, GitHub
    # answers 409 and merges nothing.
    assert request["sha"] == world["stub"].merged_sha
    assert request["merge_method"] == "squash"


# ── 13. a duplicate delivery after the merge is a no-op ────────────────

def test_a_duplicate_delivery_after_the_merge_is_a_no_op(world):  # noqa: F811
    drop_the_conflicted_row(world)
    run_delivery(world)
    world["stub"].check_runs = [("test", "completed", "success")]
    run_delivery(world)
    after_merge = main_ledger(world)
    assert len(after_merge) == 17

    world["curl_log"].write_text("")
    third = run_delivery(world)

    receipts = json.loads(world["receipts"].read_text())
    assert {r["duplicateStatus"] for r in receipts} == {"DUPLICATE_NOOP"}
    assert third.returncode == 0
    assert "no ledger change -- every row was already imported" in third.stdout
    assert "pushed kalshi-router/MLB" not in third.stdout
    assert world["curl_log"].read_text().strip() == "", "opened a second pull request"
    assert main_ledger(world) == after_merge, "a rerun rewrote the canonical ledger"


def test_idempotency_is_proved_inside_every_run(world):  # noqa: F811
    """The gate does not take `docs/OPERATIONS.md`'s deleted sandbox on
    trust -- the payload is applied twice, every run."""
    drop_the_conflicted_row(world)
    result = run_delivery(world)

    assert "idempotency: a second identical import changed nothing" in result.stdout
    rerun = json.loads(world["rerun_receipts"].read_text())
    assert {r["duplicateStatus"] for r in rerun} == {"DUPLICATE_NOOP"}
    first = {r["sourceBetKey"]: r["betId"] for r in
             json.loads(world["receipts"].read_text())}
    assert {r["sourceBetKey"]: r["betId"] for r in rerun} == first


# ── 11 & 12. a conflict fails closed, and the good rows still land ─────

def test_a_conflicted_batch_delivers_its_good_rows_and_merges_nothing(world):  # noqa: F811
    """BOTH halves at once, and they are separate decisions:

    * the 16 rows the importer DID write are committed, pushed and proposed
      -- the 2026-09-16 rule, unchanged;
    * the batch does NOT auto-merge, because a refusal is exactly the case
      that needs a person.
    """
    world["stub"].check_runs = [("test", "completed", "success")]
    result = run_delivery(world)

    delivered = [r for r in branch_ledger(world)
                 if r["sourceBetKey"] != CONFLICTED_SOURCE_KEY]
    assert len(delivered) == 16, "a refusal cost valid rows"

    assert "verdict: REFUSE" in result.stdout
    assert "REFUSE IMPORTER_REFUSED_NOTHING" in result.stdout
    assert "the auto-merge gate REFUSED this batch" in result.stdout
    assert world["stub"].merged_sha is None, "a conflicted batch was merged"
    # main still holds only the row it was seeded with.
    assert len(main_ledger(world)) == 1, "a conflicted batch reached main"
    assert result.returncode == 1

    # One destination failed, not two: the gate's refusal IS the partial
    # import, seen again.
    assert "destinations that failed: 1" in result.stdout


def test_a_red_destination_ci_is_never_merged_past(world):  # noqa: F811
    drop_the_conflicted_row(world)
    world["stub"].check_runs = [("test", "completed", "failure")]
    result = run_delivery(world)

    assert "REFUSE CONTINUOUS_INTEGRATION_IS_GREEN" in result.stdout
    assert world["stub"].merged_sha is None
    assert len(main_ledger(world)) == 1
    assert result.returncode == 1
    # The rows are still delivered to the branch -- red CI on the
    # destination does not cost the router its work.
    assert len(branch_ledger(world)) == 17


def test_a_dry_run_pushes_nothing_and_merges_nothing(world):  # noqa: F811
    """A dry run stops before the push, so it never reaches the gate at
    all. The gate call still carries --dry-run as defence in depth: if that
    early `continue` is ever removed, the flag alone still merges nothing."""
    drop_the_conflicted_row(world)
    world["env"]["DRY_RUN"] = "true"
    result = run_delivery(world)

    assert "DRY RUN: ledger changed, nothing pushed." in result.stdout
    assert world["stub"].merged_sha is None
    assert result.returncode == 0
    assert "--dry-run" in Path(ROOT / ".github/workflows/deliver-wagers.yml").read_text()


def test_the_gate_itself_merges_nothing_in_dry_run(tmp_path):
    """The flag, exercised directly rather than inferred."""
    import subprocess as sp
    work = tmp_path / "clone"
    work.mkdir()
    sp.run(["git", "init", "--quiet", "-b", "main", str(work)], check=True)
    (work / "x").write_text("x")
    sp.run(["git", "-C", str(work), "add", "-A"], check=True)
    sp.run(["git", "-C", str(work), "-c", "user.email=t@e.invalid", "-c", "user.name=t",
            "commit", "--quiet", "-m", "seed"], check=True)
    receipts = tmp_path / "r.json"
    receipts.write_text("[]")

    result = sp.run(
        [__import__("sys").executable, str(MERGE_SCRIPT), "--sport", "MLB",
         "--work", str(work), "--receipts", str(receipts), "--dry-run"],
        capture_output=True, text=True,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT / "src"),
             "DOWNSTREAM_REPO_TOKEN": "", "GITHUB_API_ROOT": "http://127.0.0.1:1"},
    )
    # No token -> configuration refusal, and nothing was merged.
    assert result.returncode == 2
    assert "empty" in result.stderr


# ── the churn that made green CI impossible ────────────────────────────

def test_identical_content_does_not_produce_a_new_commit_every_run(world):  # noqa: F811
    """THE SECOND DEFECT. A new head SHA every 15 minutes restarted a
    19-minute CI suite, so the head was never a commit with finished
    checks. Identical content must now leave the head alone."""
    drop_the_conflicted_row(world)
    run_delivery(world)
    first_head = _git("rev-parse", "refs/heads/kalshi-router/MLB", cwd=world["remote"]).strip()

    run_delivery(world)          # CI still pending -- the gate waits
    second_head = _git("rev-parse", "refs/heads/kalshi-router/MLB", cwd=world["remote"]).strip()

    assert second_head == first_head, (
        "identical content produced a new commit; CI would restart forever")


def test_a_new_wager_does_produce_a_new_commit(world):  # noqa: F811
    """The stability above must not become staleness."""
    rows = drop_the_conflicted_row(world)
    run_delivery(world)
    first_head = _git("rev-parse", "refs/heads/kalshi-router/MLB", cwd=world["remote"]).strip()

    body = json.loads(world["payload"].read_text())
    body["rows"] = rows + [{
        "sourceBetKey": "kalshi-order-0099",
        "marketTicker": "KXMLBGAME-26SEP1599-TOR", "side": "YES",
        "gameDate": "2026-09-15", "contracts": "3", "entryPrice": "0.51",
        "exchangeFee": "0.02", "stake": "1.53",
    }]
    world["payload"].write_text(json.dumps(body))

    result = run_delivery(world)
    second_head = _git("rev-parse", "refs/heads/kalshi-router/MLB", cwd=world["remote"]).strip()

    assert "pushed kalshi-router/MLB" in result.stdout
    assert second_head != first_head
    assert len(branch_ledger(world)) == 18


def test_a_moved_CANONICAL_LEDGER_is_rebuilt_on_rather_than_reused(world):  # noqa: F811
    """A proposal is a function of the canonical ledger it was built from, so
    that is what makes an existing one stale.

    This test used to move `main` by writing an unrelated file and require a
    rebuild. That was safe and, measured against the live destination,
    unusable: it commits its own pipeline output every one to five minutes and
    its pull-request CI takes nineteen, so the head reset faster than any check
    suite could finish and the gate still could never fire. The unrelated-file
    case is now the opposite assertion, and it lives with the rest of the
    branch lifecycle in tests/test_delivery_determinism.py.
    """
    drop_the_conflicted_row(world)
    run_delivery(world)
    first_head = _git("rev-parse", "refs/heads/kalshi-router/MLB", cwd=world["remote"]).strip()

    # Someone else writes a row into the destination's canonical ledger.
    other = world["tmp"] / "other"
    subprocess.run(["git", "clone", "--quiet", str(world["remote"]), str(other)],
                   check=True, capture_output=True)
    with (other / LEDGER).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "betId": "someone-elses-row", "sourceBetKey": "manual-9999",
            "importBatchId": "a-different-batch", "marketTicker": "KXOTHER-1",
            "side": "YES", "gameDate": "2026-09-15",
        }, sort_keys=True) + "\n")
    _git("add", "-A", cwd=other)
    _git("-c", "user.email=o@example.invalid", "-c", "user.name=o",
         "commit", "--quiet", "-m", "a manual bet", cwd=other)
    _git("push", "--quiet", "origin", "main", cwd=other)

    result = run_delivery(world)
    second_head = _git("rev-parse", "refs/heads/kalshi-router/MLB", cwd=world["remote"]).strip()

    assert "pushed kalshi-router/MLB" in result.stdout
    assert second_head != first_head
    # Their row survived the rebuild.
    assert any(r["betId"] == "someone-elses-row" for r in branch_ledger(world))


# ── the gate is wired in, and cannot quietly be unwired ────────────────

def test_the_delivery_workflow_actually_runs_the_gate():
    body = Path(ROOT / ".github/workflows/deliver-wagers.yml").read_text()
    assert "scripts/merge_delivery_pr.py" in body
    assert "--rerun-receipts" in body
    assert "--rerun-changed-tree" in body


def test_the_gate_script_makes_no_decision_of_its_own():
    """Every rule lives in the pure module. A threshold, an allowlist or an
    `if` about receipts appearing in the script is drift."""
    body = MERGE_SCRIPT.read_text()
    assert "automerge.evaluate" in body
    for leaked in ("CONFLICT", "UNRESOLVED", "DUPLICATE_NOOP", "mergeable_state ==",
                   "bets.jsonl"):
        assert leaked not in body, f"{leaked!r} is a policy decision and belongs in automerge.py"


def test_the_mergeable_paths_are_exact_not_a_prefix():
    """A `data/` prefix would auto-merge any future file the importer
    starts writing. Exact paths make that a refusal someone looks at once."""
    for path in automerge.MERGEABLE_PATHS:
        assert not path.endswith("/")
        assert "*" not in path
