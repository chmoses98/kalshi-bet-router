"""THE 2026-09-16 PARTIAL-DELIVERY INCIDENT, REPRODUCED AGAINST THE REAL WORKFLOW.

A scheduled production run built 17 eligible MLB wagers. The destination's
own importer wrote 16 of them as NEW and refused the 17th as CONFLICT,
exiting 1 as its contract says it does. The delivery loop read that exit
code and `continue`d -- discarding the entire clone, all 16 successful
canonical writes included. No commit, no branch, no pull request, and a
postmortem the next morning that could not see the night's bets.

These tests do not re-describe the fix in a mock. They EXTRACT THE COMMITTED
BASH out of `.github/workflows/deliver-wagers.yml` and run it, so the thing
under test is the text that production executes -- if someone reinstates the
`continue`, these fail.

Only the step's external world is substituted, and only where a test cannot
reach the real one:

  * github.com is redirected to a local bare repository via git's
    `insteadOf`, so `git clone` and `git push` are real git against a real
    remote -- the branch, the lease and the pushed tree are all genuine.
  * `scripts/edgelab/import_bet_batch.py` inside that destination is a
    stand-in reproducing the contract the real one documents (see
    FakeImporter below). The real importer needs the whole edge-finder-api
    dependency tree, which the router does not have.
  * `curl` is a PATH shim that answers the pull-request POST, because the
    test must not talk to api.github.com.

Everything else -- the loop, the refusal branch, the emptiness check, the
`git add -A` containment check, the lease, the push, the summary lines and
the exit status -- is production's own code.
"""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from kalshi_router.destination import ROUTER_IMPORT_BATCH_ID
from tests.github_api_stub import GitHubStub

ROOT = Path(__file__).resolve().parents[1]
DELIVER = ROOT / ".github/workflows/deliver-wagers.yml"

DESTINATION = "chmoses98/edge-finder-api"
LEDGER = "data/edgelab/bets/bets.jsonl"
# The PRODUCTION constant, not a dated label. The batch id is part of a row's
# primary key (docs/DELIVERY.md), so the real 2026-09-16 payload carried this
# exact value -- and the auto-merge gate refuses any added row that does not.
IMPORT_BATCH_ID = ROUTER_IMPORT_BATCH_ID

# The conflicted row's identity already exists in the destination, with
# DIFFERENT economics. That is what makes it a CONFLICT -- the fake importer
# has no notion of "row 17", it just compares against the ledger it is given.
CONFLICTED_SOURCE_KEY = "kalshi-order-0017"


