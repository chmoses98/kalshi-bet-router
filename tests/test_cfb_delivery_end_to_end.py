"""A CFB delivery, driven by the REAL delivery step, end to end.

*** WHY THIS FILE IS THE ACTIVATION'S PROOF ***
Everything else about CFB routing is a table entry and a unit test. This runs
`deliver-wagers.yml`'s committed bash against a CFB-SHAPED destination -- ledger
on `accounting-data`, importer on `main`, snake_case rows, one file per season
-- and asserts the wagers land on a branch with a pull request against the
right base.

It is the same harness `test_partial_delivery_regression.py` uses, and for the
same reason: substituting the loop with a mock would test the mock. Only the
destination's own importer is a stand-in (the real one needs the CFB
repository's dependency tree), and it reproduces that importer's documented
contract -- validate, deduplicate on `source_bet_key`, mint `wager_id`
deterministically from that key, append only, and report per row.

WHAT THIS WOULD HAVE CAUGHT
Every one of these, before the profiles existed:
  * the delivery cloned `main` and found no ledger;
  * it ran `scripts/edgelab/import_bet_batch.py`, which is not in that repo;
  * the containment check refused `wagers/2026.jsonl` for being outside
    `data/`;
  * the pull request targeted `main`, where the ledger is not;
  * the merge gate refused the base branch and the row identity spelling.
"""

from __future__ import annotations

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

DESTINATION = "chmoses98/cfb-edge-finder"
LEDGER_BRANCH = "accounting-data"
CODE_BRANCH = "main"
SEASON = 2026
LEDGER = f"wagers/{SEASON}.jsonl"


#: A stand-in for the CFB repository's `scripts/import_routed_wagers.py`.
#:
#: Reproduces the contract that importer documents: `--payload`, `--base-dir`,
#: `--season` (required, never inferred), refuses a row carrying a field the
#: ledger does not model, deduplicates on `source_bet_key` against the whole
#: file, mints `wager_id` as `routed-<sha256(source_bet_key)[:24]>`, appends
#: only, and exits 1 if any row was refused while still writing the rest.
FAKE_CFB_IMPORTER = '''#!/usr/bin/env python3
import argparse, hashlib, json, os, sys

KNOWN = {
    "source_bet_key", "import_batch_id", "entry_method", "game_date",
    "market_ticker", "side", "executed_at", "contracts", "execution_price",
    "stake", "fees_paid", "fees_are_estimated", "venue",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True)
    parser.add_argument("--base-dir", required=True)
    parser.add_argument("--season", required=True, type=int)
    parser.add_argument("--receipts-out", default=None)
    args = parser.parse_args()

    payload = json.load(open(args.payload))
    batch = payload["importBatchId"]
    rows = payload["rows"]

    ledger = os.path.join(args.base_dir, "wagers", f"{args.season}.jsonl")
    existing = set()
    if os.path.exists(ledger):
        for line in open(ledger):
            if line.strip():
                existing.add(json.loads(line)["source_bet_key"])

    receipts = []
    refused = 0
    os.makedirs(os.path.dirname(ledger), exist_ok=True)
    for index, row in enumerate(rows):
        key = row.get("source_bet_key")
        unknown = sorted(set(row) - KNOWN)
        if unknown or not key:
            refused += 1
            receipts.append({
                "source_bet_key": key, "wager_id": None,
                "duplicate_status": "REFUSED", "success": False,
                "reason": f"unknown field(s): {unknown}" if unknown else "no source key",
                "row": index,
            })
            continue
        if row.get("import_batch_id") != batch:
            refused += 1
            receipts.append({
                "source_bet_key": key, "wager_id": None,
                "duplicate_status": "REFUSED", "success": False,
                "reason": "row batch disagrees with its envelope", "row": index,
            })
            continue
        ident = "routed-" + hashlib.sha256(key.encode()).hexdigest()[:24]
        if key in existing:
            receipts.append({
                "source_bet_key": key, "wager_id": ident,
                "duplicate_status": "DUPLICATE_NOOP", "success": True, "row": index,
            })
            continue
        record = dict(row, wager_id=ident, season=args.season,
                      schema_version="cfb_accounted_wager.v1")
        with open(ledger, "a") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\\n")
        existing.add(key)
        receipts.append({
            "source_bet_key": key, "wager_id": ident,
            "duplicate_status": "NEW", "success": True, "row": index,
        })

    print(f"import batch: {batch}")
    print(f"  written: {sum(1 for r in receipts if r['duplicate_status'] == 'NEW')}")
    if args.receipts_out:
        json.dump({
            "importBatchId": batch,
            "season": args.season,
            "written": sum(1 for r in receipts if r["duplicate_status"] == "NEW"),
            "alreadyPresent": sum(
                1 for r in receipts if r["duplicate_status"] == "DUPLICATE_NOOP"
            ),
            "refused": refused,
            "refusals": [
                {"row": r["row"], "reason": r["reason"]}
                for r in receipts if not r["success"]
            ],
            "keysWritten": [
                r["source_bet_key"] for r in receipts if r["duplicate_status"] == "NEW"
            ],
            "rows": receipts,
        }, open(args.receipts_out, "w"), indent=2, sort_keys=True)
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
'''

