"""THE 2026-09-19 CHURN, REPRODUCED -- AND THE REASON IT ESCAPED #78.

WHAT PRODUCTION DID
-------------------
Two `Deliver wagers downstream` runs, 27 minutes apart, on the same router
commit and the same destination base ``067c321e``::

    run 35465098715  ->  edge-finder-api 7e7108bc  tree a9591950
    run 35466474703  ->  edge-finder-api 673991474  tree de4b2511

Same 41-row payload. Same 41 canonical bet ids. 17 DUPLICATE_NOOP, 24 NEW,
0 failed rows, both times. And two different trees, differing in exactly three
fields on exactly the 24 new rows::

    createdAt              19:50:03Z -> 20:17:33Z
    recordedAt             19:50:03Z -> 20:17:33Z
    provenance.ingestedAt  19:50:03Z -> 20:17:33Z

A different tree is a new commit, a new commit is a force-push, and a
force-push restarts a ~19 minute check suite on a 15 minute cadence. The head
was therefore essentially never a commit whose checks had finished, and the
auto-merge gate -- which requires green CI -- could never fire.

WHY #78 DID NOT CATCH IT
------------------------
#78 added exactly the right check: keep the existing commit when the remote
branch already carries this tree on this base. It also added
``test_identical_content_does_not_produce_a_new_commit_every_run``, which
passed -- because the harness's stand-in importer wrote rows with no
timestamps at all. A deterministic fake produced an identical tree on the
second run; the real importer never could. The check was correct and
unreachable, and the test proved a property of the fixture.

So the fake importer now stamps the clock exactly like
`lib.edgelab.bets.build_manual_bet_record` does, and every test in this file
ADVANCES that clock between runs, the way wall time advances between two
scheduled runs. Without the repair, that alone is enough to reproduce the
churn -- which is the property #78's version of this test was missing.

THE REPAIR
----------
`kalshi_router.delivery_branch` changes WHERE the importer runs, not what it
writes. When the router's branch already proposes against the destination's
current main, the importer is run on top of that branch, so rows it already
proposed come back DUPLICATE_NOOP and keep their original bytes by the
importer's own contract. Nothing is faked, nothing is stripped, and the router
still never edits a ledger file.
"""

import json
import os
import subprocess

import pytest

from kalshi_router import delivery_branch
from kalshi_router.destination import DESTINATION_REPOS
from tests.test_automerge_gate import drop_the_conflicted_row, main_ledger
from tests.test_partial_delivery_regression import (  # the real harness, reused
    CONFLICTED_SOURCE_KEY,
    LEDGER,
    ROOT,
    _git,
    branch_ledger,
    run_delivery,
    world,  # noqa: F401 -- pytest fixture
)

BRANCH = "kalshi-router/MLB"

#: Two readings of the clock, 27 minutes apart -- the real gap between run
#: 35465098715 and run 35466474703.
FIRST_CLOCK = "2026-09-19T19:50:03Z"
SECOND_CLOCK = "2026-09-19T20:17:33Z"

VOLATILE_FIELDS = ("createdAt", "recordedAt")


def deliver_at(world, clock):  # noqa: F811
    """One delivery run, with the destination's clock pinned to `clock`.

    Pinning is what makes this deterministic rather than dependent on two
    subprocesses happening to land in different seconds. It models the real
    importer faithfully: `ids.utc_now_iso()` returns whatever the wall clock
    says at the moment a NEW row is written.
    """
    world["env"]["FAKE_IMPORT_CLOCK"] = clock
    return run_delivery(world)


def branch_head(world):  # noqa: F811
    return _git("rev-parse", f"refs/heads/{BRANCH}", cwd=world["remote"]).strip()


def branch_tree(world):  # noqa: F811
    return _git("rev-parse", f"refs/heads/{BRANCH}^{{tree}}", cwd=world["remote"]).strip()


