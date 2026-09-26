"""THE 2026-09-26 SETTLEMENT INCIDENT: a few unmatched rows cost every valid one.

*** WHAT HAPPENED ***
The scheduled `Settle wagers downstream` run built 43 CFB settlements. 41 of
them settle wagers already on `accounting-data`. The other 2 settle wagers the
delivery run had put onto the router's open wager proposal
(`kalshi-router/CFB`), which is HELD FOR OBSERVATION and so had not merged.
Their Kalshi markets had already settled.

`scripts/settlement_season.py` refused the WHOLE batch because 2 of its 43
rows matched no wager on the ledger, so the destination's settlement importer
never ran. That importer screens per row: it would have written the 41 and
refused the 2. The workflow says a refused settlement must not cost the rest
of the batch, and the season script, running first, made that impossible.

*** THE TWO QUESTIONS ***
"Which season's ledger does this batch belong to?" and "is every row
attributable to a wager on the ledger?" are different questions. The rows
that match answer the first. The destination importer answers the second,
per row. These tests pin both halves:

  * settlement_season.py: season resolution cases A-E (the 41 + 2 case,
    every row unmatched, two seasons, no source key, the clean case);
  * the COMMITTED BASH of settle-wagers.yml, run against a local destination
    in the production shape: the 41 valid rows are written and proposed, the
    2 are refused per row and reported, the delivery is flagged PARTIAL,
    reconciliation accounts for all 43 and says the 2 await their wagers, a
    re-run is idempotent, and once the wager proposal merges the next run
    settles the 2 without anyone touching them.

The destination is a stand-in for cfb-edge-finder's
`scripts/import_routed_settlements.py` that reproduces the contract it
documents (see FAKE_SETTLEMENT_IMPORTER); the rest is production's own code.
Only github.com (a local bare repo via `insteadOf`), `curl` (a PATH shim) and
the GitHub API (tests/github_api_stub.py) are substituted, as in
tests/test_cfb_delivery_end_to_end.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from kalshi_router.destinations import PROFILES, profile_for
from kalshi_router.sports import Sport
from tests.github_api_stub import GitHubStub

ROOT = Path(__file__).resolve().parents[1]
SEASON_SCRIPT = ROOT / "scripts" / "settlement_season.py"
SETTLE = ROOT / ".github" / "workflows" / "settle-wagers.yml"

DESTINATION = "chmoses98/cfb-edge-finder"
LEDGER_BRANCH = "accounting-data"
CODE_BRANCH = "main"
WAGER_PROPOSAL = "kalshi-router/CFB"
SETTLEMENT_BRANCH = "kalshi-router/settle-CFB"

#: The production shape. Nothing about the fix depends on these numbers, the
#: year, or which rows are unmatched; they are what the incident measured.
SEASON = 2026
ON_LEDGER = 41
ON_PROPOSAL = 2


def key(index: int) -> str:
    return f"kalshi:v1:{index:064x}"


def wager_row(index: int, season: int = SEASON) -> dict:
    return {
        "source_bet_key": key(index),
        "wager_id": f"routed-{index:024x}",
        "season": season,
        "market_ticker": f"KXNCAAFTOTAL-26SEP25TEAM{index}-51",
        "side": "NO",
        "stake": 10.0,
    }


def settlement_row(index: int) -> dict:
    return {
        "source_bet_key": key(index),
        "market_ticker": f"KXNCAAFTOTAL-26SEP25TEAM{index}-51",
        "side": "NO",
        "settlement_status": "SETTLED",
        "settled_at": "2026-09-26T07:12:00Z",
        "result": "WON",
        "gross_return": 20.0,
        "net_profit_loss": 9.83,
        "refusals": [],
        "venue": "kalshi",
    }


def jsonl(rows) -> str:
    return "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)


# ------------------------------------------------ settlement_season.py (A-E)


def write_ledger(root: Path, season: int, indices) -> Path:
    (root / "wagers").mkdir(parents=True, exist_ok=True)
    (root / "wagers" / f"{season}.jsonl").write_text(jsonl(wager_row(i, season) for i in indices))
    return root


def write_payload(tmp_path: Path, rows) -> Path:
    path = tmp_path / "CFB-settlements.json"
    path.write_text(json.dumps({"settlements": rows}))
    return path


def resolve(payload: Path, ledger: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SEASON_SCRIPT), "--payload", str(payload), "--ledger-dir", str(ledger)],
        capture_output=True, text=True, check=False,
    )


def test_A_one_season_with_a_minority_of_unmatched_rows_resolves_that_season(tmp_path):
    """THE PRODUCTION REGRESSION. 41 matched in one season, 2 unmatched: the
    season is proved by the 41, and the 2 go on to the importer."""
    ledger = write_ledger(tmp_path / "dest", SEASON, range(1, ON_LEDGER + 1))
    payload = write_payload(
        tmp_path, [settlement_row(i) for i in range(1, ON_LEDGER + ON_PROPOSAL + 1)]
    )
    result = resolve(payload, ledger)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(SEASON)
    assert f"canonical wager matches: {ON_LEDGER}" in result.stderr
    assert f"unmatched settlement parents: {ON_PROPOSAL}" in result.stderr
    assert "per-row refusal" in result.stderr
    # It is not described as the all-undelivered case, which points elsewhere.
    assert "have not been delivered yet" not in result.stderr


def test_A_names_no_key_ticker_or_payout_even_when_rows_are_unmatched(tmp_path):
    ledger = write_ledger(tmp_path / "dest", SEASON, range(1, 3))
    payload = write_payload(tmp_path, [settlement_row(i) for i in range(1, 5)])
    result = resolve(payload, ledger)
    assert result.returncode == 0
    for secret in ("kalshi:v1", "KXNCAAF", "9.83", "20.0"):
        assert secret not in result.stdout + result.stderr


def test_B_every_row_unmatched_fails_closed_and_guesses_no_season(tmp_path):
    """No match is no evidence of a season. Not today's date, not the
    settlement time, not the ticker."""
    ledger = write_ledger(tmp_path / "dest", SEASON, range(1, ON_LEDGER + 1))
    payload = write_payload(tmp_path, [settlement_row(i) for i in (900, 901)])
    result = resolve(payload, ledger)
    assert result.returncode == 2
    assert result.stdout.strip() == ""
    assert "none is guessed" in result.stderr
    assert "have not been delivered yet" in result.stderr


@pytest.mark.parametrize("with_unmatched", [False, True])
def test_C_matches_across_two_seasons_fail_closed(tmp_path, with_unmatched):
    """Never one season picked arbitrarily -- with or without unmatched rows
    alongside."""
    dest = tmp_path / "dest"
    write_ledger(dest, 2025, [1, 2])
    write_ledger(dest, 2026, [3, 4])
    rows = [settlement_row(i) for i in (1, 3)]
    if with_unmatched:
        rows.append(settlement_row(900))
    result = resolve(write_payload(tmp_path, rows), dest)
    assert result.returncode == 2
    assert result.stdout.strip() == ""
    assert "[2025, 2026]" in result.stderr


@pytest.mark.parametrize("other_rows_match", [False, True])
def test_D_a_row_with_no_source_key_fails_closed(tmp_path, other_rows_match):
    """Matched siblings do not rescue a row that cannot be attributed at all."""
    ledger = write_ledger(tmp_path / "dest", SEASON, [1, 2])
    keyless = settlement_row(3)
    del keyless["source_bet_key"]
    rows = [keyless] + ([settlement_row(1), settlement_row(2)] if other_rows_match else [])
    result = resolve(write_payload(tmp_path, rows), ledger)
    assert result.returncode == 2
    assert result.stdout.strip() == ""
    assert "no source key" in result.stderr


def test_E_single_season_with_nothing_unmatched_is_unchanged(tmp_path):
    ledger = write_ledger(tmp_path / "dest", SEASON, range(1, ON_LEDGER + 1))
    payload = write_payload(tmp_path, [settlement_row(i) for i in range(1, ON_LEDGER + 1)])
    result = resolve(payload, ledger)
    assert result.returncode == 0
    assert result.stdout.strip() == str(SEASON)
    assert "unmatched settlement parents: 0" in result.stderr
    assert "per-row refusal" not in result.stderr


def test_the_season_script_has_no_clock_and_reads_no_ticker():
    """The shortcuts this fix must not take, pinned in the source."""
    source = SEASON_SCRIPT.read_text()
    for banned in ("datetime", "time.time", "date.today", "settled_at", "market_ticker"):
        assert banned not in source, f"settlement_season.py reads {banned!r}"


# ------------------------------------- the workflow, end to end (G, F, H)


#: A stand-in for cfb-edge-finder's `scripts/import_routed_settlements.py`,
#: reproducing the contract of it and of
#: `cfb_edge_finder.accounting.import_settlements.import_rows`:
#:
#:   * a settlement is written only if its `source_bet_key` is in the SAME
#:     season's wager ledger; otherwise it is REFUSED per row and the rest of
#:     the batch continues;
#:   * appends key on `source_bet_key`, so a re-run writes nothing
#:     (DUPLICATE_NOOP) -- a market settles once;
#:   * per-row receipts `{row, source_bet_key, settlement_id, duplicate_status,
#:     success, reason}` inside an object of counts;
#:   * exit 1 if anything was refused, 0 otherwise.
FAKE_SETTLEMENT_IMPORTER = '''#!/usr/bin/env python3
import argparse, hashlib, json, os, sys


def keys(path):
    out = set()
    if os.path.exists(path):
        for line in open(path):
            if line.strip():
                out.add(json.loads(line)["source_bet_key"])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--payload", required=True)
    ap.add_argument("--base-dir", required=True)
    ap.add_argument("--season", required=True, type=int)
    ap.add_argument("--receipts-out", default=None)
    a = ap.parse_args()

    rows = json.load(open(a.payload))["settlements"]
    wagers = keys(os.path.join(a.base_dir, "wagers", f"{a.season}.jsonl"))
    ledger = os.path.join(a.base_dir, "settlements", f"{a.season}.jsonl")
    settled = keys(ledger)
    receipts, refusals, written = [], [], []
    for index, row in enumerate(rows):
        k = row.get("source_bet_key")
        if k not in wagers:
            reason = f"no wager with this source_bet_key is in the {a.season} ledger"
            refusals.append({"row": index, "reason": reason})
            receipts.append({"row": index, "source_bet_key": k, "settlement_id": None,
                             "duplicate_status": "REFUSED", "success": False, "reason": reason})
            continue
        sid = "stl-" + hashlib.sha256(k.encode()).hexdigest()[:24]
        status = "DUPLICATE_NOOP" if k in settled else "NEW"
        if status == "NEW":
            os.makedirs(os.path.dirname(ledger), exist_ok=True)
            with open(ledger, "a") as h:
                h.write(json.dumps(dict(row, settlement_id=sid), sort_keys=True) + "\\n")
            settled.add(k)
            written.append(k)
        receipts.append({"row": index, "source_bet_key": k, "settlement_id": sid,
                         "duplicate_status": status, "success": True})

    print(f"season ledger: {a.season}")
    print(f"  written: {len(written)}  refused: {len(refusals)}")
    if a.receipts_out:
        json.dump({"season": a.season, "written": len(written),
                   "alreadyPresent": sum(r["duplicate_status"] == "DUPLICATE_NOOP" for r in receipts),
                   "refused": len(refusals), "refusals": refusals, "keysWritten": written,
                   "rows": receipts}, open(a.receipts_out, "w"), indent=2, sort_keys=True)
    return 1 if refusals else 0


if __name__ == "__main__":
    sys.exit(main())
'''

#: The destination validator's settlement rule (cfb-edge-finder's
#: `validate_accounting_ledger.py`, check 5): every settlement's wager is in
#: the same season's wager ledger.
FAKE_VALIDATOR = '''#!/usr/bin/env python3
import argparse, json, os, sys

ap = argparse.ArgumentParser()
ap.add_argument("--base-dir", required=True)
ap.add_argument("--result-out", default=None)
a = ap.parse_args()
problems = []
root = os.path.join(a.base_dir, "settlements")
for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
    wagers = os.path.join(a.base_dir, "wagers", name)
    known = {json.loads(l)["source_bet_key"] for l in open(wagers) if l.strip()} if os.path.exists(wagers) else set()
    for line in open(os.path.join(root, name)):
        if line.strip() and json.loads(line)["source_bet_key"] not in known:
            problems.append(f"{name}: a settlement refers to a wager this ledger has no record of")
if a.result_out:
    json.dump({"passed": not problems, "problems": problems}, open(a.result_out, "w"))
print(f"problems: {len(problems)}")
sys.exit(1 if problems else 0)
'''

CURL_SHIM = '''#!/usr/bin/env bash
out=""
while [ $# -gt 0 ]; do
  case "$1" in
    --output) out="$2"; shift 2 ;;
    *) shift ;;
  esac
done
[ -n "${out}" ] && printf '{"number": 1}' > "${out}"
printf '%s' "${CURL_STATUS:-201}"
'''


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def settle_step() -> str:
    config = yaml.safe_load(SETTLE.read_text())
    for job in config["jobs"].values():
        for step in job.get("steps") or []:
            if step.get("name") == "Deliver each destination through its own settlement importer":
                return step["run"]
    raise AssertionError("the settlement delivery step is gone from settle-wagers.yml")


@pytest.fixture
def world(tmp_path):
    """cfb-edge-finder in the 2026-09-26 shape: 41 wagers on `accounting-data`,
    2 more on the open, unmerged wager proposal, and 43 settled markets."""
    if shutil.which("git") is None:  # pragma: no cover
        pytest.skip("git is required to exercise the real settlement step")

    remote = tmp_path / "remotes" / f"{DESTINATION}.git"
    remote.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", CODE_BRANCH, str(remote)], check=True)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "--quiet", "-b", CODE_BRANCH, ".", cwd=seed)
    _git("config", "user.email", "seed@example.invalid", cwd=seed)
    _git("config", "user.name", "seed", cwd=seed)
    (seed / "scripts").mkdir()
    for name, text in (("import_routed_settlements.py", FAKE_SETTLEMENT_IMPORTER),
                       ("validate_accounting_ledger.py", FAKE_VALIDATOR)):
        (seed / "scripts" / name).write_text(text)
    (seed / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    _git("add", "-A", cwd=seed)
    _git("commit", "--quiet", "-m", "code", cwd=seed)
    _git("push", "--quiet", str(remote), CODE_BRANCH, cwd=seed)

    _git("checkout", "--quiet", "--orphan", LEDGER_BRANCH, cwd=seed)
    _git("rm", "-rf", "--quiet", ".", cwd=seed)
    (seed / "wagers").mkdir()
    (seed / "wagers" / f"{SEASON}.jsonl").write_text(
        jsonl(wager_row(i) for i in range(1, ON_LEDGER + 1))
    )
    (seed / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    _git("add", "-A", cwd=seed)
    _git("commit", "--quiet", "-m", "ledger", cwd=seed)
    _git("push", "--quiet", str(remote), LEDGER_BRANCH, cwd=seed)

    # The open wager proposal (PR #56's shape): the ledger plus 2 wagers,
    # pushed to the router's branch and NOT merged.
    _git("checkout", "--quiet", "-b", WAGER_PROPOSAL, cwd=seed)
    (seed / "wagers" / f"{SEASON}.jsonl").write_text(
        jsonl(wager_row(i) for i in range(1, ON_LEDGER + ON_PROPOSAL + 1))
    )
    _git("commit", "--quiet", "-am", "two more wagers", cwd=seed)
    _git("push", "--quiet", str(remote), WAGER_PROPOSAL, cwd=seed)

    runner_temp = tmp_path / "runner_temp"
    (runner_temp / "settlements").mkdir(parents=True)
    payload = runner_temp / "settlements" / "CFB-settlements.json"
    payload.write_text(json.dumps(
        {"settlements": [settlement_row(i) for i in range(1, ON_LEDGER + ON_PROPOSAL + 1)]},
        indent=2, sort_keys=True,
    ))

    home = tmp_path / "home"
    home.mkdir()
    gitconfig = home / "gitconfig"
    gitconfig.write_text(
        f'[url "file://{tmp_path}/remotes/"]\n\tinsteadOf = https://github.com/\n'
        "[init]\n\tdefaultBranch = main\n[protocol]\n\tallow = always\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(CURL_SHIM)
    (bin_dir / "curl").chmod(0o755)
    summary = tmp_path / "step_summary.md"
    summary.write_text("")

    stub = GitHubStub(remote, DESTINATION, branch=SETTLEMENT_BRANCH, base_branch=LEDGER_BRANCH,
                      check_runs=())
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
    )
    try:
        yield {"tmp": tmp_path, "env": env, "remote": remote, "summary": summary,
               "seed": seed, "receipts": runner_temp / "settle-receipts-CFB.json"}
    finally:
        stub.stop()


def run_settle(w) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", settle_step()], env=w["env"], cwd=w["tmp"],
                          capture_output=True, text=True, check=False)


def remote_keys(w, ref: str, path: str) -> list[str]:
    shown = subprocess.run(["git", "-C", str(w["remote"]), "show", f"{ref}:{path}"],
                           capture_output=True, text=True, check=False)
    if shown.returncode != 0:
        return []
    return [json.loads(line)["source_bet_key"] for line in shown.stdout.splitlines() if line.strip()]


def remote_tree(w, ref: str) -> str:
    return _git("rev-parse", f"{ref}^{{tree}}", cwd=w["remote"]).strip()


def merge_wager_proposal(w):
    """What a person does when they have read PR #56: merge it."""
    _git("push", "--quiet", "--force", str(w["remote"]), f"{WAGER_PROPOSAL}:{LEDGER_BRANCH}",
         cwd=w["seed"])


