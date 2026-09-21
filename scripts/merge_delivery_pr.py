#!/usr/bin/env python3
"""Finish a delivery: evaluate the auto-merge gate and, if it passes, land it.

WHAT THIS CLOSES
----------------
`deliver-wagers.yml` hands a payload to the destination's own importer, commits
what the importer wrote, pushes it to the router's long-lived branch and opens a
pull request. Until now that was the end. `chmoses98/edge-finder-api#218` --
8 MLB wagers from 2026-09-18, clean, mergeable, CI green -- sat open for three
days while the destination's own daily report said `Placed bets: 0`.

A wager that reaches a branch and stops is not recorded. This script is the
missing last step.

THE DECISION IS NOT MADE HERE
-----------------------------
`kalshi_router.automerge.evaluate` is a pure function over facts and holds the
entire policy. This script only:

  * gathers the facts (receipts on disk, the local clone, the GitHub API);
  * prints the verdict, condition by condition;
  * calls the merge endpoint when -- and only when -- the verdict is MERGE.

The split is the point: every rule can be tested directly, and this file cannot
quietly acquire a rule of its own.

WHAT IT PRINTS
--------------
Condition names and counts. Never a ticker, a stake, a price or a row. That is
the same rule the rest of this repository's public logs follow, and the gate
needs nothing more to explain itself.

Exit codes:
    0  MERGE (merged) or WAIT (nothing to do yet -- the next run is the fix)
    2  bad input or configuration
    3  REFUSE -- a condition failed and a human is needed
    4  the GitHub API refused an operation
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from kalshi_router import automerge
from kalshi_router.destinations import profile_for  # noqa: E402
from kalshi_router.receipts import normalise  # noqa: E402
from kalshi_router.destination import destination_repo_for  # noqa: E402

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_REFUSED = 3
EXIT_API = 4

API_ROOT = os.environ.get("GITHUB_API_ROOT", "https://api.github.com")

#: How long to let GitHub finish computing `mergeable`. It is computed lazily
#: on first read, so the first response is very often `null` and the second is
#: not. This is a few seconds of politeness, not a wait for CI -- CI is waited
#: for across RUNS, not inside one.
MERGEABILITY_ATTEMPTS = 4
MERGEABILITY_PAUSE_SECONDS = 3


def _request(method, url, token, payload=None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
        return response.status, (json.loads(raw) if raw else {})


def _get(url, token, default=None):
    try:
        _status, payload = _request("GET", url, token)
        return payload
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return default
        raise


def _load_json(path, default=None):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _git(work, *args):
    result = subprocess.run(["git", "-C", work, *args],
                            capture_output=True, text=True, check=False)
    return result.returncode, result.stdout


def ledger_diff(work, base_ref, ledger_paths):
    """
    (added rows, removed raw lines) between the destination's base branch and
    this clone's HEAD, for the canonical ledger.

    Read from GIT rather than from the API: this run built the commit, so the
    local diff is the same bytes that would merge, needs no extra API call, and
    cannot be truncated by a large-diff response.

    A line that fails to parse as JSON is NOT skipped -- it is returned as a
    row with no identity, which the gate then refuses. Skipping it would be the
    one way an unparseable ledger line could slip through.
    """
    if not ledger_paths:
        return None, None
    status, out = _git(work, "diff", "--unified=0", base_ref, "HEAD", "--", *ledger_paths)
    if status != 0:
        return None, None
    added, removed = [], []
    for line in out.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            payload = line[1:].strip()
            if not payload:
                continue
            try:
                added.append(json.loads(payload))
            except ValueError:
                added.append({"_unparseable": True})
        elif line.startswith("-"):
            payload = line[1:].strip()
            if payload:
                removed.append(payload)
    return added, removed


def changed_files(work, base_ref):
    status, out = _git(work, "diff", "--name-only", base_ref, "HEAD")
    if status != 0:
        return ()
    return tuple(line.strip() for line in out.splitlines() if line.strip())


def find_pull_request(repo, branch, token):
    rows = _get(f"{API_ROOT}/repos/{repo}/pulls"
                f"?state=open&head={repo.split('/')[0]}:{branch}&per_page=10",
                token, default=[]) or []
    for row in rows:
        if (row.get("head") or {}).get("ref") == branch:
            return row
    return None


def pull_request_detail(repo, number, token):
    """
    The pull request, re-read until `mergeable` stops being null.

    GitHub computes mergeability in the background on first request. Reading it
    once and treating null as "not mergeable" would make the gate WAIT forever
    on a repository nobody else is touching.
    """
    detail = None
    for attempt in range(MERGEABILITY_ATTEMPTS):
        detail = _get(f"{API_ROOT}/repos/{repo}/pulls/{number}", token)
        if detail is None or detail.get("mergeable") is not None:
            return detail
        if attempt + 1 < MERGEABILITY_ATTEMPTS:
            time.sleep(MERGEABILITY_PAUSE_SECONDS)
    return detail


def check_runs_for(repo, sha, token):
    payload = _get(f"{API_ROOT}/repos/{repo}/commits/{sha}/check-runs?per_page=100",
                   token, default={}) or {}
    runs = [
        (run.get("name"), run.get("status"), run.get("conclusion"))
        for run in payload.get("check_runs") or []
    ]
    # A repository can also report legacy commit STATUSES, which are not check
    # runs and are invisible to the endpoint above. Folding them in means a
    # red status cannot be merged past just because it took the old shape.
    statuses = _get(f"{API_ROOT}/repos/{repo}/commits/{sha}/status", token, default={}) or {}
    for status in statuses.get("statuses") or []:
        state = status.get("state")
        runs.append((
            status.get("context"),
            "completed" if state in ("success", "failure", "error") else "in_progress",
            {"success": "success", "failure": "failure", "error": "failure"}.get(state),
        ))
    return tuple(runs)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sport", required=True)
    parser.add_argument("--work", required=True,
                        help="the destination clone this run built the commit in")
    parser.add_argument("--receipts", required=True,
                        help="receipts from this run's import")
    parser.add_argument("--rerun-receipts", default=None,
                        help="receipts from an immediate second import of the same payload")
    parser.add_argument("--rerun-changed-tree", default=None, choices=["true", "false"],
                        help="whether that second import changed the working tree")
    parser.add_argument("--partial", default="false", choices=["true", "false"],
                        help="whether the importer refused at least one row this run")
    parser.add_argument("--base-ref", default="origin/main",
                        help="the destination commit this delivery was built on. A "
                             "two-dot diff against it -- not a three-dot merge-base "
                             "diff -- because the branch is rebuilt from that exact "
                             "commit every run, and a shallow clone has no history to "
                             "compute a merge base from anyway.")
    parser.add_argument(
        "--validator-passed", default=None, choices=["true", "false"],
        help=(
            "the verdict of the DESTINATION'S OWN ledger validator, for a destination whose "
            "ledger branch runs no CI. Absent means it has not reported, and the gate waits "
            "rather than merging."
        ),
    )
    parser.add_argument(
        "--branch-kind", default=automerge.WAGERS, choices=list(automerge.KINDS),
        help=(
            "whether this proposal carries wagers or settlements. They are separate "
            "branches with separate lifecycles; evaluating the wrong one would read a "
            "different commit than the one that would merge."
        ),
    )
    parser.add_argument("--token-env", default="DOWNSTREAM_REPO_TOKEN")
    parser.add_argument("--merge-method", default="squash",
                        choices=["merge", "squash", "rebase"])
    parser.add_argument("--dry-run", action="store_true",
                        help="evaluate and report; never call the merge endpoint")
    args = parser.parse_args(argv)

    repo = destination_repo_for(args.sport)
    if not repo:
        print(f"{args.sport} has no configured destination repository", file=sys.stderr)
        return EXIT_CONFIG

    token = (os.environ.get(args.token_env) or "").strip()
    if not token:
        print(f"{args.token_env} is empty", file=sys.stderr)
        return EXIT_CONFIG

    branch = automerge.router_branch_for(args.sport, args.branch_kind)
    # EVERY mergeable path, not the first one alphabetically. CFB's ledger is
    # one file per season and one per kind, so a diff over `sorted(...)[0]`
    # would read `settlements/2024.jsonl` and report that a delivery of 2026
    # wagers changed nothing -- which makes the append-only check vacuous and
    # the identity check WAIT forever.
    ledger_paths = sorted(automerge.mergeable_paths_for(args.sport))

    _status, head_sha = _git(args.work, "rev-parse", "HEAD")
    verified_sha = head_sha.strip() or None
    added, removed = ledger_diff(args.work, args.base_ref, ledger_paths)

    try:
        pull = find_pull_request(repo, branch, token)
        detail = pull_request_detail(repo, pull["number"], token) if pull else None
        head = (detail or {}).get("head") or {}
        checks = check_runs_for(repo, head.get("sha"), token) if head.get("sha") else ()
    except urllib.error.HTTPError as exc:
        print(f"the GitHub API refused a read (HTTP {exc.code})", file=sys.stderr)
        return EXIT_API
    except urllib.error.URLError as exc:
        print(f"could not reach the GitHub API: {type(exc).__name__}", file=sys.stderr)
        return EXIT_API

    facts = automerge.MergeFacts(
        sport=args.sport,
        destination_repo=repo,
        pull_number=(detail or {}).get("number"),
        state=(detail or {}).get("state"),
        draft=(detail or {}).get("draft"),
        head_ref=head.get("ref"),
        head_sha=head.get("sha"),
        head_repo=((head.get("repo") or {}).get("full_name")),
        base_ref=((detail or {}).get("base") or {}).get("ref"),
        mergeable=(detail or {}).get("mergeable"),
        mergeable_state=(detail or {}).get("mergeable_state"),
        verified_sha=verified_sha,
        changed_files=changed_files(args.work, args.base_ref),
        added_ledger_rows=tuple(added or ()),
        removed_ledger_rows=tuple(removed or ()),
        # NORMALISED at the boundary, so the gate reasons about one shape.
        # MLB writes a list of camelCase rows; CFB writes an object. Handing
        # the object straight through would iterate its KEYS and produce
        # receipts that are strings.
        receipts=tuple(r.as_dict() for r in normalise(_load_json(args.receipts, default=[]))),
        rerun_receipts=tuple(
            r.as_dict() for r in normalise(_load_json(args.rerun_receipts, default=[]))
        ),
        rerun_changed_the_tree=(None if args.rerun_changed_tree is None
                                else args.rerun_changed_tree == "true"),
        check_runs=checks,
        destination_validator_passed=(
            None if args.validator_passed is None else args.validator_passed == "true"
        ),
        branch_kind=args.branch_kind,
        partial_delivery=args.partial == "true",
    )

    verdict = automerge.evaluate(facts)
    print(f"merge gate [{args.sport} -> {repo} #{facts.pull_number}]")
    print(verdict.render())

    if verdict.verdict == automerge.REFUSE:
        return EXIT_REFUSED
    if verdict.verdict == automerge.WAIT:
        print("  nothing merged this run; the next scheduled run re-evaluates.")
        return EXIT_OK

    # PASSED, but this destination is still being watched. The gate ran, its
    # verdict is printed above, the rows are delivered and the pull request is
    # open -- a person merges it. This is not a refusal and not an error: a
    # destination whose real deliveries nobody has yet read should not close
    # the loop over itself on the strength of a dry run.
    if not profile_for(args.sport).auto_merge:
        print(
            f"  HELD FOR OBSERVATION: {args.sport} passed every gate condition and was "
            f"NOT merged. The rows are delivered and the pull request is open for review."
        )
        print(
            "  Set auto_merge=True in that destination's DestinationProfile once the "
            "first real deliveries have been read."
        )
        return EXIT_OK

    if args.dry_run:
        print("  DRY RUN: the gate passed and nothing was merged.")
        return EXIT_OK

    try:
        status, body = _request(
            "PUT", f"{API_ROOT}/repos/{repo}/pulls/{facts.pull_number}/merge", token,
            {
                "merge_method": args.merge_method,
                # Pinned to the exact commit every condition above was checked
                # against. If anything moved the branch between the evaluation
                # and this call, GitHub returns 409 and merges nothing -- which
                # is the correct outcome, not a race this script has to win.
                "sha": facts.head_sha,
                "commit_title": f"Record Kalshi wagers ({args.sport}) (#{facts.pull_number})",
                "commit_message": (
                    "Merged by the router's deterministic auto-merge gate. Every "
                    "condition in kalshi_router.automerge.CONDITIONS passed on this "
                    "commit: the importer refused nothing, the import is idempotent, "
                    "the diff appends only to the canonical wager ledger, and the "
                    "destination's CI is green."
                ),
            },
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            print("  the branch moved between evaluation and merge; nothing was "
                  "merged. The next run re-evaluates.")
            return EXIT_OK
        print(f"could not merge (HTTP {exc.code})", file=sys.stderr)
        return EXIT_API
    except urllib.error.URLError as exc:
        print(f"could not reach the GitHub API: {type(exc).__name__}", file=sys.stderr)
        return EXIT_API

    if not body.get("merged"):
        print(f"the merge endpoint returned HTTP {status} without merging: "
              f"{body.get('message')}", file=sys.stderr)
        return EXIT_API

    print(f"  MERGED #{facts.pull_number} into {repo}@main "
          f"({(body.get('sha') or '')[:12]})")
    print(f"  rows landed on main: {len(facts.added_ledger_rows)}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