#: A stand-in for the CFB repository's `scripts/validate_accounting_ledger.py`.
#:
#: Reproduces its contract: check every row, write a JSON verdict, exit 0 when
#: the ledger is clean and 1 when it is not. The real one validates against the
#: full accounting schemas, which need the CFB repository's dependency tree.
FAKE_VALIDATOR = '''#!/usr/bin/env python3
import argparse, json, os, sys

REQUIRED = {"source_bet_key", "market_ticker", "side", "stake", "wager_id"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-dir", required=True)
    parser.add_argument("--against", default=None)
    parser.add_argument("--result-out", default=None)
    args = parser.parse_args()

    problems = []
    rows = 0
    seen = set()
    wagers = os.path.join(args.base_dir, "wagers")
    for name in sorted(os.listdir(wagers)) if os.path.isdir(wagers) else []:
        path = os.path.join(wagers, name)
        for number, line in enumerate(open(path), start=1):
            if not line.strip():
                continue
            rows += 1
            try:
                row = json.loads(line)
            except Exception:
                problems.append(f"{name}:{number} not decodable JSON")
                continue
            missing = REQUIRED - set(row)
            if missing:
                problems.append(f"{name}:{number} missing {sorted(missing)}")
            key = row.get("source_bet_key")
            if key in seen:
                problems.append(f"{name}:{number} duplicate source_bet_key")
            seen.add(key)

    print(f"wager rows:      {rows}")
    print(f"problems:        {len(problems)}")
    for problem in problems:
        print(f"  {problem}")
    if args.result_out:
        json.dump({"passed": not problems, "wager_rows": rows, "problems": problems},
                  open(args.result_out, "w"), indent=2, sort_keys=True)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
'''