def stamps(rows):
    """{sourceBetKey: (createdAt, recordedAt, provenance.ingestedAt)}.

    Router-written rows only. The fixture seeds the destination with one row
    that predates every delivery here and was written straight into the ledger,
    so it carries none of these fields -- including it would assert about the
    fixture rather than about the router.
    """
    rows = [row for row in rows if row["sourceBetKey"] != CONFLICTED_SOURCE_KEY]
    return {
        row["sourceBetKey"]: tuple(
            [row.get(field) for field in VOLATILE_FIELDS]
            + [(row.get("provenance") or {}).get("ingestedAt")]
        )
        for row in rows
    }


# ══════════════════════════════════════════════════════════════════════
# The decisions themselves. Pure, so the production state is one literal.
# ══════════════════════════════════════════════════════════════════════

def test_the_importer_is_seeded_from_a_branch_whose_ledger_has_not_moved():
    """THE FIX, as one assertion."""
    remote = delivery_branch.RemoteBranch(head="b" * 40, base="a" * 40, tree="t" * 40)
    seed, why = delivery_branch.choose_seed("a" * 40, remote, ledger_moved=False)
    assert seed == "b" * 40
    assert why == delivery_branch.SEED_BRANCH


def test_a_destination_commit_the_proposal_does_not_depend_on_keeps_the_branch():
    """THE SECOND FIX. main moved, the canonical ledger did not.

    Rebuilding here is what made the repair unusable in production: the live
    destination commits its own pipeline output every one to five minutes and
    its CI takes nineteen, so a head that resets on every destination commit
    can never carry a finished check suite -- which is the same failure the
    timestamp churn caused, arriving by a different road.
    """
    remote = delivery_branch.RemoteBranch(head="b" * 40, base="OLD-MAIN", tree="t" * 40)
    seed, why = delivery_branch.choose_seed("NEW-MAIN", remote, ledger_moved=False)
    assert seed == "b" * 40
    assert why == delivery_branch.SEED_BRANCH


def test_the_importer_is_seeded_from_main_when_the_canonical_ledger_moved():
    """The one thing a proposal DOES depend on. Reconcile against what is
    there now, whatever that costs in check-suite minutes."""
    remote = delivery_branch.RemoteBranch(head="b" * 40, base="OLD-MAIN", tree="t" * 40)
    seed, why = delivery_branch.choose_seed("NEW-MAIN", remote, ledger_moved=True)
    assert seed == "NEW-MAIN"
    assert why == delivery_branch.SEED_MAIN


def test_the_importer_is_seeded_from_main_when_no_branch_exists():
    seed, why = delivery_branch.choose_seed(
        "a" * 40, delivery_branch.RemoteBranch(), ledger_moved=False)
    assert seed == "a" * 40
    assert why == delivery_branch.SEED_MAIN


def test_an_identical_tree_on_the_kept_proposal_is_adopted():
    remote = delivery_branch.RemoteBranch(head="b" * 40, base="a" * 40, tree="tree-x")
    action, _why = delivery_branch.decide(
        "base-tree", "tree-x", remote, seeded_from_branch=True)
    assert action == delivery_branch.ADOPT


def test_an_identical_tree_is_NOT_adopted_when_the_batch_was_rebuilt():
    """A rebuild means the ledger moved under the old proposal, so its tree is
    not evidence about the new one. It is re-pushed and re-checked."""
    remote = delivery_branch.RemoteBranch(head="b" * 40, base="old", tree="tree-x")
    action, _why = delivery_branch.decide(
        "base-tree", "tree-x", remote, seeded_from_branch=False)
    assert action == delivery_branch.PUSH


def test_a_changed_tree_is_pushed():
    remote = delivery_branch.RemoteBranch(head="b" * 40, base="a" * 40, tree="tree-x")
    action, _why = delivery_branch.decide(
        "base-tree", "tree-y", remote, seeded_from_branch=True)
    assert action == delivery_branch.PUSH


