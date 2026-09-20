"""The bankroll leaves this repository through exactly one channel.

Both repositories in this system are PUBLIC, so every surface a workflow
can write to -- logs, job summaries, artifacts, commits -- is a public
surface. These tests pin the two properties that follow from that:

  * the amount is never printed, committed or uploaded;
  * the only thing that carries it is an encrypted Actions secret.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import yaml

from kalshi_router import cli

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish-bankroll.yml"

#: A balance whose digits are distinctive enough that any leak is findable.
BALANCE_CENTS = 731917          # $7,319.17
BALANCE_DIGITS = ("731917", "7319.17", "7,319.17")


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload

    def get_balance(self):
        return self._payload


def _run_bankroll(tmp_path, payload):
    out, err = io.StringIO(), io.StringIO()
    args = cli.build_parser().parse_args(
        ["bankroll", "--out", str(tmp_path / "bankroll.json")])
    code = cli._run_bankroll(args, _FakeClient(payload), out, err)
    return code, out.getvalue(), err.getvalue()


# ------------------------------------------------------------------- the CLI

def test_the_cli_writes_the_context_but_never_prints_the_amount(tmp_path):
    code, out, err = _run_bankroll(tmp_path, {"balance": BALANCE_CENTS})
    assert code == cli.EXIT_OK

    written = json.loads((tmp_path / "bankroll.json").read_text(encoding="utf-8"))
    assert written["bankroll"] == pytest.approx(7319.17)

    combined = out + err
    for digits in BALANCE_DIGITS:
        assert digits not in combined, f"the amount leaked into stdout/stderr: {combined!r}"
    assert "withheld" in combined
    assert written["observedAt"] in out          # the SHAPE is reportable


def test_the_written_file_carries_no_account_metadata(tmp_path):
    _run_bankroll(tmp_path, {
        "balance": BALANCE_CENTS,
        "portfolio_value": 999999,
        "account_id": "ACCT-LEAK",
        "balance_breakdown": [{"exchange_index": 0, "balance": BALANCE_CENTS}],
    })
    blob = (tmp_path / "bankroll.json").read_text(encoding="utf-8")
    for forbidden in ("ACCT-LEAK", "portfolio_value", "balance_breakdown", "999999"):
        assert forbidden not in blob


def test_a_failed_balance_read_publishes_nothing(tmp_path):
    """No fallback, no last-known value, no file. A bankroll this process
    could not read is one the destination must be told it does not have."""
    code, _out, err = _run_bankroll(tmp_path, {"portfolio_value": 999999})
    assert code == cli.EXIT_API
    assert not (tmp_path / "bankroll.json").exists()
    assert "999999" not in err


def test_the_context_file_is_not_world_readable(tmp_path):
    _run_bankroll(tmp_path, {"balance": BALANCE_CENTS})
    mode = (tmp_path / "bankroll.json").stat().st_mode & 0o777
    assert mode == 0o600


# -------------------------------------------------------------- the workflow

@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_workflow_uploads_no_artifact(workflow):
    """On a public repository an artifact is a public download link."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "upload-artifact" not in text


def test_the_workflow_writes_nothing_to_the_job_summary(workflow):
    assert "GITHUB_STEP_SUMMARY" not in WORKFLOW.read_text(encoding="utf-8")


def test_the_workflow_has_no_write_permission_on_this_repository(workflow):
    assert workflow["permissions"] == {"contents": "read"}


def test_the_workflow_refuses_a_ref_other_than_main(workflow):
    steps = workflow["jobs"]["publish"]["steps"]
    assert any("refs/heads/main" in (step.get("run") or "") for step in steps)


def test_the_context_is_written_to_runner_temp_not_the_workspace(workflow):
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "${RUNNER_TEMP}/bankroll.json" in text
    assert "GITHUB_WORKSPACE}/bankroll" not in text


def test_the_workflow_uses_a_secrets_token_distinct_from_the_delivery_token():
    """Writing a secret and pushing a branch are different powers, and the
    delivery token should not silently acquire the first."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "DOWNSTREAM_SECRETS_TOKEN" in text
    assert "DOWNSTREAM_REPO_TOKEN" not in text


def test_the_workflow_does_not_enable_any_trading_command():
    text = WORKFLOW.read_text(encoding="utf-8")
    # The only CLI command this workflow runs. `bankroll` issues one
    # GET /portfolio/balance; the client has no mutating capability at all
    # (tests/test_client.py::test_no_mutating_method_exists_on_the_client).
    commands = [
        line.strip() for line in text.splitlines()
        if "kalshi_router.cli" in line
    ]
    assert commands and all("cli bankroll" in line for line in commands), commands


# ── the publisher is a SINK in the cross-repo call graph ──────────────
#
# edge-finder-api's fetch-slate.yml now DISPATCHES this workflow before it
# builds a real-money handicapping card, so the card never sizes against a
# reading nobody refreshed (see that repo's
# scripts/ci/refresh_bankroll_context.py and
# tests/test_bankroll_refresh_coupling.py).
#
# That makes the call graph edge-finder-api -> this publisher. A single
# directed edge cannot cycle -- but only for as long as this end stays a
# SINK. The moment this workflow dispatches anything back, two repositories
# can trigger each other forever, each run burning a real authenticated
# Kalshi read. The destination's own suite cannot check that, because this
# file does not live there. So it is checked here.

#: Ways a workflow can trigger something in another repository.
_DISPATCH_SHAPES = (
    "repository_dispatch",
    "/dispatches",
    "workflow_run",
    "peter-evans/repository-dispatch",
    "benc-uk/workflow-dispatch",
    "gh workflow run",
    "gh api",
)


def test_the_publisher_dispatches_nothing_back():
    """The other half of the loop-safety proof. This workflow's ONLY
    outbound call to the destination is the sealed-secret PUT, which
    triggers no workflow there: an Actions secret write fires no event."""
    text = WORKFLOW.read_text(encoding="utf-8")
    offenders = [shape for shape in _DISPATCH_SHAPES if shape in text]
    assert offenders == [], (
        f"publish-bankroll.yml can now trigger another repository ({offenders}). "
        "edge-finder-api dispatches THIS workflow, so anything dispatched back "
        "closes a cross-repo loop that would read the account forever.")


def test_the_publisher_is_reachable_by_dispatch_at_all():
    """The coupling depends on it. A schedule-only publisher could not be
    pulled just in time, and the destination would be back to hoping an
    unrelated cron fired recently."""
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = spec.get(True) or spec.get("on")
    assert "workflow_dispatch" in triggers, triggers


def test_the_publisher_still_runs_on_its_own_schedule_as_a_backup():
    """The cron may stay -- it just must not be the only correctness
    mechanism for dollar sizing any more. Removing it would make a missed
    dispatch mean no reading at all."""
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = spec.get(True) or spec.get("on")
    assert "schedule" in triggers, triggers


def test_a_second_dispatch_cannot_cancel_a_reading_already_in_flight():
    """This workflow declares cancel-in-progress, so a duplicate dispatch
    would KILL the run the destination is waiting on and leave the secret
    exactly as stale as it was. The destination therefore adopts an
    in-flight run rather than dispatching a second one
    (edge-finder-api tests/test_bankroll_refresh_coupling.py::
    test_a_run_already_in_flight_is_adopted_rather_than_duplicated).
    This test pins the premise that makes that adoption necessary, so the
    two cannot drift apart silently."""
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    concurrency = spec["concurrency"]
    assert concurrency["group"] == "publish-bankroll"
    assert concurrency["cancel-in-progress"] is True