FAKE_IMPORTER = '''#!/usr/bin/env python3
"""Stand-in for this repository's own import_bet_batch.py.

Reproduces the contract the real one documents verbatim:

    "Exit codes: 0 if every row wrote successfully (NEW/DUPLICATE_NOOP/
    CORRECTED); 1 if any row failed (unresolved ticker, schema-invalid,
    unresolved conflict) -- the full per-row receipt list is still always
    printed/written so a partially-successful batch is never silently lossy."

Rows are persisted ONE AT A TIME, exactly like the real process_row ->
write_placed_bet path, so a non-zero exit leaves earlier writes on disk.
Identity is hash(importBatchId, sourceBetKey, marketTicker, side), which is
the real build_bet_id -- no economics participate, so a row whose price or
fee changed keeps its identity and lands as a CONFLICT rather than a second
wager.

IT ALSO STAMPS THE CLOCK, AND THAT IS NOT DECORATION.
-----------------------------------------------------
The real `lib.edgelab.bets.build_manual_bet_record` sets `createdAt`,
`recordedAt` and `provenance.ingestedAt` to `ids.utc_now_iso()` on every NEW
row. This stand-in did not, and that omission is why the 2026-09-19 incident
reached production with a green test suite: the branch-reuse check added in
#78 was correct, and `test_identical_content_does_not_produce_a_new_commit_
every_run` passed because a deterministic fake importer produced an identical
tree on the second run. The real importer never could. So the fake stamps the
clock too, and `FAKE_IMPORT_CLOCK` lets a test advance it the way wall time
advances between two scheduled runs.

The two rules that make re-delivery survivable are the real ones, and both
are modelled here:

  * a DUPLICATE_NOOP writes NOTHING and returns the ALREADY-STORED row, so an
    existing row's timestamps are preserved byte for byte
    (`write_placed_bet`: "nothing is written, the ALREADY-STORED row is
    returned in the receipt");
  * a CONFLICT is decided on economics alone, never on a timestamp -- the
    real `_content_fingerprint` pops createdAt/updatedAt/recordedAt and
    provenance.ingestedAt before comparing, precisely so a clock tick is not
    a content change.
"""
import argparse, datetime, hashlib, json, os, sys

LEDGER = "data/edgelab/bets/bets.jsonl"
ECONOMICS = ("contracts", "entryPrice", "exchangeFee", "stake")


def bet_id(batch, row):
    parts = [batch, row["sourceBetKey"], row["marketTicker"], row["side"]]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:40]


def now_iso():
    """The destination's ids.utc_now_iso(), or whatever a test pinned it to."""
    pinned = os.environ.get("FAKE_IMPORT_CLOCK")
    if pinned:
        return pinned
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--file")
    g.add_argument("--json")
    ap.add_argument("--receipts-out")
    args = ap.parse_args()

    payload = json.loads(open(args.file).read() if args.file else args.json)
    batch, rows = payload["importBatchId"], payload["rows"]

    existing = {}
    if os.path.exists(LEDGER):
        for line in open(LEDGER):
            if line.strip():
                r = json.loads(line)
                existing[r["betId"]] = r

    receipts = []
    for index, row in enumerate(rows):
        ident = bet_id(batch, row)
        prior = existing.get(ident)
        if prior is None:
            stamped = now_iso()
            record = dict(row, betId=ident, importBatchId=batch,
                          createdAt=stamped, recordedAt=stamped,
                          provenance={"sourceSystem": "manual_entry",
                                      "capturedAt": row.get("entryTimestamp"),
                                      "ingestedAt": stamped})
            # ONE ROW AT A TIME -- this is the whole point of the contract.
            os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
            with open(LEDGER, "a") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\\n")
            existing[ident] = record
            verdict, ok, disagreeing = "NEW", True, []
        else:
            # SAME SHAPE AS THE REAL IMPORTER. lib.edgelab.bets._diff_fields
            # emits {"field", "existing", "incoming"} dicts, not bare names --
            # and the delivery step now reads that shape to name the fields
            # without printing the values. A fake that emitted plain strings
            # would let a reader-side mistake pass every test here.
            disagreeing = [
                {"field": f, "existing": prior.get(f), "incoming": row.get(f)}
                for f in ECONOMICS if prior.get(f) != row.get(f)
            ]
            if disagreeing:
                verdict, ok = "CONFLICT", False
            else:
                verdict, ok = "DUPLICATE_NOOP", True

        receipts.append({
            "betId": ident if ok or verdict == "CONFLICT" else None,
            "duplicateStatus": verdict,
            "success": ok,
            "sourceBetKey": row["sourceBetKey"],
            "sourceRow": index,
            "conflictingFields": disagreeing,
        })

    print(json.dumps(receipts, indent=2, sort_keys=True))
    if args.receipts_out:
        with open(args.receipts_out, "w") as fh:
            json.dump(receipts, fh, indent=2, sort_keys=True)

    failures = [r for r in receipts if not r["success"]]
    if failures:
        print(f"[import_bet_batch] {len(failures)}/{len(receipts)} row(s) NOT written",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


CURL_SHIM = '''#!/usr/bin/env bash
# Answers the pull-request POST. Records the URL and the body ONLY --
# never the headers, because the delivery step passes the downstream token
# in an Authorization header and a test artifact is not a place for it.
out=""
url=""
body=""
while [ $# -gt 0 ]; do
  case "$1" in
    --output) out="$2"; shift 2 ;;
    -d) body="$2"; shift 2 ;;
    -H) shift 2 ;;
    https://*) url="$1"; shift ;;
    *) shift ;;
  esac