def test_a_result_identical_to_its_base_is_nothing_to_deliver():
    """Checked FIRST, so a branch whose rows have landed is not re-proposed."""
    remote = delivery_branch.RemoteBranch(head="b" * 40, base="a" * 40, tree="base-tree")
    action, _why = delivery_branch.decide(
        "base-tree", "base-tree", remote, seeded_from_branch=False)
    assert action == delivery_branch.NOTHING_TO_DELIVER


@pytest.mark.parametrize("path", [
    "scripts/edgelab/__pycache__/x.pyc",
    "data/edgelab/bets/bets.jsonl.lock.oops",
    ".gitignore",
    "config/rules.json",
])
def test_only_data_may_be_committed_into_someone_elses_repository(path):
    """`git add -A` is safe only because the destination's own .gitignore
    covers what the importer leaves behind -- THEIR property, which can change
    without telling this router."""
    staged = ["data/edgelab/bets/bets.jsonl", path]
    if path.startswith("data/"):
        assert delivery_branch.uncommittable(staged) == ()
    else:
        assert delivery_branch.uncommittable(staged) == (path,)


def test_nothing_in_the_branch_lifecycle_is_sport_specific():
    """The router is cross-sport. A destination added to DESTINATION_REPOS must
    inherit the repaired lifecycle, not need a copy of it."""
    source = (ROOT / "src/kalshi_router/delivery_branch.py").read_text()
    for sport in DESTINATION_REPOS:
        assert sport.value not in source.replace("MLB wagers", ""), (
            f"{sport.value} is named in the branch lifecycle")
    assert "edge-finder-api" not in source


# ══════════════════════════════════════════════════════════════════════
# TEST A -- same payload, same base, time advanced
# ══════════════════════════════════════════════════════════════════════

def test_A_an_unchanged_batch_redelivered_later_keeps_the_same_head(world):  # noqa: F811
    """THE PRODUCTION FAILURE, and the proof it is fixed.

    Two runs, same payload, same destination main, 27 minutes apart. Before
    the repair this produced two trees and a force-push; the second run must
    now produce the SAME canonical tree and leave the branch alone.
    """
    drop_the_conflicted_row(world)

    first = deliver_at(world, FIRST_CLOCK)
    assert "pushed kalshi-router/MLB" in first.stdout
    head_after_first, tree_after_first = branch_head(world), branch_tree(world)
    rows_after_first = branch_ledger(world)

    second = deliver_at(world, SECOND_CLOCK)

    assert branch_tree(world) == tree_after_first, (
        "the same payload on the same base produced a different canonical tree")
    assert branch_head(world) == head_after_first, (
        "the branch head moved for identical content; CI would restart forever")
    assert "already carries this exact content on this base; not pushing" in second.stdout
    assert "pushed kalshi-router/MLB" not in second.stdout
    assert second.returncode == 0, "a stable re-delivery must be a green run"


def test_A_the_second_run_reimports_as_duplicate_noop_not_as_new(world):  # noqa: F811
    """WHY the tree is stable: the importer recognises its own rows.

    This is the whole mechanism. The rows are not preserved by the router
    rewriting them -- they are preserved because the importer is shown the
    branch it already wrote them to, and its own duplicate detection returns
    the ALREADY-STORED row.
    """
    drop_the_conflicted_row(world)
    deliver_at(world, FIRST_CLOCK)
    first_receipts = json.loads(world["receipts"].read_text())
    assert {r["duplicateStatus"] for r in first_receipts} == {"NEW"}

    deliver_at(world, SECOND_CLOCK)
    second_receipts = json.loads(world["receipts"].read_text())

    assert {r["duplicateStatus"] for r in second_receipts} == {"DUPLICATE_NOOP"}
    assert not [r for r in second_receipts if not r["success"]]
    # 12. THE CANONICAL BET ID IS STABLE FOR A STABLE sourceBetKey.
    assert ({r["sourceBetKey"]: r["betId"] for r in second_receipts}
            == {r["sourceBetKey"]: r["betId"] for r in first_receipts})