SETTLEMENTS_FILE = f"settlements/{SEASON}.jsonl"


def test_G_valid_settlements_are_written_and_proposed_while_their_siblings_wagers_are_unmerged(world):
    """THE INCIDENT, END TO END. Before the fix this run printed "the season
    could not be established" and imported nothing; the 41 valid settlements
    were thrown away on every run until a person merged the wager proposal."""
    result = run_settle(world)
    out = result.stdout + result.stderr

    assert "the season could not be established" not in out
    assert f"season ledger: {SEASON}" in result.stdout
    assert f"canonical wager matches: {ON_LEDGER}" in result.stderr
    assert f"unmatched settlement parents: {ON_PROPOSAL}" in result.stderr

    # The importer ran, wrote the 41 and refused the 2, per row.
    receipts = json.loads(world["receipts"].read_text())
    assert receipts["written"] == ON_LEDGER
    assert receipts["refused"] == ON_PROPOSAL

    # The 41 are on the settlement proposal; the 2 are not anywhere canonical.
    proposed = remote_keys(world, SETTLEMENT_BRANCH, SETTLEMENTS_FILE)
    assert sorted(proposed) == sorted(key(i) for i in range(1, ON_LEDGER + 1))
    assert not {key(i) for i in range(ON_LEDGER + 1, ON_LEDGER + ON_PROPOSAL + 1)} & set(proposed)
    assert remote_keys(world, LEDGER_BRANCH, SETTLEMENTS_FILE) == [], "nothing merged itself"

    # The partial condition is loud, and the run is still red for it.
    assert "refused at least one settlement" in result.stdout
    assert "DELIVERED PARTIALLY" in result.stdout
    assert "PARTIAL settlement" in world["summary"].read_text()
    assert result.returncode == 1

    # Every one of the 43 is accounted for; the 2 are REFUSED and named as
    # awaiting their wager -- never proposed, never on the ledger, never lost.
    assert (f"on ledger 0, proposed not merged {ON_LEDGER}, refused {ON_PROPOSAL} "
            f"{{'REFUSED': {ON_PROPOSAL}}} ({ON_PROPOSAL} of them await their wager") in result.stdout
    assert "UNACCOUNTED 0" in result.stdout
    assert "settlement reconciliation by identity found" not in result.stdout

    assert "a second identical settlement import changed nothing" in result.stdout


