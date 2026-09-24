#!/usr/bin/env python3
"""Drive the router's delivery branch: seed the importer, then settle its output.

`kalshi_router.delivery_branch` holds the decisions and explains why they are
what they are. This script is the hands: it reads the facts out of git, calls
those decisions, and is the only thing in the delivery path that writes to the
destination's remote.

It is invoked TWICE per destination by `deliver-wagers.yml`, around the import:

    seed    -- before the importer runs. Fetches the router's existing delivery
               branch and checks out whatever the importer should run on top of.
               Prints that commit.

    settle  -- after the importer (and the idempotency re-run) have finished.
               Stages the result, shapes it into exactly one commit on top of
               the destination's main, checks containment, and then ADOPTs the
               existing head, PUSHes a new one, or reports that there is
               NOTHING_TO_DELIVER. Prints the action.

WHY THE COMMIT IS ALWAYS RESHAPED ONTO main
-------------------------------------------
`settle` runs `git reset --soft` to the destination's main before committing, so
the branch is always exactly ONE commit whose parent is that main. Stacking
commits instead would be simpler here and wrong everywhere else: the branch tip's
parent is what `RemoteBranch.base` means, it is what a ``--depth 2`` fetch can
establish on a shallow clone, and it is what tells the next run whether the
proposal is still built on current main. A multi-commit branch would make that
unanswerable without deep history the delivery clone does not have.

WHAT IT PRINTS
--------------
One token on stdout, for the workflow to branch on. Everything a human reads
goes to stderr. Neither ever carries a ticker, a stake, a price or a row --
the same rule the rest of this repository's public logs follow.

Exit codes:
    0  the action printed on stdout was taken
    2  bad input or configuration
    3  REFUSED -- the import dirtied files outside `data/` and nothing was
       committed or pushed
    4  git refused an operation (a lost lease, a rejected push)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from kalshi_router import delivery_branch  # noqa: E402
from kalshi_router.automerge import ledger_pathspecs_for, router_branch_for  # noqa: E402
from kalshi_router.destinations import UnknownDestinationError, profile_for  # noqa: E402

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_REFUSED = 3
EXIT_GIT = 4


def log(message: str) -> None:
    """Human-readable detail. stderr, so stdout stays a single parseable token."""
    print(message, file=sys.stderr)


def git(work: str, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", work, *args],
                            capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args[:2])} failed ({result.returncode}): "
            f"{result.stderr.strip().splitlines()[-1] if result.stderr.strip() else ''}")
    return result.stdout.strip()


def git_ok(work: str, *args: str) -> bool:
    return subprocess.run(["git", "-C", work, *args],
                          capture_output=True, text=True, check=False).returncode == 0


def rev(work: str, ref: str) -> str | None:
    """A revision, or None when the ref does not resolve.

    ``--verify --quiet`` IS LOAD-BEARING and its absence cost a real wager: a
    bare `git rev-parse <missing-ref>` ECHOES ITS ARGUMENT to stdout before
    failing, so a caller that swallows the error gets the literal ref string
    back and later hands it to git as an object name. With ``--verify --quiet``
    a missing ref prints nothing and exits non-zero.
    """
    result = subprocess.run(["git", "-C", work, "rev-parse", "--verify", "--quiet", ref],
                            capture_output=True, text=True, check=False)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def read_remote_branch(work: str, branch: str) -> delivery_branch.RemoteBranch:
    """The router's delivery branch as the remote holds it, or an empty one."""
    ref = f"refs/remotes/origin/{branch}"
    head = rev(work, ref)
    if not head:
        return delivery_branch.RemoteBranch()
    return delivery_branch.RemoteBranch(
        head=head,
        base=rev(work, f"{head}^"),
        tree=rev(work, f"{head}^{{tree}}"),
    )