def test_A_the_originally_proposed_ingestion_metadata_survives(world):  # noqa: F811
    """The three fields the incident moved, pinned by name.

    They are PRESERVED, not faked and not stripped: the row really was first
    ingested at 19:50:03Z, and the run at 20:17:33Z must not claim otherwise
    -- nor invent a new answer to a question already answered.
    """
    drop_the_conflicted_row(world)
    deliver_at(world, FIRST_CLOCK)
    before = stamps(branch_ledger(world))
    assert before, "no rows were delivered"
    assert all(FIRST_CLOCK in row for row in before.values())

    deliver_at(world, SECOND_CLOCK)
    after = stamps(branch_ledger(world))

    assert after == before, "a re-delivery rewrote canonical ingestion metadata"
    assert not any(SECOND_CLOCK in row for row in after.values()), (
        "the later run restamped rows it had already proposed")


def test_A_no_force_push_happens_merely_because_the_clock_moved(world):  # noqa: F811
    """7. Proved from the REMOTE's reflog, not from a log line.

    A push that lands an identical tree is still a push: it updates the ref,
    restarts CI and resets the check suite. So the assertion is that the
    remote's ref did not move at all.
    """
    drop_the_conflicted_row(world)
    deliver_at(world, FIRST_CLOCK)
    before = _git("for-each-ref", "--format=%(objectname)", f"refs/heads/{BRANCH}",
                  cwd=world["remote"]).strip()

    deliver_at(world, SECOND_CLOCK)
    deliver_at(world, "2026-09-19T20:44:10Z")

    after = _git("for-each-ref", "--format=%(objectname)", f"refs/heads/{BRANCH}",
                 cwd=world["remote"]).strip()
    assert after == before, "the delivery ref moved across two unchanged re-runs"


# ══════════════════════════════════════════════════════════════════════
# TEST B -- a genuinely new wager
# ══════════════════════════════════════════════════════════════════════

def test_B_a_genuinely_new_wager_does_produce_a_new_proposal(world):  # noqa: F811
    """Stability must not become staleness. A real new wager is a real new
    proposal, and the destination's CI is expected to run again for it."""
    rows = drop_the_conflicted_row(world)
    deliver_at(world, FIRST_CLOCK)
    head_before = branch_head(world)
    before = stamps(branch_ledger(world))

    body = json.loads(world["payload"].read_text())
    body["rows"] = rows + [{
        "sourceBetKey": "kalshi-order-0099",
        "marketTicker": "KXMLBGAME-26SEP1599-TOR", "side": "YES",
        "gameDate": "2026-09-15", "contracts": "3", "entryPrice": "0.51",
        "exchangeFee": "0.02", "stake": "1.53",
    }]
    world["payload"].write_text(json.dumps(body))

    result = deliver_at(world, SECOND_CLOCK)

    assert "pushed kalshi-router/MLB" in result.stdout
    assert branch_head(world) != head_before
    ledger = branch_ledger(world)
    assert len(ledger) == 18, "the new wager did not reach the branch"

    # The new row carries the clock reading of the run that FIRST ingested it,
    # and the sixteen already-proposed rows keep theirs.
    after = stamps(ledger)
    assert {k: v for k, v in after.items() if k in before} == before
    assert SECOND_CLOCK in after["kalshi-order-0099"]

    # And the new proposal is itself stable: re-delivering it changes nothing.
    deliver_at(world, "2026-09-19T21:03:00Z")
    assert branch_head(world) != head_before
    assert stamps(branch_ledger(world)) == after


# ══════════════════════════════════════════════════════════════════════
# TEST C -- the destination's main advanced
# ══════════════════════════════════════════════════════════════════════

def _commit_on_destination_main(world, path, body, message):  # noqa: F811
    """Somebody else commits to the destination's main, as its own pipelines do."""
    other = world["tmp"] / f"other-{os.urandom(4).hex()}"
    subprocess.run(["git", "clone", "--quiet", str(world["remote"]), str(other)],
                   check=True, capture_output=True)
    target = other / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if path == LEDGER:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(body)
    else:
        target.write_text(body)
    _git("add", "-A", cwd=other)
    _git("-c", "user.email=o@example.invalid", "-c", "user.name=o",
         "commit", "--quiet", "-m", message, cwd=other)
    _git("push", "--quiet", "origin", "main", cwd=other)
    return _git("rev-parse", "refs/heads/main", cwd=world["remote"]).strip()