def test_F_a_second_identical_run_changes_nothing(world):
    first = run_settle(world)
    assert first.returncode == 1, first.stdout + first.stderr
    tree = remote_tree(world, SETTLEMENT_BRANCH)

    second = run_settle(world)
    assert second.returncode == 1, second.stdout + second.stderr
    assert remote_tree(world, SETTLEMENT_BRANCH) == tree
    assert "a second identical settlement import changed nothing" in second.stdout
    assert "Refusing to push a non-idempotent import" not in second.stdout


def test_once_the_wager_proposal_merges_the_deferred_settlements_land_with_no_one_touching_them(world):
    """The refusal was a deferral in fact: the next run re-offers every
    settled market, and the 2 now have their wagers on the ledger."""
    assert run_settle(world).returncode == 1
    merge_wager_proposal(world)

    result = run_settle(world)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "unmatched settlement parents: 0" in result.stderr
    assert "DELIVERED PARTIALLY" not in result.stdout
    proposed = remote_keys(world, SETTLEMENT_BRANCH, SETTLEMENTS_FILE)
    assert sorted(proposed) == sorted(key(i) for i in range(1, ON_LEDGER + ON_PROPOSAL + 1))
    assert f"proposed not merged {ON_LEDGER + ON_PROPOSAL}, refused 0" in result.stdout
    assert "UNACCOUNTED 0" in result.stdout
    # CFB is still held for observation: the gate passed and nothing merged.
    assert "HELD FOR OBSERVATION" in result.stdout
    assert remote_keys(world, LEDGER_BRANCH, SETTLEMENTS_FILE) == []