done
printf '%s\\n' "${url}" >> "${CURL_LOG}"
printf '%s\\n' "${body}" >> "${CURL_LOG}"
[ -n "${out}" ] && printf '{"number": 1}' > "${out}"
printf '%s' "${CURL_STATUS:-201}"
'''


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True,
        capture_output=True, text=True,
    ).stdout


def _row(index, *, contracts="10", entry_price="0.43", fee="0.07", stake="4.30"):
    return {
        "sourceBetKey": f"kalshi-order-{index:04d}",
        "marketTicker": f"KXMLBGAME-26SEP15{index:02d}-TOR",
        "side": "YES",
        "gameDate": "2026-09-15",
        "contracts": contracts,
        "entryPrice": entry_price,
        "exchangeFee": fee,
        "stake": stake,
    }


@pytest.fixture
def world(tmp_path):
    """A destination repository, a payload, and the environment the step reads."""
    if shutil.which("git") is None:  # pragma: no cover
        pytest.skip("git is required to exercise the real delivery step")

    remote = tmp_path / "remotes" / f"{DESTINATION}.git"
    remote.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(remote)], check=True)

    # Seed the destination: its importer, its .gitignore, and a ledger that
    # ALREADY holds one row sharing the 17th payload row's identity.
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "--quiet", "-b", "main", ".", cwd=seed)
    _git("config", "user.email", "seed@example.invalid", cwd=seed)
    _git("config", "user.name", "seed", cwd=seed)

    (seed / "scripts" / "edgelab").mkdir(parents=True)
    importer = seed / "scripts" / "edgelab" / "import_bet_batch.py"
    importer.write_text(FAKE_IMPORTER)
    importer.chmod(0o755)
    (seed / ".gitignore").write_text("__pycache__/\n*.pyc\n*.lock\n")

    ledger = seed / LEDGER
    ledger.parent.mkdir(parents=True)

    # The pre-existing row: same identity as row 17, different economics.
    conflicted = _row(17)
    ident = hashlib.sha256(
        "|".join([IMPORT_BATCH_ID, CONFLICTED_SOURCE_KEY,
                  conflicted["marketTicker"], "YES"]).encode()
    ).hexdigest()[:40]
    prior = dict(conflicted, betId=ident, importBatchId=IMPORT_BATCH_ID,
                 contracts="10", entryPrice="0.41", exchangeFee="0.06", stake="4.10")
    ledger.write_text(json.dumps(prior, sort_keys=True) + "\n")

    _git("add", "-A", cwd=seed)
    _git("commit", "--quiet", "-m", "seed destination", cwd=seed)
    _git("push", "--quiet", str(remote), "main", cwd=seed)

    # 17 eligible rows; the 17th carries the execution evidence that
    # disagrees with what the destination already recorded.
    rows = [_row(i) for i in range(1, 17)] + [conflicted]
    assert len(rows) == 17

    runner_temp = tmp_path / "runner_temp"
    (runner_temp / "payloads").mkdir(parents=True)
    (runner_temp / "payloads" / "MLB.json").write_text(
        json.dumps({"importBatchId": IMPORT_BATCH_ID, "rows": rows}, indent=2)
    )

    # github.com -> the local bare repo. Real git, real remote, no network.
    home = tmp_path / "home"
    home.mkdir()
    gitconfig = home / "gitconfig"
    gitconfig.write_text(
        f'[url "file://{tmp_path}/remotes/"]\n'
        f"\tinsteadOf = https://github.com/\n"
        "[init]\n\tdefaultBranch = main\n"
        "[protocol]\n\tallow = always\n"
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(CURL_SHIM)
    curl.chmod(0o755)

    summary = tmp_path / "step_summary.md"
    summary.write_text("")
    curl_log = tmp_path / "curl.log"
    curl_log.write_text("")

    # The delivery step now finishes with the auto-merge gate, which asks
    # GitHub four questions. `GitHubStub` answers them from THIS bare
    # repository, so merging is a real ref update rather than a mocked
    # boolean. Its default is a check suite still RUNNING -- which is exactly
    # what the run that just pushed a commit sees, so the gate WAITs and none
    # of the tests in this file (all about the import itself) are perturbed
    # by it. The auto-merge tests set it green on purpose.
    stub = GitHubStub(
        remote, DESTINATION, branch="kalshi-router/MLB",
        check_runs=(("test", "in_progress", None),),
    )
    api_root = stub.start()

    env = dict(os.environ)
    env.update(
        RUNNER_TEMP=str(runner_temp),
        DRY_RUN="false",
        DOWNSTREAM_REPO_TOKEN="not-a-real-token",
        GITHUB_STEP_SUMMARY=str(summary),
        GITHUB_WORKSPACE=str(ROOT),
        GITHUB_API_ROOT=api_root,
        GIT_CONFIG_GLOBAL=str(gitconfig),
        GIT_CONFIG_NOSYSTEM="1",
        HOME=str(home),
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        CURL_LOG=str(curl_log),
    )
    try:
        yield {
            "tmp": tmp_path, "env": env, "remote": remote,
            "summary": summary, "curl_log": curl_log,
            "receipts": runner_temp / "receipts-MLB.json",
            "rerun_receipts": runner_temp / "receipts-rerun-MLB.json",
            "payload": runner_temp / "payloads" / "MLB.json",
            "stub": stub,
            "rows": rows,
        }
    finally:
        stub.stop()


def delivery_step() -> str:
    """The committed bash of `deliver-wagers.yml`'s delivery step.

    Read from the workflow file, never copied into this test -- that is what
    makes this a regression test for production rather than for a fixture.
    """
    config = yaml.safe_load(DELIVER.read_text())
    for job in config["jobs"].values():
        for step in job.get("steps") or []:
            if step.get("name") == "Deliver each destination":
                return step["run"]
    raise AssertionError("the delivery step is gone from deliver-wagers.yml")


def run_delivery(world):
    return subprocess.run(
        ["bash", "-c", delivery_step()],
        env=world["env"], cwd=world["tmp"],
        capture_output=True, text=True,
    )


def branch_ledger(world) -> list[dict]:
    """The ledger exactly as it exists on the pushed router branch."""
    checkout = world["tmp"] / f"verify-{os.urandom(4).hex()}"
    subprocess.run(
        ["git", "clone", "--quiet", "--branch", "kalshi-router/MLB",
         str(world["remote"]), str(checkout)],
        check=True, capture_output=True,
    )
    text = (checkout / LEDGER).read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


#: The script the delivery step delegates the branch lifecycle to. It is
#: production code invoked by production's own step, so the tests below still
#: run the real thing -- but the tripwire has to know where "the real thing"
#: now lives, or it would pass while testing a fixture.
BRANCH_SCRIPT = ROOT / "scripts/deliver_branch.py"


def test_the_delivery_step_is_the_committed_one():
    """If the step is renamed, or its work moved somewhere these tests do not
    reach, every test below would silently stop testing production. This is
    the tripwire.

    The push and the lease moved into `scripts/deliver_branch.py` when the
    branch lifecycle was repaired. That is not an escape from this test: the
    step still calls that script, `run_delivery` still executes the step, and
    the assertions simply follow the code. What they must NOT do is stop
    asserting, so the lease is still pinned -- at its new address.
    """
    body = delivery_step()
    assert "import_bet_batch.py" in body
    assert "scripts/deliver_branch.py" in body, (
        "the delivery step no longer drives the branch lifecycle")

    script = BRANCH_SCRIPT.read_text()
    assert "force-with-lease" in script
    assert "git" in script and "push" in script, "the push left the script too"


def test_sixteen_written_rows_survive_a_seventeenth_that_is_refused(world):
    """THE INCIDENT. 17 eligible -> 16 NEW + 1 CONFLICT -> all 16 delivered."""
    result = run_delivery(world)

    receipts = json.loads(world["receipts"].read_text())
    assert len(receipts) == 17, receipts
    written = [r for r in receipts if r["success"]]
    refused = [r for r in receipts if not r["success"]]
    assert len(written) == 16
    assert len(refused) == 1
    assert refused[0]["duplicateStatus"] == "CONFLICT"
    assert refused[0]["sourceBetKey"] == CONFLICTED_SOURCE_KEY

    # THE ROWS REACHED THE DESTINATION. Not "were computed" -- are on the
    # branch, in a commit, in the tree git actually received.
    ledger = branch_ledger(world)
    delivered = [r for r in ledger if r["sourceBetKey"] != CONFLICTED_SOURCE_KEY]
    assert len(delivered) == 16, f"the refused row cost valid rows: {len(delivered)}/16"
    assert {r["betId"] for r in written} - {CONFLICTED_SOURCE_KEY} <= {r["betId"] for r in ledger}

    # The conflicted row is REFUSED, not overwritten: the destination's
    # earlier record still reads exactly as it did.
    prior = [r for r in ledger if r["sourceBetKey"] == CONFLICTED_SOURCE_KEY]
    assert len(prior) == 1, "the refused row was written anyway"
    assert prior[0]["entryPrice"] == "0.41", "an existing canonical row was overwritten"

    # EXECUTION ECONOMICS SURVIVE THE TRIP EXACTLY.
    by_key = {r["sourceBetKey"]: r for r in ledger}
    for row in world["rows"][:16]:
        landed = by_key[row["sourceBetKey"]]
        for field in ("contracts", "entryPrice", "exchangeFee", "stake"):
            assert landed[field] == row[field], (field, row["sourceBetKey"])

    assert result.returncode == 1, "a partial delivery must not be a green run"


def test_the_refusal_is_still_reported(world):
    """Delivering the 16 must not hide the 1. Both facts, or neither."""
    result = run_delivery(world)
    out = result.stdout

    assert "::error::MLB: the destination importer refused at least one row" in out
    assert "::warning::MLB: DELIVERED PARTIALLY" in out
    assert "  rows: 17" in out
    assert "  failed rows: 1" in out
    assert "destinations that failed: 1" in out
    assert "PARTIAL" in world["summary"].read_text()
    assert result.returncode == 1


def test_the_refusal_names_the_fields_it_disagrees_on(world):
    """THE 2026-09-20 00:52 RUN.

    A CONFLICT is correct: the destination's canonical row disagrees with
    this reading of Kalshi and only a person may decide which is right. But
    the run log said "CONFLICT: 1" and nothing else, and the scheduled
    workflow goes red on the same row every fifteen minutes until someone
    resolves it. A refusal nobody can identify is a refusal nobody can
    resolve, so the step names the FIELDS that disagree.
    """
    out = run_delivery(world).stdout
    assert "  fields the destination disagrees on (names only): entryPrice" in out


def test_the_refusal_still_never_prints_the_values(world):
    """Names, not numbers. The counterpart to the test above, and the
    reason it prints `field` rather than the whole receipt entry: the
    receipt carries `existing` and `incoming`, and both are execution
    economics that must not reach a public Actions log -- the same rule
    the step already applies to the stake and the ticker."""
    out = run_delivery(world).stdout

    receipts = json.loads(world["receipts"].read_text())
    refused = [r for r in receipts if not r["success"]]
    assert refused, "the fixture no longer produces a refusal"
    values = [
        str(d[k]) for r in refused for d in r["conflictingFields"]
        for k in ("existing", "incoming") if d[k] is not None
    ]
    assert values, "the receipt carries no values -- this test proves nothing"

    # Only the block this step prints. The fake importer dumps its whole
    # receipt list to stdout (the real one writes it to --receipts-out),
    # and that dump is not what this test is about.
    marker = "  fields the destination disagrees on (names only): "
    line = next(l for l in out.splitlines() if l.startswith(marker))
    for value in values:
        assert value not in line, f"{value!r} reached the public log"


def test_the_partial_delivery_still_opens_a_pull_request(world):
    """A branch is not the ledger. The 16 have to be proposed, not parked."""
    run_delivery(world)
    log = world["curl_log"].read_text()
    assert f"https://api.github.com/repos/{DESTINATION}/pulls" in log
    assert '"head":"kalshi-router/MLB"' in log
    # The token is passed in a header; it must not have reached this log.
    assert "not-a-real-token" not in log


def test_rerunning_before_the_pull_request_merges_does_not_duplicate_the_sixteen(world):
    """Retry safety BEFORE the destination merges.

    Each run re-clones the destination's MAIN and rebuilds the router branch
    from it, so the importer legitimately reports NEW again -- the 16 rows are
    on an unmerged branch and main has never seen them. DUPLICATE_NOOP is a
    POST-MERGE property (proved in the next test), and demanding it here would
    be demanding the wrong thing.

    What must hold now is that nothing DOUBLES: one branch, one pull request,
    the same canonical bet ids, and exactly as many ledger rows as before.
    """
    first = run_delivery(world)
    assert first.returncode == 1
    before = branch_ledger(world)
    first_ids = sorted(r["betId"] for r in before)

    second = run_delivery(world)
    after = branch_ledger(world)

    assert len(after) == len(before) == 17, (len(before), len(after))
    assert sorted(r["betId"] for r in after) == first_ids, "canonical identity moved"
    assert after == before, "a second delivery changed the delivered ledger"

    branches = _git("for-each-ref", "--format=%(refname:short)", "refs/heads/",
                    cwd=world["remote"]).split()
    assert sorted(branches) == ["kalshi-router/MLB", "main"], branches

    # One pull request. The second run gets 422 in production; the shim
    # returns 201 both times, so assert on the branch named, not the count.
    assert world["curl_log"].read_text().count('"head":"kalshi-router/MLB"') == 2
    assert second.returncode == 1, "the unresolved conflict must stay visible"


def test_once_merged_the_sixteen_come_back_as_duplicate_noop(world):
    """Retry safety AFTER the destination merges -- MISSION 2's final proof.

    With the rows in main, a further delivery must write nothing at all: 16
    DUPLICATE_NOOP, the conflict still refused, no new commit, no push.
    """
    assert run_delivery(world).returncode == 1

    # Merge the router's branch exactly as the destination would.
    merged = world["tmp"] / "merge"
    subprocess.run(["git", "clone", "--quiet", str(world["remote"]), str(merged)],
                   check=True, capture_output=True)
    _git("-c", "user.email=m@example.invalid", "-c", "user.name=m",
         "merge", "--no-ff", "--quiet", "-m", "Record Kalshi wagers (MLB)",
         "origin/kalshi-router/MLB", cwd=merged)
    _git("push", "--quiet", "origin", "main", cwd=merged)
    main_ledger = [json.loads(line) for line in (merged / LEDGER).read_text().splitlines() if line.strip()]
    assert len(main_ledger) == 17

    world["curl_log"].write_text("")
    again = run_delivery(world)

    receipts = json.loads(world["receipts"].read_text())
    verdicts = {}
    for row in receipts:
        verdicts[row["duplicateStatus"]] = verdicts.get(row["duplicateStatus"], 0) + 1
    assert verdicts == {"DUPLICATE_NOOP": 16, "CONFLICT": 1}, verdicts

    assert "refused row(s) and wrote nothing" in again.stdout
    assert "pushed kalshi-router/MLB" not in again.stdout
    assert world["curl_log"].read_text().strip() == "", "opened a second pull request"

    after = [json.loads(line) for line in
             (merged / LEDGER).read_text().splitlines() if line.strip()]
    assert after == main_ledger, "a post-merge rerun rewrote the ledger"
    assert again.returncode == 1, "the unresolved conflict must stay visible"


def test_an_importer_that_refuses_everything_delivers_nothing(world):
    """The other half of the rule: 'wrote nothing' must not become a commit,
    and must not read like 'every row was already imported'."""
    seed = world["tmp"] / "break"
    subprocess.run(["git", "clone", "--quiet", str(world["remote"]), str(seed)],
                   check=True, capture_output=True)
    importer = seed / "scripts" / "edgelab" / "import_bet_batch.py"
    importer.write_text(
        '#!/usr/bin/env python3\n'
        'import json, sys, argparse\n'
        'ap = argparse.ArgumentParser()\n'
        'ap.add_argument("--file"); ap.add_argument("--receipts-out")\n'
        'a = ap.parse_args()\n'
        'rows = json.load(open(a.file))["rows"]\n'
        'r = [{"betId": None, "duplicateStatus": "UNRESOLVED", "success": False,\n'
        '      "sourceBetKey": x["sourceBetKey"]} for x in rows]\n'
        'json.dump(r, open(a.receipts_out, "w"))\n'
        'sys.exit(1)\n'
    )
    _git("add", "-A", cwd=seed)
    _git("-c", "user.email=b@example.invalid", "-c", "user.name=b",
         "commit", "--quiet", "-m", "importer refuses everything", cwd=seed)
    _git("push", "--quiet", "origin", "main", cwd=seed)

    result = run_delivery(world)
    assert "refused row(s) and wrote nothing" in result.stdout
    assert "no ledger change" not in result.stdout, (
        "a total refusal printed as a clean no-op"
    )
    assert "pushed kalshi-router/MLB" not in result.stdout
    assert world["curl_log"].read_text().strip() == "", "opened a PR for nothing"
    assert result.returncode == 1


def test_a_clean_batch_is_still_a_green_delivery(world):
    """The fix must not make every run look partial."""
    payload = world["env"]["RUNNER_TEMP"] + "/payloads/MLB.json"
    body = json.loads(Path(payload).read_text())
    body["rows"] = body["rows"][:16]          # drop the conflicted row
    Path(payload).write_text(json.dumps(body))

    result = run_delivery(world)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DELIVERED PARTIALLY" not in result.stdout
    assert "refused at least one row" not in result.stdout
    assert "destinations that failed: 0" in result.stdout
    assert len(branch_ledger(world)) == 17   # 16 delivered + the untouched prior row