def test_C_a_moved_canonical_ledger_is_rebuilt_on_and_never_overwritten(world):  # noqa: F811
    """THE SAFETY HALF OF TEST C, and the one that matters.

    The canonical ledger is the only thing a proposal depends on. When it
    moves, the batch is reconciled against what is on main NOW -- and the row
    somebody else wrote there survives untouched.
    """
    drop_the_conflicted_row(world)
    deliver_at(world, FIRST_CLOCK)
    head_before = branch_head(world)

    foreign = json.dumps({
        "betId": "someone-elses-row", "sourceBetKey": "manual-9999",
        "importBatchId": "a-different-batch", "marketTicker": "KXOTHER-1",
        "side": "YES", "gameDate": "2026-09-15",
    }, sort_keys=True) + "\n"
    moved_main = _commit_on_destination_main(world, LEDGER, foreign, "a manual bet")

    result = deliver_at(world, SECOND_CLOCK)

    assert "pushed kalshi-router/MLB" in result.stdout
    assert branch_head(world) != head_before, "a moved ledger did not force a rebuild"
    parent = _git("rev-parse", f"refs/heads/{BRANCH}^", cwd=world["remote"]).strip()
    assert parent == moved_main, "the proposal was not rebuilt on current main"

    ledger = branch_ledger(world)
    assert any(r["betId"] == "someone-elses-row" for r in ledger), (
        "the delivery overwrote a row somebody else wrote to main")
    # Every row this batch delivers is still there, alongside theirs. (The
    # fixture also seeds main with one earlier router row, which is why a
    # count of router-batch rows is not the assertion to make here.)
    delivered = {r["sourceBetKey"] for r in json.loads(world["payload"].read_text())["rows"]}
    assert delivered <= {r["sourceBetKey"] for r in ledger}

    # And the rebuilt proposal is stable in its turn.
    rebuilt = branch_head(world)
    deliver_at(world, "2026-09-19T21:30:00Z")
    assert branch_head(world) == rebuilt


def test_C_a_destination_commit_the_proposal_does_not_depend_on_keeps_the_head(world):  # noqa: F811
    """THE SECOND PRODUCTION DEFECT, and it arrived by a different road.

    Rebuilding on ANY destination commit is safe and, measured against the
    live destination, unusable: it commits its own pipeline output every one
    to five minutes and its pull-request CI takes nineteen, so the head reset
    faster than any check suite could finish and the gate still could not
    fire. Measured 2026-09-19, across four consecutive deliveries.

    A snapshot or a report is not the canonical wager ledger. The proposal
    does not depend on it, so the head stays and its checks are allowed to
    finish -- and the destination's change is still carried into the merge,
    which is what the append-only and clean-mergeability conditions verify.
    """
    drop_the_conflicted_row(world)
    deliver_at(world, FIRST_CLOCK)
    head_before = branch_head(world)
    stamps_before = stamps(branch_ledger(world))

    _commit_on_destination_main(
        world, "reports/daily.md", "an unrelated report\n", "nightly report")

    result = deliver_at(world, SECOND_CLOCK)

    assert branch_head(world) == head_before, (
        "an unrelated destination commit reset the head; CI would restart forever")
    assert "pushed kalshi-router/MLB" not in result.stdout
    assert "already carries this exact content on this base; not pushing" in result.stdout
    assert stamps(branch_ledger(world)) == stamps_before
    assert result.returncode == 0


