"""The deterministic gate that finishes a delivery.

WHY THIS EXISTS
---------------
Delivery used to stop at a pull request. `docs/DELIVERY.md` is explicit that
**a branch is not the ledger** -- and a pull request nobody merges is not the
ledger either. On 2026-09-16 the router classified 8 MLB wagers, handed them to
the destination's own importer, pushed them and opened
``chmoses98/edge-finder-api#218``. Three days later that pull request was still
open, still clean, still mergeable, and the destination's own 2026-09-18 report
said ``Placed bets: 0`` -- because the rows were not on ``main``.

The architecture is right: the router must never write the destination's ledger
file, so the unit of delivery is a payload handed to the destination's importer,
and what comes back has to be proposed rather than forced. What was missing is
the last step -- a MACHINE-VERIFIABLE decision about whether that proposal is
safe to land without a human reading it.

This module is that decision, and only that decision. It is pure: it takes
facts and returns a verdict. It performs no I/O, holds no credential, and
cannot merge anything. `scripts/merge_delivery_pr.py` gathers the facts and
acts on the verdict; separating them is what lets every rule below be tested
directly instead of through a workflow.

THREE VERDICTS, AND THE DIFFERENCE MATTERS
------------------------------------------
``MERGE``  -- every condition is machine-verified. Land it.
``WAIT``   -- a condition is not yet decidable and will decide itself
              (CI still running, mergeability not yet computed by GitHub).
              The next scheduled run is the fix; nobody is paged.
``REFUSE`` -- a condition FAILED. Nothing is merged, the pull request stays
              open with its rows intact, and a human is told exactly which
              condition and why.

Conflating the last two is how a system either merges something it should not
or pages someone every fifteen minutes for a job that was simply not finished
yet. `docs/OPERATIONS.md` already makes that distinction for health states;
this is the same principle applied to merging.

WHAT IS NOT WEAKENED
--------------------
Nothing here touches ambiguous ticker matching, conflict handling, partial
failures, idempotency, the canonical ``sourceBetKey``, the constant
``importBatchId``, the execution economics, or rows the destination already
holds. Every one of those is a REFUSAL condition below: the gate is strictly
*additional* scrutiny applied after the importer has already had its say. A
batch the importer refused in part still delivers the rows it wrote -- that
2026-09-16 rule is preserved exactly -- it simply does not auto-merge, because
a refusal is the one thing that genuinely needs a person.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .destination import DESTINATION_REPOS, ROUTER_IMPORT_BATCH_ID

#: The branch a sport's delivery lives on. One long-lived branch per sport, per
#: `deliver-wagers.yml`. Derived here from the same map the workflow routes by,
#: so a new destination cannot get a branch name this gate does not recognise.
BRANCH_PREFIX = "kalshi-router/"


def router_branch_for(sport: str) -> str:
    return f"{BRANCH_PREFIX}{sport}"


def router_branches() -> frozenset[str]:
    return frozenset(router_branch_for(sport.value) for sport in DESTINATION_REPOS)


#: The ONLY files a router-authored pull request may change.
#:
#: Deliberately exact paths, not a `data/` prefix. The delivery workflow already
#: refuses to COMMIT anything outside `data/`; that is a containment check, and
#: this is a different, narrower question -- what may be merged to `main`
#: without a human. A file the importer starts writing later is not
#: automatically safe to land unread, so it lands here as a refusal and someone
#: looks once, rather than being waved through forever by a prefix match.
MERGEABLE_PATHS = frozenset({
    "data/edgelab/bets/bets.jsonl",
})

#: Importer verdicts that represent a row the destination accepted without
#: needing anyone's judgement. Anything else -- CONFLICT, UNRESOLVED, an
#: ambiguous ticker match, a schema refusal -- is a human's call.
NO_JUDGEMENT_VERDICTS = frozenset({"NEW", "DUPLICATE_NOOP", "CORRECTED"})

#: The verdicts a SECOND, identical import must produce. This is the
#: idempotency proof: the same payload, applied twice to the same tree, must
#: change nothing the second time. A `NEW` here would mean a row's identity is
#: not a function of the row.
IDEMPOTENT_RERUN_VERDICTS = frozenset({"DUPLICATE_NOOP"})

#: Mergeability states that resolve on their own.
#:
#: `unstable` is here because of a real failure, on the first production run
#: of this gate (deliver-wagers #41 and #42, 2026-09-19). Ten conditions
#: passed, CI was correctly WAITing on a check that had been running for
#: seconds -- and this condition REFUSED, turning a perfectly healthy
#: delivery red.
#:
#: GitHub reports `unstable` for "merges cleanly, but a check run is pending,
#: failing, neutral or skipped". Every one of those is ALREADY decided by
#: CONTINUOUS_INTEGRATION_IS_GREEN, which reads the check runs directly:
#: pending waits, failing refuses, neutral and skipped pass. So `unstable`
#: carries no information this gate does not already have, and refusing on it
#: double-counts CI -- guaranteeing that the run which pushes a commit is
#: always red, since CI is by definition still running at that moment.
#:
#: This is not a relaxation. Nothing merges while `mergeable` is false or a
#: check is unfinished; the merge still needs GitHub to agree AND this gate's
#: own CI condition to pass. What it stops is a red job for a state that
#: means "the thing you are already waiting for".
#:
#: `blocked` stays a REFUSAL, and that distinction is the whole point:
#: `blocked` is something OTHER than CI -- a required review, a protection
#: rule -- withholding the merge, and automation must not route around it.
TRANSIENT_MERGE_STATES = frozenset({"unknown", "behind", "dirty", "unstable"})

#: Check-run conclusions that are not a failure. `skipped` and `neutral` are
#: included because a check that declined to run is not a check that failed;
#: `None` (still running) is handled separately, as a WAIT.
PASSING_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})

MERGE = "MERGE"
WAIT = "WAIT"
REFUSE = "REFUSE"

#: Stable identifiers for every condition, so a job log, a test and this module
#: all name the same thing.
CONDITIONS = (
    "PULL_REQUEST_EXISTS",
    "PULL_REQUEST_IS_OPEN_AND_NOT_A_DRAFT",
    "BRANCH_ORIGINATED_FROM_THE_ROUTER_WORKFLOW",
    "HEAD_IS_THE_COMMIT_THIS_RUN_VERIFIED",
    "IMPORTER_REFUSED_NOTHING",
    "EVERY_ROW_NEEDED_NO_HUMAN_JUDGEMENT",
    "THE_IMPORT_IS_IDEMPOTENT",
    "ONLY_CANONICAL_WAGER_FILES_CHANGED",
    "THE_LEDGER_DIFF_IS_APPEND_ONLY",
    "EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY",
    "CONTINUOUS_INTEGRATION_IS_GREEN",
    "THE_DESTINATION_BRANCH_IS_CLEANLY_MERGEABLE",
)


@dataclass(frozen=True)
class MergeFacts:
    """Everything the gate is allowed to reason about.

    Every field is something a machine can establish without interpretation.
    There is deliberately no "looks fine" input and no override flag: a gate
    with an escape hatch is a gate that gets escaped.
    """

    sport: str
    destination_repo: str
    #: The pull request, as the GitHub API currently reports it.
    pull_number: int | None = None
    state: str | None = None
    draft: bool | None = None
    head_ref: str | None = None
    head_sha: str | None = None
    head_repo: str | None = None
    base_ref: str | None = None
    mergeable: bool | None = None
    mergeable_state: str | None = None
    #: The commit this run actually produced and inspected locally. If the
    #: pull request's head is something else, this run verified a different
    #: tree than the one that would merge.
    verified_sha: str | None = None
    #: Paths the pull request changes, relative to the destination root.
    changed_files: tuple[str, ...] = ()
    #: Ledger lines the pull request ADDS and REMOVES, already parsed. The
    #: router only ever appends, so `removed_ledger_rows` must be empty.
    added_ledger_rows: tuple[dict, ...] = ()
    removed_ledger_rows: tuple[str, ...] = ()
    #: This run's own receipts from the destination's importer.
    receipts: tuple[dict, ...] = ()
    #: Receipts from an immediate SECOND import of the identical payload.
    rerun_receipts: tuple[dict, ...] = ()
    #: Whether that second import changed the working tree at all.
    rerun_changed_the_tree: bool | None = None
    #: (name, status, conclusion) per check run on `head_sha`.
    check_runs: tuple[tuple[str, str | None, str | None], ...] = ()
    #: True when the delivery step reported a partial import this run.
    partial_delivery: bool = False


@dataclass
class MergeVerdict:
    verdict: str
    reasons: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    waiting_on: list[str] = field(default_factory=list)

    @property
    def may_merge(self) -> bool:
        return self.verdict == MERGE

    def render(self) -> str:
        lines = [f"  verdict: {self.verdict}"]
        for condition in self.passed:
            lines.append(f"    PASS   {condition}")
        for condition in self.waiting_on:
            lines.append(f"    WAIT   {condition}")
        for condition in self.failed:
            lines.append(f"    REFUSE {condition}")
        for reason in self.reasons:
            lines.append(f"    - {reason}")
        return "\n".join(lines)


def _check_state(check_runs):
    """(pending, failed) names from a set of check runs."""
    pending, failed = [], []
    for name, status, conclusion in check_runs:
        if status != "completed":
            pending.append(name)
        elif conclusion not in PASSING_CONCLUSIONS:
            failed.append(f"{name}={conclusion}")
    return pending, failed


def evaluate(facts: MergeFacts) -> MergeVerdict:
    """Pure. The whole decision, condition by condition.

    Every condition is evaluated -- the function does not short-circuit -- so
    one run's output names *everything* that is not yet right rather than the
    first thing it tripped over. A verdict is MERGE only if nothing failed and
    nothing is pending.
    """
    passed: list[str] = []
    failed: list[str] = []
    waiting: list[str] = []
    reasons: list[str] = []

    def ok(condition):
        passed.append(condition)

    def refuse(condition, why):
        failed.append(condition)
        reasons.append(why)

    def wait(condition, why):
        waiting.append(condition)
        reasons.append(why)

    # ── the pull request itself ──────────────────────────────────────
    if facts.pull_number is None:
        wait("PULL_REQUEST_EXISTS",
             "no pull request is open for this delivery yet; the next run opens it")
    else:
        ok("PULL_REQUEST_EXISTS")

    if facts.state is None:
        wait("PULL_REQUEST_IS_OPEN_AND_NOT_A_DRAFT", "the pull request's state is unknown")
    elif facts.state != "open":
        # Already merged or closed. Nothing to do, and nothing wrong.
        wait("PULL_REQUEST_IS_OPEN_AND_NOT_A_DRAFT",
             f"the pull request is {facts.state}, not open -- nothing to merge")
    elif facts.draft:
        # Someone marked it a draft. That is a deliberate human signal and
        # this gate must not step over it.
        refuse("PULL_REQUEST_IS_OPEN_AND_NOT_A_DRAFT",
               "the pull request is a DRAFT; a person marked it not-ready and "
               "automation must not overrule that")
    else:
        ok("PULL_REQUEST_IS_OPEN_AND_NOT_A_DRAFT")

    # ── provenance: this must be the router's own branch ─────────────
    expected_branch = router_branch_for(facts.sport)
    problems = []
    if facts.head_ref != expected_branch:
        problems.append(f"head is {facts.head_ref!r}, not {expected_branch!r}")
    if facts.head_repo and facts.head_repo != facts.destination_repo:
        problems.append(
            f"head repository is {facts.head_repo!r}, not the destination "
            f"{facts.destination_repo!r} (a fork cannot be auto-merged)")
    if facts.base_ref != "main":
        problems.append(f"base is {facts.base_ref!r}, not 'main'")
    if problems:
        refuse("BRANCH_ORIGINATED_FROM_THE_ROUTER_WORKFLOW",
               "; ".join(problems))
    else:
        ok("BRANCH_ORIGINATED_FROM_THE_ROUTER_WORKFLOW")

    # ── the head that would merge is the head this run inspected ─────
    if not facts.head_sha or not facts.verified_sha:
        wait("HEAD_IS_THE_COMMIT_THIS_RUN_VERIFIED",
             "the pull request's head commit is not known yet")
    elif facts.head_sha != facts.verified_sha:
        # Something moved the branch between this run's push and this read.
        # Not necessarily wrong -- a concurrent run -- but this run verified a
        # different tree, so it has no standing to merge this one.
        wait("HEAD_IS_THE_COMMIT_THIS_RUN_VERIFIED",
             f"the pull request head ({facts.head_sha[:12]}) is not the commit this "
             f"run verified ({facts.verified_sha[:12]}); the next run re-verifies it")
    else:
        ok("HEAD_IS_THE_COMMIT_THIS_RUN_VERIFIED")

    # ── the importer's own verdicts ──────────────────────────────────
    # A REFUSED ROW MUST NEVER DESTROY SUCCESSFUL ROWS -- and it must never
    # be auto-merged past, either. Both are true at once: the rows that were
    # written are on the branch and in the pull request; the batch simply
    # does not land without someone looking at the refusal.
    if not facts.receipts:
        wait("IMPORTER_REFUSED_NOTHING", "this run produced no receipts to check")
        wait("EVERY_ROW_NEEDED_NO_HUMAN_JUDGEMENT", "this run produced no receipts to check")
    else:
        refused = [r.get("sourceBetKey") for r in facts.receipts if not r.get("success")]
        if refused or facts.partial_delivery:
            refuse("IMPORTER_REFUSED_NOTHING",
                   f"the destination importer refused {len(refused)} row(s). The rows it "
                   "DID write are delivered and stay on the branch; the batch does not "
                   "auto-merge, because a refusal is exactly the case that needs a person")
        else:
            ok("IMPORTER_REFUSED_NOTHING")

        judged = sorted({
            str(r.get("duplicateStatus")) for r in facts.receipts
            if r.get("duplicateStatus") not in NO_JUDGEMENT_VERDICTS
        })
        conflicting = [
            r.get("sourceBetKey") for r in facts.receipts if r.get("conflictingFields")
        ]
        missing_identity = [
            r.get("sourceBetKey") for r in facts.receipts if not r.get("betId")
        ]
        problems = []
        if judged:
            problems.append(f"verdict(s) requiring judgement: {judged}")
        if conflicting:
            problems.append(f"{len(conflicting)} row(s) report conflicting fields")
        if missing_identity:
            problems.append(f"{len(missing_identity)} row(s) came back with no canonical betId")
        if problems:
            refuse("EVERY_ROW_NEEDED_NO_HUMAN_JUDGEMENT", "; ".join(problems))
        else:
            ok("EVERY_ROW_NEEDED_NO_HUMAN_JUDGEMENT")

    # ── idempotency, proved this run rather than assumed ─────────────
    if facts.rerun_changed_the_tree is None or not facts.rerun_receipts:
        wait("THE_IMPORT_IS_IDEMPOTENT", "this run did not re-apply the payload")
    else:
        problems = []
        if facts.rerun_changed_the_tree:
            problems.append("re-applying the identical payload changed the tree")
        offenders = sorted({
            str(r.get("duplicateStatus")) for r in facts.rerun_receipts
            if r.get("duplicateStatus") not in IDEMPOTENT_RERUN_VERDICTS
        })
        if offenders:
            problems.append(f"second import returned {offenders}, not DUPLICATE_NOOP")
        if len(facts.rerun_receipts) != len(facts.receipts):
            problems.append(
                f"second import returned {len(facts.rerun_receipts)} receipts, "
                f"the first returned {len(facts.receipts)}")
        first = {r.get("sourceBetKey"): r.get("betId") for r in facts.receipts}
        drifted = [
            key for key, ident in
            ((r.get("sourceBetKey"), r.get("betId")) for r in facts.rerun_receipts)
            if first.get(key) != ident
        ]
        if drifted:
            problems.append(f"{len(drifted)} row(s) changed canonical betId on re-import")
        if problems:
            refuse("THE_IMPORT_IS_IDEMPOTENT", "; ".join(problems))
        else:
            ok("THE_IMPORT_IS_IDEMPOTENT")

    # ── what the pull request actually changes ───────────────────────
    if not facts.changed_files:
        wait("ONLY_CANONICAL_WAGER_FILES_CHANGED",
             "the pull request's file list is not known yet")
    else:
        unexpected = sorted(set(facts.changed_files) - MERGEABLE_PATHS)
        if unexpected:
            refuse("ONLY_CANONICAL_WAGER_FILES_CHANGED",
                   f"changes {len(unexpected)} file(s) outside the canonical wager "
                   f"ledger: {unexpected[:5]}")
        else:
            ok("ONLY_CANONICAL_WAGER_FILES_CHANGED")

    # ── the ledger diff is an APPEND, not an edit ────────────────────
    # The single most valuable property here. The importer appends; it never
    # rewrites or deletes a canonical row. So a diff that removes one is not a
    # delivery at all, whatever produced it, and it must never land unread.
    if facts.removed_ledger_rows:
        refuse("THE_LEDGER_DIFF_IS_APPEND_ONLY",
               f"the diff REMOVES OR REWRITES {len(facts.removed_ledger_rows)} existing "
               "canonical ledger row(s); a delivery only ever appends")
    else:
        ok("THE_LEDGER_DIFF_IS_APPEND_ONLY")

    # ── every added row is one of ours, and is identifiable ──────────
    if not facts.added_ledger_rows:
        wait("EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY",
             "the pull request adds no ledger rows")
    else:
        foreign = [
            row.get("betId") for row in facts.added_ledger_rows
            if row.get("importBatchId") != ROUTER_IMPORT_BATCH_ID
        ]
        unidentified = [
            index for index, row in enumerate(facts.added_ledger_rows)
            if not row.get("betId") or not row.get("sourceBetKey")
        ]
        problems = []
        if foreign:
            problems.append(
                f"{len(foreign)} added row(s) do not carry importBatchId "
                f"{ROUTER_IMPORT_BATCH_ID!r}")
        if unidentified:
            problems.append(
                f"{len(unidentified)} added row(s) have no betId or no sourceBetKey")
        receipt_ids = {r.get("betId") for r in facts.receipts if r.get("betId")}
        if receipt_ids:
            unreceipted = [
                row.get("betId") for row in facts.added_ledger_rows
                if row.get("betId") not in receipt_ids
            ]
            if unreceipted:
                problems.append(
                    f"{len(unreceipted)} added row(s) have no matching receipt from "
                    "this run's import")
        if problems:
            refuse("EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY", "; ".join(problems))
        else:
            ok("EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY")

    # ── the destination's own CI ─────────────────────────────────────
    continuous_integration_is_green = False
    if not facts.check_runs:
        # No check has reported yet. On a repository with pull-request CI this
        # is "too early", not "nothing runs here": waiting costs one cycle,
        # and merging past an unreported suite is exactly what this is for.
        wait("CONTINUOUS_INTEGRATION_IS_GREEN",
             "no check run has reported on the head commit yet")
    else:
        pending, broken = _check_state(facts.check_runs)
        if broken:
            refuse("CONTINUOUS_INTEGRATION_IS_GREEN",
                   f"failing check(s): {broken}")
        elif pending:
            wait("CONTINUOUS_INTEGRATION_IS_GREEN",
                 f"still running: {pending}")
        else:
            ok("CONTINUOUS_INTEGRATION_IS_GREEN")
            continuous_integration_is_green = True

    # ── mergeability, as GitHub computes it ──────────────────────────
    state = facts.mergeable_state
    if facts.mergeable is None or state in (None, "unknown"):
        wait("THE_DESTINATION_BRANCH_IS_CLEANLY_MERGEABLE",
             "GitHub has not finished computing mergeability")
    elif state == "unstable" and facts.mergeable:
        # The branch MERGES cleanly; what makes it "unstable" is a check run
        # that is pending, failing, neutral or skipped.
        # CONTINUOUS_INTEGRATION_IS_GREEN above has already read every one of
        # those -- check runs AND legacy commit statuses -- and reached the
        # right verdict, so this condition must defer to it rather than
        # double-count it.
        if continuous_integration_is_green:
            # Every check reported and none failed. `unstable` here is
            # GitHub's accounting of a neutral or skipped check, not a reason
            # to hold a delivery -- and treating it as one would deadlock,
            # because nothing about it will ever change.
            ok("THE_DESTINATION_BRANCH_IS_CLEANLY_MERGEABLE")
        else:
            wait("THE_DESTINATION_BRANCH_IS_CLEANLY_MERGEABLE",
                 "mergeable, with a check run unfinished or not passing (GitHub "
                 "calls this 'unstable'); CONTINUOUS_INTEGRATION_IS_GREEN above "
                 "is the verdict on those checks")
    elif not facts.mergeable or state in TRANSIENT_MERGE_STATES:
        # `dirty` and `behind` both resolve by themselves here, because the
        # next run rebuilds this branch from the destination's current main.
        wait("THE_DESTINATION_BRANCH_IS_CLEANLY_MERGEABLE",
             f"mergeable={facts.mergeable} state={state!r}; the next run rebuilds "
             "the branch from the destination's current main")
    elif state != "clean":
        # `blocked` means a required review or a protection rule. That is a
        # human's decision and automation must not route around it.
        refuse("THE_DESTINATION_BRANCH_IS_CLEANLY_MERGEABLE",
               f"mergeable_state is {state!r}, not 'clean'; something other than "
               "this gate is withholding the merge")
    else:
        ok("THE_DESTINATION_BRANCH_IS_CLEANLY_MERGEABLE")

    assert len(passed) + len(failed) + len(waiting) == len(CONDITIONS), (
        "every condition must reach exactly one bucket")

    if failed:
        verdict = REFUSE
    elif waiting:
        verdict = WAIT
    else:
        verdict = MERGE
    return MergeVerdict(verdict=verdict, reasons=reasons, passed=passed,
                        failed=failed, waiting_on=waiting)
