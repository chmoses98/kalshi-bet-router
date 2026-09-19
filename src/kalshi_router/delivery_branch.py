"""Where the destination's importer runs, and whether its output is a new proposal.

WHY THIS EXISTS
---------------
`deliver-wagers.yml` rebuilt the delivery branch from the destination's `main`
on every run, re-ran the importer, and compared the resulting tree against the
branch already on the remote. If they matched it kept the existing commit --
precisely so a repeated, unchanged delivery would not restart the destination's
CI. That check is right, and on 2026-09-19 it never once fired.

The reason is in the ledger, not in the comparison. Seeded from `main`, the 24
undelivered wagers are absent, so the importer writes all 24 as `NEW`, and
`lib.edgelab.bets.build_manual_bet_record` stamps every new row with
``ids.utc_now_iso()``::

    run 35465098715   createdAt/recordedAt/provenance.ingestedAt = 19:50:03Z
    run 35466474703   createdAt/recordedAt/provenance.ingestedAt = 20:17:33Z

Same base commit ``067c321e``, same 41-row payload, same 41 canonical bet ids,
and two different trees -- ``a9591950`` and ``de4b2511`` -- differing in exactly
those three fields on exactly those 24 rows. A different tree is a different
commit, a different commit is a force-push, and a force-push restarts a ~19
minute check suite on a 15 minute cadence. The head was therefore almost never a
commit with a finished check suite, and a gate that requires green CI could
never fire. PR #218 churned this way for three days.

THE FIX IS WHERE THE IMPORTER RUNS, NOT WHAT IT WRITES
------------------------------------------------------
Those three fields are not noise to be stripped and not values to be faked.
``createdAt`` really is when the canonical record was created and
``provenance.ingestedAt`` really is when it was ingested -- the row genuinely
was first ingested at 19:50:03Z. The defect is that the second run asked the
question again from scratch instead of recognising that it had already been
answered, and the answer was sitting on the router's own open delivery branch.

So this module changes the SEED: when the router's branch already carries a
proposal built on the destination's current `main`, the importer runs on top of
that branch rather than on top of bare `main`. Every already-proposed row is
then a `DUPLICATE_NOOP`, and `write_placed_bet`'s own contract -- "nothing is
written, the ALREADY-STORED row is returned in the receipt" -- preserves its
original bytes, timestamps included. Only a genuinely new wager appends.

That is option (B), preserving the originally proposed canonical ingestion
metadata, implemented WITHOUT the router ever editing a ledger file: the
destination's importer remains the only thing that writes its ledger, and it
reaches the same conclusion it always would have. The destination already holds
this exact principle for its other entry point -- `_content_fingerprint`
excludes `createdAt`/`recordedAt`/`provenance.ingestedAt` so "a rerun against an
unchanged legacy ledger is a true no-op, not a timestamp-only diff on every row
every day". The canonical import path simply never had a caller that let it
apply.

WHAT THIS MODULE IS
-------------------
Pure. Two decisions and a containment rule, over facts a caller reads from git.
It runs no command, holds no credential and cannot push. `scripts/deliver_branch.py`
reads the facts, acts on the decisions, and is the only thing that touches the
remote. Everything here is therefore testable directly, which is the point: the
production failure above is a two-line state (`remote.base == base_sha` and
`remote.tree == resulting_tree`) that no test ever reached because the tree
could not be equal.

NOTHING HERE IS SPORT-SPECIFIC
------------------------------
No sport is named in this module and nothing here is shaped around one. The
delivery lifecycle is one mechanism applied to every destination in
`destination.DESTINATION_REPOS`, so a sport added there inherits the repaired
lifecycle rather than a copy of it.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The only path prefix a router-authored commit may touch in the destination.
#:
#: This is a CONTAINMENT rule, and deliberately broader than
#: `automerge.MERGEABLE_PATHS`, which is what may be MERGED without a human.
#: The importer is allowed to write anywhere under `data/`; landing it on main
#: unread is a separate, narrower permission. Both must hold to merge.
COMMITTABLE_PREFIX = "data/"

#: The importer runs on top of the destination's `main`.
SEED_MAIN = "SEED_MAIN"
#: The importer runs on top of the router's existing delivery branch.
SEED_BRANCH = "SEED_BRANCH"

#: The importer's output matches `main`: every row is already recorded.
NOTHING_TO_DELIVER = "NOTHING_TO_DELIVER"
#: The branch already carries exactly this proposal on exactly this base.
#: Keep its commit, let its CI finish, and evaluate the head that is there.
ADOPT = "ADOPT"
#: This is a genuinely different proposal. It needs a commit and a push, and
#: the destination's CI is expected -- and required -- to run again.
PUSH = "PUSH"


@dataclass(frozen=True)
class RemoteBranch:
    """The router's delivery branch as the remote currently holds it.

    `base` is the branch tip's PARENT. The router keeps the branch at exactly
    one commit on top of the destination's `main`, so the parent *is* the base
    the proposal was built against -- which is why a shallow ``--depth 2`` fetch
    is enough to establish it, and why `scripts/deliver_branch.py` rebuilds
    rather than stacking commits.
    """

    head: str | None = None
    base: str | None = None
    tree: str | None = None

    @property
    def exists(self) -> bool:
        return bool(self.head)

    def is_built_on(self, base_sha: str) -> bool:
        """Does this branch propose against exactly `base_sha`?

        Both halves matter. A branch whose parent is some OTHER commit may hold
        an identical tree and still be a different proposal, because its diff
        against the destination's current main is something no run inspected.
        """
        return bool(self.head) and bool(base_sha) and self.base == base_sha


def choose_seed(base_sha: str, remote: RemoteBranch) -> tuple[str, str]:
    """(commit the importer should run on top of, why).

    THE WHOLE DETERMINISM FIX IS THIS FUNCTION.

    Seeding from the branch is safe precisely because it is not a shortcut: the
    importer still runs, still resolves tickers, still detects duplicates and
    conflicts, and still has the final say on every row. It is simply shown the
    rows it already proposed, so it can recognise them instead of minting them
    again with a fresh clock reading.

    Seeding from `main` is the answer whenever the branch is NOT built on the
    destination's current main -- main moved, so the old proposal's diff is
    stale and the batch must be reconciled against what is there now. That
    rebuild legitimately produces a new tree and legitimately restarts CI;
    determinism is promised for a fixed base, not across a moving one.
    """
    if remote.is_built_on(base_sha):
        return remote.head, SEED_BRANCH
    return base_sha, SEED_MAIN


def decide(base_sha: str, base_tree: str, resulting_tree: str,
           remote: RemoteBranch) -> tuple[str, str]:
    """(what to do with the importer's output, why).

    Order matters. "Everything is already on main" is checked FIRST, because a
    branch that still exists while its rows have landed must not be re-proposed
    -- and because a tree equal to main's is not a delivery at all.
    """
    if resulting_tree == base_tree:
        return NOTHING_TO_DELIVER, (
            "the importer's output is identical to the destination's main; "
            "every row is already recorded")
    if remote.is_built_on(base_sha) and remote.tree == resulting_tree:
        return ADOPT, (
            f"{remote.head[:12]} already carries this exact tree on this exact "
            "base; keeping it so its checks can finish")
    if remote.exists and not remote.is_built_on(base_sha):
        return PUSH, (
            "the existing branch was built on a different base; rebuilding the "
            "proposal against the destination's current main")
    if remote.exists:
        return PUSH, "the proposal changed on this base; a new commit is required"
    return PUSH, "no delivery branch exists yet"


def uncommittable(paths) -> tuple[str, ...]:
    """Staged paths a router-authored commit may NOT contain.

    `git add -A` is only safe because the destination's own `.gitignore` covers
    what the importer leaves behind. That is a property of THEIR repository and
    it can change without anyone telling this router, so what actually got
    staged is checked rather than assumed. A `.pyc` or a lock file appearing
    here means something moved upstream, and stopping beats quietly committing
    it every fifteen minutes.
    """
    return tuple(sorted(
        path for path in paths
        if path.strip() and not path.startswith(COMMITTABLE_PREFIX)
    ))
