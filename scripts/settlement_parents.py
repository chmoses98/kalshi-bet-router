#!/usr/bin/env python3
"""Split one destination's settlement payload by the state of each row's PARENT WAGER. Counts only.

    python scripts/settlement_parents.py --sport NHL --work "$work" --payload NHL-settlements.json \
        --ready-out ready.json --waiting-out waiting.json

Run by `settle-wagers.yml` after the ledger clone and BEFORE the destination's settlement importer. The policy is
`kalshi_router.settlement_parents` (pure, tested); this script only gathers its facts:

  * the canonical WAGER ledger's source keys, from `refs/remotes/origin/<ledger_branch>` (the workflow fetched it);
  * the router's wager proposal: `kalshi-router/<SPORT>` fetched from the destination, and the wager records on
    the commit it points at;
  * the pull request GitHub reports for that branch (DOWNSTREAM_REPO_TOKEN, read only).

Writes `--ready-out` (the payload envelope, holding the rows the importer should see: canonical parents AND rows
with no valid parent, which it refuses) and `--waiting-out` (`{"waiting_for_parent_wager": [source keys]}`), both
into RUNNER_TEMP. Prints counts and reason codes, plus one `ROUTER_SETTLEMENT_PARENTS_JSON=` line. Never a key, a
ticker, a payout or a stake.

Exit codes: 0 the split was written; 2 the inputs could not be read. On 2 the workflow hands the FULL payload to the
importer, which refuses every orphan -- the behaviour before this script existed, and fail-closed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from kalshi_router import automerge  # noqa: E402
from kalshi_router.destinations import UnknownDestinationError, profile_for  # noqa: E402
from kalshi_router.settlement_parents import Proposal, partition  # noqa: E402

import merge_delivery_pr as gate  # noqa: E402
import reconcile_delivery as ledger  # noqa: E402


def _git(work, *args):
    return subprocess.run(["git", "-C", work, *args], capture_output=True, text=True, check=False)


def read_proposal(work: str, repo: str, sport: str, profile, token: str | None) -> Proposal | None:
    """The router's wager proposal, or None when its branch does not exist on the destination."""
    branch = automerge.router_branch_for(sport, automerge.WAGERS)
    ref = f"refs/remotes/origin/settlement-parents-{sport}"
    if _git(work, "fetch", "-q", "--depth", "1", "origin", f"+refs/heads/{branch}:{ref}").returncode != 0:
        return None
    sha = _git(work, "rev-parse", "--verify", "--quiet", ref).stdout.strip() or None
    if sha is None:
        return None
    records = tuple(ledger.ledger_records(work, ref, profile, "wagers"))
    if not token:
        return Proposal(branch=branch, fetched_sha=sha, pull=None, records=records,
                        unverifiable="no token to read the pull request")
    try:
        pull = gate.find_pull_request(repo, branch, token)
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        return Proposal(branch=branch, fetched_sha=sha, pull=None, records=records,
                        unverifiable=type(exc).__name__)
    return Proposal(branch=branch, fetched_sha=sha, pull=pull, records=records)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sport", required=True)
    ap.add_argument("--work", required=True, help="the destination ledger clone")
    ap.add_argument("--payload", required=True)
    ap.add_argument("--ready-out", required=True)
    ap.add_argument("--waiting-out", required=True)
    ap.add_argument("--ledger-ref", default=None,
                    help="the canonical ledger ref (default refs/remotes/origin/<ledger_branch>)")
    ap.add_argument("--token-env", default="DOWNSTREAM_REPO_TOKEN")
    a = ap.parse_args(argv)
    try:
        profile = profile_for(a.sport)
        with open(a.payload, encoding="utf-8") as handle:
            payload = json.load(handle)
        rows = payload.get("settlements")
        if not isinstance(rows, list):
            raise ValueError("the payload holds no settlements list")
        canonical = ledger.ledger_keys(a.work, a.ledger_ref or f"refs/remotes/origin/{profile.ledger_branch}",
                                       profile, "wagers")
    except (UnknownDestinationError, OSError, ValueError, RuntimeError) as exc:
        print(f"  settlement parents could not be checked ({type(exc).__name__})", file=sys.stderr)
        return 2

    token = (os.environ.get(a.token_env) or "").strip() or None
    proposal = read_proposal(a.work, profile.repo, a.sport, profile, token)
    split = partition(rows, canonical, proposal, repo=profile.repo, ledger_branch=profile.ledger_branch)

    ready = dict(payload)
    ready["settlements"] = split.ready
    with open(a.ready_out, "w", encoding="utf-8") as handle:
        json.dump(ready, handle, indent=2, sort_keys=True)
        handle.write("\n")
    with open(a.waiting_out, "w", encoding="utf-8") as handle:
        json.dump({"waiting_for_parent_wager": split.waiting_keys()}, handle, indent=2)
        handle.write("\n")

    summary = split.summary()
    pull_number = (proposal.pull or {}).get("number") if proposal is not None else None
    print(f"  settlement parents ({a.sport}, {len(rows)} settled row(s)): canonical {summary['canonical_parent']}, "
          f"WAITING_FOR_PARENT_WAGER {summary['waiting_for_parent_wager']}"
          + (f" (wagers on open proposal #{pull_number}, not yet merged; withheld, re-offered next run)"
             if summary["waiting_for_parent_wager"] else "")
          + f", no valid parent {summary['no_valid_parent']}"
          + (f" {summary['no_valid_parent_reasons']}" if summary["no_valid_parent"] else ""))
    print("ROUTER_SETTLEMENT_PARENTS_JSON=" + json.dumps(
        {"sport": a.sport, "settled_rows": len(rows), "proposal_pull_request": pull_number, **summary},
        sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
