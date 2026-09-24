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
from .destinations import PROFILES, UnknownDestinationError, profile_for
from .receipts import (
    IDEMPOTENT_RERUN_VERDICTS,
    conflicting_field_names,
    normalise,
)
from .sports import Sport

#: The branch a sport's delivery lives on. One long-lived branch per sport, per
#: `deliver-wagers.yml`. Derived here from the same map the workflow routes by,
#: so a new destination cannot get a branch name this gate does not recognise.
BRANCH_PREFIX = "kalshi-router/"


#: What a router branch carries. A destination receives WAGERS and, where it
#: does not settle its own, SETTLEMENTS -- and they are separate proposals with
#: separate lifecycles, so they get separate branches. One branch carrying both
#: would make a settlement wait on a wager's check suite and vice versa.
WAGERS = "wagers"
SETTLEMENTS = "settlements"
KINDS = (WAGERS, SETTLEMENTS)


def router_branch_for(sport: str, kind: str = WAGERS) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown router branch kind {kind!r}; expected one of {KINDS}")
    prefix = BRANCH_PREFIX if kind == WAGERS else f"{BRANCH_PREFIX}settle-"
    return f"{prefix}{sport}"


def router_branches() -> frozenset[str]:
    """Every branch name this router may own, and no others.

    A settlement branch is listed only for a destination that ACCEPTS
    settlements. MLB settles its own bets, so `kalshi-router/settle-MLB` is not
    a branch this router may ever push -- and a name in this set is a name the
    gate is willing to recognise, which is not somewhere to be generous."""
    names = {router_branch_for(sport.value) for sport in DESTINATION_REPOS}
    names |= {
        router_branch_for(sport.value, SETTLEMENTS)
        for sport, profile in PROFILES.items()
        if profile.settlement_importer is not None
    }
    return frozenset(names)


#: The ONLY files a router-authored pull request may change, for MLB.
#:
#: Deliberately exact paths, not a prefix. The delivery workflow already
#: refuses to COMMIT anything outside the destination's own prefixes; that is a
#: containment check, and this is a different, narrower question -- what may be
#: merged to the ledger branch without a human. A file the importer starts
#: writing later is not automatically safe to land unread, so it lands here as
#: a refusal and someone looks once, rather than being waved through forever by
#: a prefix match.
#:
#: KEPT AS MLB'S ANSWER, and no longer as THE answer. Every destination has its
#: own set in its profile; this name remains because `deliver_branch.py` reads
#: it to decide whether the canonical ledger MOVED, and because a caller that
#: does not know its sport is better served by MLB's exact paths than by a
#: prefix. `mergeable_paths_for` is what the gate uses.
MERGEABLE_PATHS = PROFILES[Sport.MLB].mergeable_paths


def mergeable_paths_for(sport: str) -> frozenset[str]:
    """What this destination may land on its ledger branch without a human.

    An unknown sport gets an EMPTY set, which makes every changed file
    unexpected and the gate refuse. That is the correct answer: a destination
    nobody has described is not one this router may merge into.
    """
    try:
        return profile_for(sport).mergeable_paths
    except UnknownDestinationError:
        return frozenset()


def is_mergeable_path(sport: str, path: str) -> bool:
    """May this file land on the ledger branch without a human?

    An exact `mergeable_paths` entry, or a FULL match of one of the profile's
    `mergeable_patterns` (a per-record ledger cannot list its paths). An unknown
    sport matches nothing, so the gate refuses."""
    import re

    try:
        profile = profile_for(sport)
    except UnknownDestinationError:
        return False
    if path in profile.mergeable_paths:
        return True
    return any(re.fullmatch(pattern, path) for pattern in profile.mergeable_patterns)


def record_layout_for(sport: str) -> str:
    try:
        return profile_for(sport).record_layout
    except UnknownDestinationError:
        return "jsonl"


def ledger_pathspecs_for(sport: str) -> tuple[str, ...]:
    """What `git diff` should be restricted to when asking about the ledger.

    The exact paths for a JSONL ledger; the committable prefixes for a
    per-record one, whose files cannot be named in advance. Empty for an
    unknown sport -- callers treat that as "cannot reason about it"."""
    try:
        profile = profile_for(sport)
    except UnknownDestinationError:
        return ()
    if profile.record_layout == "json_per_file":
        return tuple(profile.committable_prefixes)
    return tuple(sorted(profile.mergeable_paths))


def ledger_branch_runs_ci(sport: str) -> bool:
    """Does a pull request into this destination's ledger branch get checked?

    An unknown sport answers TRUE, which makes the gate demand a check run it
    will never see and therefore WAIT. That is the fail-closed direction: the
    alternative answer would let a destination nobody has described merge with
    no verdict at all."""
    try:
        return profile_for(sport).ledger_branch_runs_ci
    except UnknownDestinationError:
        return True


