"""The NFL bankroll destination, and the proof it leaves MLB alone.

`publish-bankroll-nfl.yml` seals the same available-cash reading into
`chmoses98/nfl-edge-finder`. Both repositories are PUBLIC, so the properties
pinned here are the same as for the MLB publisher (the amount is never
printed, committed or uploaded; only an encrypted Actions secret carries it)
plus the ones that are specific to a second destination:

  * it targets the NFL repository, with its OWN token, so the MLB token is
    not widened;
  * it shares no concurrency group, so neither run can cancel the other;
  * `publish-bankroll.yml` still targets only MLB and is not coupled to it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
NFL_PATH = ROOT / ".github" / "workflows" / "publish-bankroll-nfl.yml"
MLB_PATH = ROOT / ".github" / "workflows" / "publish-bankroll.yml"
PUBLISHER = ROOT / "scripts" / "publish_bankroll_secret.py"
DOCS = ROOT / "docs" / "BANKROLL_DELIVERY.md"

NFL_REPO = "chmoses98/nfl-edge-finder"
MLB_REPO = "chmoses98/edge-finder-api"
NFL_TOKEN = "DOWNSTREAM_SECRETS_TOKEN_NFL"
MLB_TOKEN = "DOWNSTREAM_SECRETS_TOKEN"
SECRET_NAME = "KALSHI_BANKROLL_CONTEXT"


@pytest.fixture(scope="module")
def text():
    return NFL_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow(text):
    return yaml.safe_load(text)


@pytest.fixture(scope="module")
def mlb_text():
    return MLB_PATH.read_text(encoding="utf-8")


def steps(workflow):
    return workflow["jobs"]["publish"]["steps"]


def code_lines(text):
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


# ------------------------------------------------------------ the target

def test_it_targets_the_nfl_repository_and_the_agreed_secret_name(text, workflow):
    envs = [s.get("env") or {} for s in steps(workflow)]
    destinations = {e["DESTINATION_REPO"] for e in envs if "DESTINATION_REPO" in e}
    secrets_ = {e["DESTINATION_SECRET"] for e in envs if "DESTINATION_SECRET" in e}
    assert destinations == {NFL_REPO}
    assert secrets_ == {SECRET_NAME}
    assert MLB_REPO not in text, "the NFL workflow must never name the MLB destination"


def test_every_publisher_call_names_the_destination_and_the_nfl_token(workflow):
    calls = [s["run"] for s in steps(workflow)
             if "publish_bankroll_secret.py" in (s.get("run") or "")]
    assert len(calls) == 2            # preflight + seal
    for run in calls:
        assert '--repo "${DESTINATION_REPO}"' in run
        assert '--secret-name "${DESTINATION_SECRET}"' in run
        assert f"--token-env {NFL_TOKEN}" in run


def test_it_uses_its_own_token_and_never_the_mlb_or_delivery_token(text, workflow):
    for step in steps(workflow):
        env = step.get("env") or {}
        if "DESTINATION_REPO" in env:
            assert env[NFL_TOKEN] == "${{ secrets.%s }}" % NFL_TOKEN
            assert MLB_TOKEN not in env
    # `DOWNSTREAM_SECRETS_TOKEN` only ever appears as the prefix of the NFL
    # name, or in the explanatory annotation about the shared script's wording.
    stripped = text.replace(NFL_TOKEN, "")
    secret_refs = [ln for ln in code_lines(stripped) if "secrets." in ln and MLB_TOKEN in ln]
    assert secret_refs == []
    assert "DOWNSTREAM_REPO_TOKEN" not in text


def test_the_mlb_token_is_not_widened(mlb_text):
    """The MLB workflow still uses the MLB token, never the NFL one."""
    assert NFL_TOKEN not in mlb_text
    assert NFL_REPO not in mlb_text


# ------------------------------------------------------------ safeguards

def test_it_refuses_a_ref_other_than_main_before_anything_else(workflow):
    first = steps(workflow)[0]
    assert "refs/heads/main" in first["run"]
    assert 'exit 1' in first["run"]


def test_it_holds_no_write_permission_on_this_repository(workflow):
    assert workflow["permissions"] == {"contents": "read"}


def test_trigger_and_cadence_are_offset_schedule_plus_dispatch(workflow):
    triggers = workflow.get(True) or workflow.get("on")
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    crons = [s["cron"] for s in triggers["schedule"]]
    assert crons == ["7,22,37,52 * * * *"]
    mlb = yaml.safe_load(MLB_PATH.read_text(encoding="utf-8"))
    mlb_triggers = mlb.get(True) or mlb.get("on")
    assert crons != [s["cron"] for s in mlb_triggers["schedule"]]


def test_it_has_its_own_concurrency_group(workflow):
    mlb = yaml.safe_load(MLB_PATH.read_text(encoding="utf-8"))
    assert workflow["concurrency"]["group"] == "publish-bankroll-nfl"
    assert workflow["concurrency"]["group"] != mlb["concurrency"]["group"]
    assert workflow["concurrency"]["cancel-in-progress"] is True


def test_the_credential_is_verified_before_the_balance_is_read(workflow):
    names = [s.get("name") or "" for s in steps(workflow)]
    pre = next(i for i, n in enumerate(names) if "credential" in n.lower())
    read = next(i for i, n in enumerate(names) if "Read the balance" in n)
    seal = next(i for i, n in enumerate(names) if n.startswith("Seal it"))
    assert pre < read < seal, names


def test_only_the_read_step_holds_kalshi_credentials(workflow):
    for step in steps(workflow):
        env = step.get("env") or {}
        holds = {"KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY"} & set(env)
        if step.get("name") == "Read the balance":
            assert holds == {"KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY"}
        else:
            assert not holds, step.get("name")


def test_the_preflight_step_reads_no_account(workflow):
    step = next(s for s in steps(workflow)
                if (s.get("name") or "").lower().startswith("verify the delivery"))
    assert "--preflight-only" in step["run"]
    assert "kalshi_router.cli" not in step["run"]


def test_it_only_ever_runs_the_balance_command(text):
    commands = [ln for ln in text.splitlines() if "kalshi_router.cli" in ln
                and not ln.strip().startswith("#")]
    assert commands and all("cli bankroll" in ln for ln in commands), commands


# ------------------------------------------------------------ privacy

def test_the_context_goes_to_runner_temp_not_the_workspace(text):
    assert "${RUNNER_TEMP}/bankroll.json" in text
    assert "GITHUB_WORKSPACE}/bankroll" not in text


def test_nothing_is_uploaded_summarised_or_committed(text):
    assert "upload-artifact" not in text
    assert "GITHUB_STEP_SUMMARY" not in text
    for line in code_lines(text):
        assert "git push" not in line and "git commit" not in line and "git add" not in line


def test_the_amount_is_never_echoed_or_read_by_the_workflow(text):
    for line in code_lines(text):
        assert "ctx['bankroll']" not in line and 'ctx["bankroll"]' not in line
        assert "bankroll'" not in line.replace("kalshi_router.cli bankroll", "")
        # no shell route that could render the file's contents either
        assert not any(tok in line for tok in ("cat ", "jq ", "$(<", "tee ")), line
    assert 'amount:     ***withheld***' in text


def test_the_report_step_prints_shape_only(workflow):
    report = next(s for s in steps(workflow) if (s.get("name") or "").startswith("Report"))
    assert report["if"] == "always()"
    printed = [ln.strip() for ln in report["run"].splitlines()
               if ln.strip().startswith("print(")]
    assert printed, "report step prints nothing"
    for ln in printed:
        assert "bankroll" not in ln
    keys = {k for ln in printed for k in
            ("observedAt", "valueType", "source", "currency") if f"ctx['{k}']" in ln}
    assert keys == {"observedAt", "valueType", "source", "currency"}


def test_it_dispatches_nothing_back_so_it_stays_a_sink(text):
    for shape in ("repository_dispatch", "/dispatches", "workflow_run",
                  "peter-evans/repository-dispatch", "benc-uk/workflow-dispatch",
                  "gh workflow run", "gh api"):
        assert shape not in text, shape


# ------------------------------------------------------------ the owner's remedy

def test_the_error_annotations_name_the_exact_owner_remedy_and_consequence(text):
    errors = [ln for ln in text.splitlines() if "::error::" in ln]
    assert errors
    credential = [ln for ln in errors if NFL_TOKEN in ln]
    assert credential, "no annotation names DOWNSTREAM_SECRETS_TOKEN_NFL"
    joined = " ".join(credential)
    assert "Secrets: Read and write" in joined
    assert NFL_REPO in joined or "${DESTINATION_REPO}" in joined
    assert "NOTHING was read from the Kalshi account" in joined
    assert "authorises NO stakes" in joined
    # every failure path says what it means for the NFL card
    for ln in errors:
        if "refs/heads/main" in ln:
            continue
        assert "NO stakes" in ln, ln


# ------------------------------------------------------------ MLB untouched

def test_the_mlb_publisher_still_targets_only_mlb(mlb_text):
    mlb = yaml.safe_load(mlb_text)
    assert mlb["concurrency"]["group"] == "publish-bankroll"
    envs = [s.get("env") or {} for s in mlb["jobs"]["publish"]["steps"]]
    assert {e["DESTINATION_REPO"] for e in envs if "DESTINATION_REPO" in e} == {MLB_REPO}
    assert {e[MLB_TOKEN] for e in envs if MLB_TOKEN in e} == {
        "${{ secrets.%s }}" % MLB_TOKEN}
    triggers = mlb.get(True) or mlb.get("on")
    assert [s["cron"] for s in triggers["schedule"]] == ["*/15 * * * *"]
    assert "workflow_dispatch" in triggers
    assert "--token-env" not in mlb_text


def test_the_publisher_script_default_token_is_still_the_mlb_one():
    """The NFL job selects its token with --token-env; the default, which the
    MLB workflow relies on, must not move."""
    src = PUBLISHER.read_text(encoding="utf-8")
    assert 'parser.add_argument("--token-env", default="DOWNSTREAM_SECRETS_TOKEN")' in src


# ------------------------------------------------------------ the script, run for NFL

def _preflight(env):
    environment = {"PATH": "/usr/bin:/bin:/usr/local/bin",
                   "PYTHONPATH": str(ROOT / "src"),
                   # unreachable: any request would fail loudly as exit 4, not 3
                   "GITHUB_API_ROOT": "http://127.0.0.1:1"}
    environment.update(env)
    return subprocess.run(
        [sys.executable, str(PUBLISHER), "--preflight-only", "--token-env", NFL_TOKEN,
         "--repo", NFL_REPO, "--secret-name", SECRET_NAME],
        capture_output=True, text=True, env=environment)


def test_a_missing_nfl_token_is_a_credential_failure_before_any_request():
    result = _preflight({})
    assert result.returncode == 3
    assert "Secrets: Read and write" in result.stderr


def test_the_mlb_token_does_not_stand_in_for_the_nfl_token():
    result = _preflight({MLB_TOKEN: "ghp_mlb_token_must_not_be_used"})
    assert result.returncode == 3
    assert "ghp_mlb_token_must_not_be_used" not in result.stdout + result.stderr


# ------------------------------------------------------------ docs

def test_the_docs_describe_the_nfl_destination():
    doc = DOCS.read_text(encoding="utf-8")
    section = doc[doc.index("## 7. The NFL destination"):]
    for needle in (NFL_REPO, NFL_TOKEN, SECRET_NAME, "AVAILABLE CASH",
                   "30 minutes", "7,22,37,52", "publish-bankroll-nfl.yml",
                   "authorises no stakes", "Secrets: Read and write"):
        assert needle in section, needle