def ledger_moved(work: str, old: str | None, new: str, sport: str) -> bool:
    """Has the canonical wager ledger changed between two commits?

    This is the ONLY thing a proposal depends on, so it is the only thing that
    can make an existing proposal stale. `git diff` is given the exact paths
    rather than a prefix: the question is about the canonical ledger, not about
    everything the destination happens to keep under `data/`.

    Unknown means moved. If the branch's base cannot be read -- a ref that never
    fetched, a history the shallow clone does not have -- this cannot establish
    that the proposal is still current, so it rebuilds. Failing towards the
    rebuild costs a check suite; failing the other way would keep a proposal
    built against a ledger nobody looked at.
    """
    if not old:
        return True
    paths = list(ledger_pathspecs_for(sport))
    if not paths:
        # A destination with no described ledger paths is one this script
        # cannot reason about. "Moved" is the fail-closed answer: it rebuilds.
        return True
    result = subprocess.run(
        ["git", "-C", work, "diff", "--name-only", old, new, "--", *paths],
        capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return True
    return bool(result.stdout.strip())


def proposal_base(work: str, args, remote: delivery_branch.RemoteBranch):
    """(the commit this proposal sits on, whether it came from the branch).

    Recomputed identically by `seed` and `settle` from the same inputs -- the
    remote-tracking ref is local and `--base-sha` is passed in, so neither can
    move between the two calls within a run.
    """
    moved = ledger_moved(work, remote.base, args.base_sha, args.sport)
    seed, why = delivery_branch.choose_seed(args.base_sha, remote, moved)
    if why == delivery_branch.SEED_BRANCH:
        return remote.base, True, moved
    return args.base_sha, False, moved


def cmd_seed(args) -> int:
    work, branch, base_sha = args.work, args.branch, args.base_sha

    # --depth 2, not 1: the decision below needs the branch tip's PARENT to know
    # which `main` that tip was built from. A --depth 1 clone is single-branch,
    # so without this fetch there is no remote-tracking ref for the router's own
    # branch at all.
    git(work, "fetch", "-q", "--depth", "2", "origin",
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}", check=False)

    remote = read_remote_branch(work, branch)
    moved = ledger_moved(work, remote.base, base_sha, args.sport)
    seed, why = delivery_branch.choose_seed(base_sha, remote, moved)

    if why == delivery_branch.SEED_BRANCH:
        log(f"  seeding the importer from {branch} ({seed[:12]}): the canonical "
            f"ledger has not moved since that proposal was built, so rows it "
            f"already proposed come back DUPLICATE_NOOP and keep their original "
            f"canonical bytes")
        if remote.base != base_sha:
            log(f"  (the destination's main has moved to {base_sha[:12]} since, but "
                f"only in files this proposal does not depend on -- the pull "
                f"request's own diff is still what the gate reads and what merging "
                f"would apply)")
    elif remote.exists:
        log(f"  seeding the importer from the destination's main ({seed[:12]}): the "
            f"canonical ledger moved since {branch} was built on "
            f"{(remote.base or 'nothing')[:12]}, so the batch is reconciled against "
            f"what is there now")
    else:
        log(f"  seeding the importer from the destination's main ({seed[:12]}): "
            f"{branch} does not exist yet")

    git(work, "checkout", "-q", "-B", branch, seed)
    print(seed)
    return EXIT_OK


def cmd_settle(args) -> int:
    work, branch, base_sha = args.work, args.branch, args.base_sha

    if rev(work, base_sha) is None:
        log(f"  the base commit {base_sha} is not present in this clone")
        return EXIT_CONFIG

    remote_before = read_remote_branch(work, branch)
    base, seeded_from_branch, _moved = proposal_base(work, args, remote_before)

    git(work, "add", "-A")

    # Reshape onto the commit this proposal sits on BEFORE inspecting: `--cached`
    # then answers "what would this pull request change", which is the question
    # both the containment check and the merge gate actually ask. Keeping the
    # branch at exactly ONE commit is what makes its parent readable as its base
    # by the next run, from a --depth 2 fetch and no deeper history.
    git(work, "reset", "--soft", "-q", base)

    staged = [line for line in git(work, "diff", "--cached", "--name-only").splitlines()]
    try:
        prefixes = profile_for(args.sport).committable_prefixes
    except UnknownDestinationError as exc:
        log(f"  {exc}")
        return EXIT_CONFIG
    unexpected = delivery_branch.uncommittable(staged, prefixes)
    if unexpected:
        log(f"  the import dirtied files outside {list(prefixes)} -- refusing to commit them:")
        for path in unexpected:
            log(f"    {path}")
        return EXIT_REFUSED

    resulting_tree = git(work, "write-tree")
    base_tree = git(work, "rev-parse", f"{base}^{{tree}}")
    remote = read_remote_branch(work, branch)

    action, why = delivery_branch.decide(
        base_tree, resulting_tree, remote, seeded_from_branch)
    log(f"  {why}")

    if action == delivery_branch.NOTHING_TO_DELIVER:
        print(action)
        return EXIT_OK

    if action == delivery_branch.ADOPT:
        # Adopt the commit that is actually on the branch, so everything
        # downstream -- the merge gate's diff, its head check -- reasons about
        # the head that would merge rather than an equivalent local rebuild.
        git(work, "checkout", "-q", "-B", branch, remote.head)
        log(f"  destination commit: {remote.head} (unchanged, not pushed)")
        print(action)
        return EXIT_OK

    if args.dry_run:
        log("  DRY RUN: the ledger changed and nothing was committed or pushed.")
        print(action)
        return EXIT_OK

    git(work, "commit", "-q", "-m", args.message)
    head = git(work, "rev-parse", "HEAD")

    # ONLY onto this router-owned branch: it is rebuilt from main every run, so
    # its previous tip is this job's own earlier output.
    #
    # The lease needs an EXPLICIT expected value. A shallow single-branch clone
    # has no upstream configured for this local branch, so a bare
    # --force-with-lease has nothing to form a lease against and git rejects the
    # push as "stale info". An EMPTY expectation means "the branch must not
    # exist yet", which is the first run. The lease still refuses if anything
    # other than this router moved the branch.
    expected = remote.head or ""
    if not git_ok(work, "push", "-q",
                  f"--force-with-lease=refs/heads/{branch}:{expected}",
                  "origin", f"HEAD:refs/heads/{branch}"):
        log(f"  the push to {branch} was refused. Nothing was delivered this run; "
            f"the branch still holds {(expected or 'nothing')[:12]}.")
        return EXIT_GIT

    log(f"  destination commit: {head} (pushed)")
    print(action)
    return EXIT_OK


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", required=True,
                        help="the destination clone this delivery runs in")
    parser.add_argument("--sport", default=None,
                        help="the sport whose delivery branch this is; the branch "
                             "name is derived from it, never supplied directly, so "
                             "no caller can push outside the router's namespace")
    parser.add_argument("--base-sha", required=True,
                        help="the destination main this delivery is built on")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="check out what the importer should run on top of")
    seed.set_defaults(handler=cmd_seed)

    settle = sub.add_parser("settle", help="adopt, push, or report nothing to deliver")
    settle.add_argument("--message", required=True, help="the commit message")
    settle.add_argument("--dry-run", action="store_true",
                        help="decide and report; never commit and never push")
    settle.set_defaults(handler=cmd_settle)

    args = parser.parse_args(argv)

    if not args.sport:
        print("--sport is required", file=sys.stderr)
        return EXIT_CONFIG
    # DERIVED, never accepted from the caller: `router_branch_for` is the same
    # function the merge gate recognises branches by, so the two cannot drift
    # and no argument can name a branch outside `kalshi-router/`.
    args.branch = router_branch_for(args.sport)

    if not os.path.isdir(os.path.join(args.work, ".git")):
        print(f"{args.work} is not a git clone", file=sys.stderr)
        return EXIT_CONFIG

    try:
        return args.handler(args)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_GIT


if __name__ == "__main__":
    sys.exit(main())