def ledger_branch_for(sport: str) -> str:
    """The branch a router pull request targets. `main` unless stated."""
    try:
        return profile_for(sport).ledger_branch
    except UnknownDestinationError:
        return "main"

#: Mergeability states that resolve on their own WITHOUT anything else changing.
#:
#: `behind` and `dirty` are here because the next run rebuilds this branch from
#: the destination's current main, so both are answered by simply running again.
#: `unstable` is deliberately NOT here -- see CHECK_DERIVED_MERGE_STATES. It is not
#: self-resolving in general: it means "mergeable, but a check is unhappy", and
#: whether that resolves depends entirely on WHICH check and HOW, which is a
#: question only the check runs can answer.
TRANSIENT_MERGE_STATES = frozenset({"unknown", "behind", "dirty"})

#: `mergeable_state` values GitHub derives from the commit's check/status
#: rollup rather than from the branch's mergeability.
#:
#: GitHub reports `unstable` when a pull request IS mergeable but its checks are
#: not all green -- and that includes checks that are *still running*. That is
#: the production state of 2026-09-19: `mergeable=true`, required CI still
#: `in_progress`, `mergeable_state="unstable"`. The gate read it as "something
#: other than this gate is withholding the merge" and REFUSED, in the same
#: breath as it correctly marked CI itself a WAIT. One run, two opposite
#: readings of one fact.
#:
#: So a check-derived state is not decided here at all. It is delegated to the
#: check runs, which are the thing it is derived FROM -- pending checks mean
#: WAIT, failing checks mean REFUSE, and a state that persists when every check
#: this gate can see is green means something it CANNOT see is unhappy, which
#: fails closed.
CHECK_DERIVED_MERGE_STATES = frozenset({"unstable"})

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
    #: The destination's OWN ledger validator's verdict, for a destination
    #: whose ledger branch runs no CI. None means it has not reported.
    destination_validator_passed: bool | None = None
    #: Whether this proposal carries wagers or settlements. They are separate
    #: branches with separate lifecycles, and the gate must evaluate the one it
    #: was pointed at rather than assume wagers.
    branch_kind: str = WAGERS


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
    expected_branch = router_branch_for(facts.sport, facts.branch_kind)
    problems = []
    if facts.head_ref != expected_branch:
        problems.append(f"head is {facts.head_ref!r}, not {expected_branch!r}")
    if facts.head_repo and facts.head_repo != facts.destination_repo:
        problems.append(
            f"head repository is {facts.head_repo!r}, not the destination "
            f"{facts.destination_repo!r} (a fork cannot be auto-merged)")
    expected_base = ledger_branch_for(facts.sport)
    if facts.base_ref != expected_base:
        # NOT hardcoded to `main` any more. CFB's canonical ledger lives on
        # `accounting-data`, and a gate that demanded `main` would refuse every
        # correct CFB delivery while accepting one aimed at the wrong branch.
        problems.append(f"base is {facts.base_ref!r}, not {expected_base!r}")
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
    receipts = normalise(list(facts.receipts))
    if not receipts:
        wait("IMPORTER_REFUSED_NOTHING", "this run produced no readable receipts to check")
        wait("EVERY_ROW_NEEDED_NO_HUMAN_JUDGEMENT",
             "this run produced no readable receipts to check")
    else:
        refused = [r.source_key for r in receipts if not r.success]
        if refused or facts.partial_delivery:
            refuse("IMPORTER_REFUSED_NOTHING",
                   f"the destination importer refused {len(refused)} row(s). The rows it "
                   "DID write are delivered and stay on the branch; the batch does not "
                   "auto-merge, because a refusal is exactly the case that needs a person")
        else:
            ok("IMPORTER_REFUSED_NOTHING")

        judged = sorted({str(r.verdict) for r in receipts if r.needs_judgement})
        conflicting = [r.source_key for r in receipts if r.conflicting_fields]
        missing_identity = [r.source_key for r in receipts if not r.identity]
        problems = []
        if judged:
            problems.append(f"verdict(s) requiring judgement: {judged}")
        if conflicting:
            problems.append(f"{len(conflicting)} row(s) report conflicting fields")
        if missing_identity:
            problems.append(
                f"{len(missing_identity)} row(s) came back with no canonical id. A row the "
                "destination will not name is a row nobody can refer to later, so it does not "
                "merge unread"
            )
        names = conflicting_field_names(receipts)
        if names:
            problems.append(f"fields the destination disagrees on (names only): {list(names)}")
        if problems:
            refuse("EVERY_ROW_NEEDED_NO_HUMAN_JUDGEMENT", "; ".join(problems))
        else:
            ok("EVERY_ROW_NEEDED_NO_HUMAN_JUDGEMENT")

    # ── idempotency, proved this run rather than assumed ─────────────
    rerun = normalise(list(facts.rerun_receipts))
    if facts.rerun_changed_the_tree is None or not rerun:
        wait("THE_IMPORT_IS_IDEMPOTENT", "this run did not re-apply the payload")
    else:
        problems = []
        if facts.rerun_changed_the_tree:
            problems.append("re-applying the identical payload changed the tree")
        offenders = sorted({
            str(r.verdict) for r in rerun if r.verdict not in IDEMPOTENT_RERUN_VERDICTS
        })
        if offenders:
            problems.append(f"second import returned {offenders}, not DUPLICATE_NOOP")
        if len(rerun) != len(receipts):
            problems.append(
                f"second import returned {len(rerun)} receipts, "
                f"the first returned {len(receipts)}")
        first = {r.source_key: r.identity for r in receipts}
        drifted = [
            key for key, ident in ((r.source_key, r.identity) for r in rerun)
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
        unexpected = sorted(p for p in set(facts.changed_files)
                            if not is_mergeable_path(facts.sport, p))
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
    #
    # *** A WAGER ROW AND A SETTLEMENT ROW DO NOT CARRY THE SAME IDENTITY ***
    #
    # A wager row carries an import batch id: it is the row's provenance, the
    # destination schemas declare it REQUIRED, and `_write_payloads` stamps it
    # on every row of every wager payload.
    #
    # A settlement row does not, and that is the contract on BOTH sides rather
    # than an omission. `_settlement_row` emits no batch id; the destinations'
    # settlement schemas model none and their importers REFUSE a row carrying
    # an unknown field, so sending one would not add identity -- it would
    # refuse all 41 rows at the importer instead of at this gate. A settlement
    # is keyed on the wager it settles: its canonical id is minted from that
    # wager's `source_bet_key` alone, and it may only be written at all if that
    # wager is already in the ledger.
    #
    # Reading a settlement row with the wager rule is what refused CFB #53 on
    # 2026-09-22 -- 41 correct rows, every other condition PASS, marked foreign
    # for want of a field their schema forbids. The fix is to check the
    # identity each kind ACTUALLY has, not to stop checking.
    if not facts.added_ledger_rows:
        wait("EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY",
             "the pull request adds no ledger rows")
    else:
        # EACH LEDGER SPELLS THESE ITS OWN WAY, and reading only MLB's spelling
        # would make every CFB row look foreign and unidentified -- which
        # refuses a correct delivery on every run, forever. Both spellings are
        # read; neither is inferred from the other.
        def batch_of(row):
            return row.get("importBatchId") or row.get("import_batch_id")

        def identity_of(row):
            # An NFL settlement AMENDMENT names itself `amendment_id` and the settlement it supersedes
            # `amends`; it deliberately carries no `settlement_id` of its own (it is not a settlement).
            return (row.get("betId") or row.get("wager_id") or row.get("imported_wager_id")
                    or row.get("settlement_id") or row.get("amendment_id"))

        def key_of(row):
            return row.get("sourceBetKey") or row.get("source_bet_key")

        settlements = facts.branch_kind == SETTLEMENTS
        problems = []

        if settlements:
            # A settlement row is not REQUIRED to carry a batch id. One that
            # does carry a foreign one is still foreign -- absence is the
            # contract, a stranger's value never is.
            foreign = [
                identity_of(row) for row in facts.added_ledger_rows
                if batch_of(row) is not None and batch_of(row) != ROUTER_IMPORT_BATCH_ID
            ]
        else:
            foreign = [
                identity_of(row) for row in facts.added_ledger_rows
                if batch_of(row) != ROUTER_IMPORT_BATCH_ID
            ]
        if foreign:
            problems.append(
                f"{len(foreign)} added row(s) do not carry the router's import batch id "
                f"{ROUTER_IMPORT_BATCH_ID!r}")

        unidentified = [
            index for index, row in enumerate(facts.added_ledger_rows)
            if not identity_of(row) or not key_of(row)
        ]
        if unidentified:
            problems.append(
                f"{len(unidentified)} added row(s) have no canonical id or no source key")

        # Matched on the SOURCE KEY rather than on the destination's minted id.
        # The source key is the router's own and every destination echoes it;
        # the minted id is the destination's and one of them does not return it
        # in its counts-only receipt shape. Matching on the id would refuse a
        # correct delivery for want of a field the router never supplied.
        #
        # *** LOAD-BEARING FOR SETTLEMENTS, SO IT IS NOT OPTIONAL THERE ***
        # For a wager batch the constant batch id is a second, independent
        # answer to "is this ours". A settlement has no such constant, so this
        # receipt match IS the answer -- and a missing answer must refuse
        # rather than skip. It is also the stronger of the two: a batch id is a
        # literal anybody could write into a row, whereas this set was produced
        # by THIS RUN'S import of THIS payload.
        receipt_keys = {r.source_key for r in receipts if r.source_key}
        if receipt_keys:
            unreceipted = [
                key_of(row) for row in facts.added_ledger_rows
                if key_of(row) not in receipt_keys
            ]
            if unreceipted:
                problems.append(
                    f"{len(unreceipted)} added row(s) have no matching receipt from "
                    "this run's import")
        elif settlements:
            problems.append(
                "this run produced no usable receipts, so there is nothing proving the "
                "added settlement rows came from it")

        if problems:
            refuse("EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY", "; ".join(problems))
        else:
            ok("EVERY_ADDED_ROW_CARRIES_THE_ROUTER_IDENTITY")

    # ── the destination's own verdict on its own data ────────────────
    #
    # TWO WAYS TO GET ONE, AND NEVER ZERO.
    #
    # Most destinations answer through pull-request CI. One cannot: GitHub runs
    # a `pull_request` workflow only if that workflow file exists on the pull
    # request's BASE branch, and CFB's base is an ORPHAN data branch with no
    # `.github/` at all. Measured, not assumed -- its two backfill pull
    # requests have zero workflow runs between them. So no check ever reports
    # there, and a gate that only knew about CI would wait forever for a signal
    # that cannot arrive.
    #
    # Such a destination supplies the verdict a different way: the delivery
    # runs THAT DESTINATION'S OWN validator, from its own code branch, against
    # the tree its own importer just produced, and the result is reported here.
    # It is the destination's judgement either way; only the transport differs.
    if not ledger_branch_runs_ci(facts.sport):
        if facts.destination_validator_passed is None:
            wait("CONTINUOUS_INTEGRATION_IS_GREEN",
                 "this destination's ledger branch runs no CI and its own validator has not "
                 "reported yet")
        elif facts.destination_validator_passed:
            ok("CONTINUOUS_INTEGRATION_IS_GREEN")
        else:
            refuse("CONTINUOUS_INTEGRATION_IS_GREEN",
                   "the destination's own ledger validator REFUSED the tree this delivery "
                   "produced")
    elif not facts.check_runs:
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

    # ── mergeability, as GitHub computes it ──────────────────────────
    state = facts.mergeable_state
    condition = "THE_DESTINATION_BRANCH_IS_CLEANLY_MERGEABLE"
    if facts.mergeable is None or state in (None, "unknown"):
        wait(condition, "GitHub has not finished computing mergeability")
    elif not facts.mergeable or state in TRANSIENT_MERGE_STATES:
        # `dirty` and `behind` both resolve by themselves here, because the
        # next run rebuilds this branch from the destination's current main.
        wait(condition,
             f"mergeable={facts.mergeable} state={state!r}; the next run rebuilds "
             "the branch from the destination's current main")
    elif state in CHECK_DERIVED_MERGE_STATES:
        # NOT a mergeability answer -- a restatement of the check rollup. The
        # check runs are the primary evidence, so they decide, and this
        # condition simply agrees with the one above it rather than
        # contradicting it.
        pending, broken = _check_state(facts.check_runs)
        if broken:
            refuse(condition,
                   f"mergeable_state is {state!r} and check(s) FAILED: {broken}")
        elif pending or not facts.check_runs:
            # THE PRODUCTION STATE. mergeable=true, checks still running.
            # The branch itself merges cleanly; only the rollup is unfinished,
            # and it finishes on its own. Nobody is paged and nothing merges.
            still = pending or ["no check has reported yet"]
            wait(condition,
                 f"mergeable={facts.mergeable} state={state!r} solely because "
                 f"check(s) are still running: {still}; the next run re-reads them")
        else:
            # Every check this gate can see is green, and GitHub still will not
            # call it clean. Something it cannot see -- a required check that
            # has not reported, a rule this gate does not model -- is
            # withholding the merge. FAIL CLOSED.
            refuse(condition,
                   f"mergeable_state is {state!r} although every check this gate "
                   "can see is green; something it cannot see is withholding the "
                   "merge, and it will not be guessed at")
    elif state != "clean":
        # `blocked` means a required review or a protection rule. That is a
        # human's decision and automation must not route around it.
        refuse(condition,
               f"mergeable_state is {state!r}, not 'clean'; something other than "
               "this gate is withholding the merge")
    else:
        ok(condition)

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