def test_C_the_gate_reads_the_branchs_own_parent_as_the_base(world):  # noqa: F811
    """What makes keeping the head safe: the gate inspects exactly what
    merging would apply.

    With a destination commit on main that the branch does not carry, a diff
    against the RUN's clone of main would show that commit's file as reverted
    and the gate would refuse. Against the branch's own parent -- the true
    merge base -- the diff is the pull request's own change and nothing else.
    """
    drop_the_conflicted_row(world)
    # The first run pushes while CI is still pending, which is what a real
    # first delivery sees -- and it must NOT merge, or there is no open
    # proposal left for the second run to reason about.
    deliver_at(world, FIRST_CLOCK)

    _commit_on_destination_main(
        world, "reports/daily.md", "an unrelated report\n", "nightly report")

    world["stub"].check_runs = [("test", "completed", "success")]
    result = deliver_at(world, SECOND_CLOCK)

    assert "PASS   ONLY_CANONICAL_WAGER_FILES_CHANGED" in result.stdout, result.stdout
    assert "PASS   THE_LEDGER_DIFF_IS_APPEND_ONLY" in result.stdout
    # No condition FAILED. Matched on the rendered bucket prefix, because the
    # condition NAME `IMPORTER_REFUSED_NOTHING` contains the word.
    assert "    REFUSE " not in result.stdout, result.stdout

    # And it goes all the way: the proposal kept its head across a destination
    # commit it does not carry, and then LANDED.
    assert "verdict: MERGE" in result.stdout
    assert "MERGED #218" in result.stdout
    assert world["stub"].merged_sha is not None
    landed = main_ledger(world)
    assert {r["sourceBetKey"] for r in json.loads(world["payload"].read_text())["rows"]} \
        <= {r["sourceBetKey"] for r in landed}, "the rows did not reach main"
    # The destination's own commit is still on main, untouched by the merge.
    assert any(r.get("betId") for r in landed)


# ══════════════════════════════════════════════════════════════════════
# TEST D -- identical sourceBetKeys
# ══════════════════════════════════════════════════════════════════════

def test_D_identical_source_bet_keys_never_duplicate_a_canonical_row(world):  # noqa: F811
    """Three deliveries of the same batch: one row per wager, forever."""
    rows = drop_the_conflicted_row(world)
    deliver_at(world, FIRST_CLOCK)
    deliver_at(world, SECOND_CLOCK)
    deliver_at(world, "2026-09-19T20:44:10Z")

    ledger = branch_ledger(world)
    keys = [row["sourceBetKey"] for row in ledger]
    ids = [row["betId"] for row in ledger]
    assert len(keys) == len(set(keys)), f"duplicate sourceBetKey: {len(keys)} rows"
    assert len(ids) == len(set(ids)), "betId collision"
    assert set(keys) >= {row["sourceBetKey"] for row in rows}


def test_D_the_canonical_bet_id_is_a_function_of_the_row_not_of_the_run(world):  # noqa: F811
    drop_the_conflicted_row(world)
    deliver_at(world, FIRST_CLOCK)
    first = {r["sourceBetKey"]: r["betId"] for r in branch_ledger(world)}

    deliver_at(world, SECOND_CLOCK)
    assert {r["sourceBetKey"]: r["betId"] for r in branch_ledger(world)} == first


# ══════════════════════════════════════════════════════════════════════
# The protections this repair must not have weakened
# ══════════════════════════════════════════════════════════════════════

def test_a_conflicted_row_still_fails_closed_across_a_stable_redelivery(world):  # noqa: F811
    """9 & 10. The conflicted row is refused on BOTH runs, the sixteen good
    rows are delivered on both, and neither run auto-merges."""
    first = deliver_at(world, FIRST_CLOCK)
    assert first.returncode == 1
    assert "the destination importer refused at least one row" in first.stdout

    second = deliver_at(world, SECOND_CLOCK)
    assert second.returncode == 1, "a refusal became green on the second run"
    assert "the destination importer refused at least one row" in second.stdout

    ledger = branch_ledger(world)
    delivered = [r for r in ledger if r["sourceBetKey"] != CONFLICTED_SOURCE_KEY]
    assert len(delivered) == 16, "a refusal cost valid rows"

    # The destination's existing row is untouched by either run.
    prior = [r for r in ledger if r["sourceBetKey"] == CONFLICTED_SOURCE_KEY]
    assert len(prior) == 1
    assert prior[0]["entryPrice"] == "0.41", "an existing canonical row was rewritten"

    # Nothing reached main.
    assert len(main_ledger(world)) == 1
    assert world["stub"].merged_sha is None