CURL_SHIM = '''#!/usr/bin/env bash
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
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def cfb_row(index, *, game_date="2026-09-19"):
    """One row in `cfb_accounted_wager.v1`'s own vocabulary.

    Snake_case, `execution_price` rather than `entryPrice`, the batch id ON
    THE ROW as well as on the envelope, and no `wager_id` -- the destination
    mints that. Every one of those differs from MLB's shape."""
    return {
        "source_bet_key": f"kalshi:v1:{index:064x}",
        "import_batch_id": ROUTER_IMPORT_BATCH_ID,
        "entry_method": "IMPORTED_RECEIPT",
        "game_date": game_date,
        "market_ticker": f"KXNCAAFSPREAD-26SEP19LSUMISS-LSU{index}",
        "side": "YES",
        "executed_at": "2026-09-19T15:30:00Z",
        "contracts": 20.0,
        "execution_price": 0.53,
        "stake": 10.6,
        "fees_paid": 0.14,
        "fees_are_estimated": False,
        "venue": "kalshi",
    }


@pytest.fixture
def cfb_world(tmp_path):
    """A CFB-shaped destination: two branches, two vocabularies, one season."""
    if shutil.which("git") is None:  # pragma: no cover
        pytest.skip("git is required to exercise the real delivery step")

    remote = tmp_path / "remotes" / f"{DESTINATION}.git"
    remote.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--quiet", "--bare", "-b", CODE_BRANCH, str(remote)], check=True
    )

    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "--quiet", "-b", CODE_BRANCH, ".", cwd=seed)
    _git("config", "user.email", "seed@example.invalid", cwd=seed)
    _git("config", "user.name", "seed", cwd=seed)

    # `main` holds the IMPORTER and no ledger.
    (seed / "scripts").mkdir()
    importer = seed / "scripts" / "import_routed_wagers.py"
    importer.write_text(FAKE_CFB_IMPORTER)
    importer.chmod(0o755)
    validator = seed / "scripts" / "validate_accounting_ledger.py"
    validator.write_text(FAKE_VALIDATOR)
    validator.chmod(0o755)
    (seed / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    _git("add", "-A", cwd=seed)
    _git("commit", "--quiet", "-m", "seed code branch", cwd=seed)
    _git("push", "--quiet", str(remote), CODE_BRANCH, cwd=seed)

    # `accounting-data` holds the LEDGER and no importer. An orphan branch,
    # exactly like the real one.
    _git("checkout", "--quiet", "--orphan", LEDGER_BRANCH, cwd=seed)
    _git("rm", "-rf", "--quiet", ".", cwd=seed)
    (seed / "wagers").mkdir()
    (seed / LEDGER).write_text("")
    (seed / "README.md").write_text("the wager ledger\n")
    (seed / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    _git("add", "-A", cwd=seed)
    _git("commit", "--quiet", "-m", "seed ledger branch", cwd=seed)
    _git("push", "--quiet", str(remote), LEDGER_BRANCH, cwd=seed)

    runner_temp = tmp_path / "runner_temp"
    (runner_temp / "payloads").mkdir(parents=True)
    rows = [cfb_row(i) for i in range(1, 5)]
    (runner_temp / "payloads" / "CFB.json").write_text(
        json.dumps({"importBatchId": ROUTER_IMPORT_BATCH_ID, "rows": rows}, indent=2)
    )

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

    stub = GitHubStub(
        remote,
        DESTINATION,
        branch="kalshi-router/CFB",
        base_branch=LEDGER_BRANCH,
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
            "tmp": tmp_path,
            "env": env,
            "remote": remote,
            "summary": summary,
            "curl_log": curl_log,
            "receipts": runner_temp / "receipts-CFB.json",
            "rerun_receipts": runner_temp / "receipts-rerun-CFB.json",
            "payload": runner_temp / "payloads" / "CFB.json",
            "stub": stub,
            "rows": rows,
        }
    finally:
        stub.stop()


def delivery_step() -> str:
    config = yaml.safe_load(DELIVER.read_text())
    for job in config["jobs"].values():
        for step in job.get("steps") or []:
            if step.get("name") == "Deliver each destination":
                return step["run"]
    raise AssertionError("the delivery step is gone from deliver-wagers.yml")


def run_delivery(world):
    return subprocess.run(
        ["bash", "-c", delivery_step()],
        env=world["env"],
        cwd=world["tmp"],
        capture_output=True,
        text=True,
    )


def branch_ledger(world) -> list[dict]:
    checkout = world["tmp"] / f"verify-{os.urandom(4).hex()}"
    subprocess.run(
        ["git", "clone", "--quiet", "--branch", "kalshi-router/CFB",
         str(world["remote"]), str(checkout)],
        check=True,
        capture_output=True,
    )
    text = (checkout / LEDGER).read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ------------------------------------------------------------- the run


def test_a_cfb_delivery_lands_on_the_accounting_data_branch(cfb_world):
    """The whole activation, end to end, through production's own bash."""
    result = run_delivery(cfb_world)
    assert result.returncode == 0, result.stdout + result.stderr

    rows = branch_ledger(cfb_world)
    assert len(rows) == 4
    assert {r["source_bet_key"] for r in rows} == {
        r["source_bet_key"] for r in cfb_world["rows"]
    }
    for row in rows:
        assert row["import_batch_id"] == ROUTER_IMPORT_BATCH_ID
        assert row["season"] == SEASON
        assert row["wager_id"].startswith("routed-")
        assert row["schema_version"] == "cfb_accounted_wager.v1"


def test_the_delivery_clones_the_ledger_branch_not_main(cfb_world):
    result = run_delivery(cfb_world)
    assert f"({LEDGER_BRANCH})" in result.stdout
    assert f"destination {LEDGER_BRANCH}:" in result.stdout


def test_the_season_is_derived_from_the_contest_dates(cfb_world):
    result = run_delivery(cfb_world)
    assert f"season ledger: {SEASON}" in result.stdout


def test_a_january_bowl_game_files_under_the_previous_season(cfb_world):
    """A season spans two calendar years. `date +%Y` would misfile this."""
    payload = json.loads(cfb_world["payload"].read_text())
    for row in payload["rows"]:
        row["game_date"] = "2027-01-02"
    cfb_world["payload"].write_text(json.dumps(payload))

    result = run_delivery(cfb_world)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "season ledger: 2026" in result.stdout


def test_a_payload_spanning_two_seasons_is_refused_not_misfiled(cfb_world):
    payload = json.loads(cfb_world["payload"].read_text())
    payload["rows"][0]["game_date"] = "2025-11-01"
    cfb_world["payload"].write_text(json.dumps(payload))

    result = run_delivery(cfb_world)
    assert result.returncode != 0
    assert "season could not be established" in result.stdout


def test_the_pull_request_targets_the_ledger_branch(cfb_world):
    run_delivery(cfb_world)
    log = cfb_world["curl_log"].read_text()
    assert "/repos/chmoses98/cfb-edge-finder/pulls" in log
    assert f'"base":"{LEDGER_BRANCH}"' in log
    assert '"base":"main"' not in log


def test_the_second_identical_delivery_writes_nothing_new(cfb_world):
    """IDEMPOTENCY, proved against the real loop rather than asserted.

    The ledger keys on `source_bet_key`, which is derived from the exchange's
    own order identity, so the same fill delivered twice is the same row."""
    first = run_delivery(cfb_world)
    assert first.returncode == 0, first.stdout + first.stderr
    rows_after_first = branch_ledger(cfb_world)

    second = run_delivery(cfb_world)
    assert second.returncode == 0, second.stdout + second.stderr
    assert branch_ledger(cfb_world) == rows_after_first
    assert "a second identical import changed nothing" in second.stdout


def test_the_idempotency_re_run_is_proved_every_run(cfb_world):
    result = run_delivery(cfb_world)
    assert "a second identical import changed nothing" in result.stdout
    rerun = json.loads(cfb_world["rerun_receipts"].read_text())
    assert rerun["written"] == 0
    assert rerun["alreadyPresent"] == len(cfb_world["rows"])


def test_a_refused_row_does_not_cost_the_rows_that_succeeded(cfb_world):
    """The 2026-09-16 incident's lesson, asserted for CFB too: a partial
    failure must not become a quiet success, and it must not cost valid rows
    either."""
    payload = json.loads(cfb_world["payload"].read_text())
    payload["rows"][2]["a_field_this_ledger_does_not_model"] = True
    cfb_world["payload"].write_text(json.dumps(payload))

    result = run_delivery(cfb_world)
    assert result.returncode != 0, "a refusal must not read as a clean run"
    rows = branch_ledger(cfb_world)
    assert len(rows) == 3, "the three good rows were still delivered"
    assert "refused at least one row" in result.stdout


def test_the_run_log_carries_no_ticker_price_stake_or_source_key(cfb_world):
    """This repository is public and so are its Actions logs."""
    result = run_delivery(cfb_world)
    combined = result.stdout + result.stderr + cfb_world["summary"].read_text()
    for secret in ("KXNCAAF", "0.53", "10.6", "kalshi:v1", "IMPORTED_RECEIPT"):
        assert secret not in combined, f"the delivery log leaked {secret!r}"


def test_the_delivery_ran_the_cfb_importer_and_not_mlbs(cfb_world):
    """A wrong importer would not merely fail -- MLB's reads `--file` and an
    envelope batch id, so it would refuse everything and look like a bad
    payload."""
    run_delivery(cfb_world)
    receipts = json.loads(cfb_world["receipts"].read_text())
    assert receipts["season"] == SEASON
    assert receipts["written"] == 4
    assert receipts["refused"] == 0


def test_the_destinations_own_validator_runs_and_reports(cfb_world):
    """The signal that replaces CI on a branch that cannot have any.

    GitHub runs a `pull_request` workflow only if that workflow file exists on
    the pull request's BASE branch, and `accounting-data` is an orphan with no
    `.github/`. So the destination's own validator supplies the verdict, run
    from its own code branch against the tree its own importer produced."""
    result = run_delivery(cfb_world)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "the destination's own ledger validator accepted this tree" in result.stdout

    verdict = json.loads(
        (cfb_world["tmp"] / "runner_temp" / "validator-CFB.json").read_text()
    )
    assert verdict["passed"] is True
    assert verdict["wager_rows"] == 4


def test_a_validator_refusal_stops_the_delivery_from_merging(cfb_world):
    """A tree the destination will not accept must not land unread."""
    code = cfb_world["tmp"] / "seed"
    subprocess.run(
        ["git", "checkout", "--quiet", CODE_BRANCH], cwd=code, check=True,
        capture_output=True,
    )
    validator = code / "scripts" / "validate_accounting_ledger.py"
    validator.write_text(
        FAKE_VALIDATOR.replace(
            'REQUIRED = {"source_bet_key", "market_ticker", "side", "stake", "wager_id"}',
            'REQUIRED = {"a_field_no_row_will_ever_have"}',
        )
    )
    _git("add", "-A", cwd=code)
    _git("commit", "--quiet", "-m", "stricter validator", cwd=code)
    _git("push", "--quiet", str(cfb_world["remote"]), CODE_BRANCH, cwd=code)

    result = run_delivery(cfb_world)
    assert result.returncode != 0
    assert "ledger validator REFUSED" in result.stdout
    # ...and the rows it DID write are still on the branch, like any other
    # partial outcome. A refusal stops the merge, not the delivery.
    assert len(branch_ledger(cfb_world)) == 4


def test_the_containment_check_permits_the_cfb_ledger_paths(cfb_world):
    """A `data/` containment rule would have refused every row written here."""
    result = run_delivery(cfb_world)
    assert "dirtied files outside" not in result.stdout
    assert result.returncode == 0


def test_a_file_outside_the_cfb_ledger_paths_still_stops_the_delivery(cfb_world):
    """The containment check is widened per destination, not removed."""
    # Make the importer scribble outside its own prefixes, the way a changed
    # upstream .gitignore would.
    code = cfb_world["tmp"] / "seed"
    subprocess.run(
        ["git", "checkout", "--quiet", CODE_BRANCH], cwd=code, check=True,
        capture_output=True,
    )
    importer = code / "scripts" / "import_routed_wagers.py"
    importer.write_text(
        FAKE_CFB_IMPORTER.replace(
            "    print(f\"import batch: {batch}\")",
            "    open(os.path.join(args.base_dir, 'stray.txt'), 'w').write('x')\n"
            "    print(f\"import batch: {batch}\")",
        )
    )
    _git("add", "-A", cwd=code)
    _git("commit", "--quiet", "-m", "scribble", cwd=code)
    _git("push", "--quiet", str(cfb_world["remote"]), CODE_BRANCH, cwd=code)

    result = run_delivery(cfb_world)
    assert result.returncode != 0
    assert "dirtied files outside" in result.stdout
    # deliver_branch.py names the offending paths on STDERR, so stdout stays a
    # single parseable token for the workflow to branch on.
    assert "stray.txt" in result.stderr