def test_every_row_unmatched_still_imports_nothing(world):
    """B, through the workflow: no importer run, no branch, a red run."""
    payload = world["tmp"] / "runner_temp" / "settlements" / "CFB-settlements.json"
    payload.write_text(json.dumps({"settlements": [settlement_row(i) for i in (900, 901)]}))
    result = run_settle(world)
    assert result.returncode == 1
    assert "the season could not be established" in result.stdout
    assert not world["receipts"].exists(), "the importer must not have run"
    assert remote_keys(world, SETTLEMENT_BRANCH, SETTLEMENTS_FILE) == []


def test_the_run_log_carries_no_key_ticker_or_payout(world):
    result = run_settle(world)
    combined = result.stdout + result.stderr + world["summary"].read_text()
    for secret in ("kalshi:v1", "KXNCAAF", "9.83", "20.0"):
        assert secret not in combined, f"the settlement log leaked {secret!r}"


# ------------------------------------------------------------- H: NFL


def test_H_nfl_never_reaches_the_season_script():
    """The fix lives in the season script, and only a destination that
    requires a season runs it. NFL resolves its week inside its own importer."""
    assert profile_for("NFL").requires_season is False
    assert PROFILES[Sport.CFB].requires_season is True
    step = settle_step()
    gate = step.index('if [ "${needs_season}" = "true" ]; then')
    call = step.index("scripts/settlement_season.py")
    assert gate < call < step.index("fi", call)