def test_a_stable_redelivery_still_appends_only(world):  # noqa: F811
    """Seeding from the branch must not become a way to rewrite history."""
    drop_the_conflicted_row(world)
    deliver_at(world, FIRST_CLOCK)
    before = {row["betId"]: row for row in branch_ledger(world)}

    deliver_at(world, SECOND_CLOCK)
    after = {row["betId"]: row for row in branch_ledger(world)}

    assert set(before) <= set(after), "a canonical row disappeared"
    for bet_id, row in before.items():
        assert after[bet_id] == row, f"{bet_id} was rewritten in place"


def test_the_seeded_branch_never_carries_anything_outside_data(world):  # noqa: F811
    """The containment rule still holds when the importer runs on top of the
    branch rather than on top of main."""
    drop_the_conflicted_row(world)
    deliver_at(world, FIRST_CLOCK)
    deliver_at(world, SECOND_CLOCK)

    listed = _git("ls-tree", "-r", "--name-only", f"refs/heads/{BRANCH}",
                  cwd=world["remote"]).splitlines()
    changed = _git("diff", "--name-only", "refs/heads/main", f"refs/heads/{BRANCH}",
                   cwd=world["remote"]).splitlines()
    assert LEDGER in listed
    assert [path for path in changed if path.strip()] == [LEDGER], changed


def test_the_repair_introduces_no_kalshi_write_capability():
    """13. The router is READ-ONLY against the Kalshi account, and this repair
    does not go near it: it moves git refs in a destination repository and
    never speaks to the exchange at all.

    The assertion names API SURFACE rather than English words -- a module whose
    prose explains what order decisions are made in is not a module that can
    place one, and a test that cannot tell those apart is a test nobody can
    write documentation against.
    """
    for path in ("src/kalshi_router/delivery_branch.py", "scripts/deliver_branch.py"):
        source = (ROOT / path).read_text().lower()
        for forbidden in (
            "kalshi.com", "trading-api", "/portfolio", "/trade-api",
            "create_order", "cancel_order", "createorder", "batch_orders",
            "kalshi_router.client", "kalshi_router.auth", "kalshi_router.http",
            "requests.", "urllib", "http.client",
        ):
            assert forbidden not in source, f"{path} reaches {forbidden!r}"

    # And it imports nothing that could: the only router module it uses is the
    # pure decision module and the gate's own branch-naming helper.
    script = (ROOT / "scripts/deliver_branch.py").read_text()
    imported = [line.strip() for line in script.splitlines()
                if line.strip().startswith(("import ", "from "))]
    assert not [line for line in imported if "client" in line or "auth" in line]


def test_the_delivery_step_still_drives_the_lifecycle_through_the_script():
    """The workflow must not grow a second, divergent copy of these rules."""
    body = (ROOT / ".github/workflows/deliver-wagers.yml").read_text()
    assert "scripts/deliver_branch.py" in body
    assert " seed" in body and "settle" in body
    # The decisions live in the module, not in the shell.
    for leaked in ("reuse_existing", "remote_tree", "local_tree"):
        assert leaked not in body, f"{leaked} is a decision and belongs in the module"


def test_the_branch_script_makes_no_decision_of_its_own():
    """Every rule lives in the pure module, so every rule is testable."""
    source = (ROOT / "scripts/deliver_branch.py").read_text()
    assert "delivery_branch.choose_seed" in source
    assert "delivery_branch.decide" in source
    assert "delivery_branch.uncommittable" in source
    for leaked in ("bets.jsonl", "createdAt", "ingestedAt"):
        assert leaked not in source, (
            f"{leaked!r} is ledger content and the router never writes it")
