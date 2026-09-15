"""Proof that the public workflow cannot disclose individual wagers.

These tests read the committed workflow YAML, so a future edit that adds an
artifact upload, a sensitive flag, a write permission, or a fork-PR trigger
fails CI rather than leaking the owner's betting activity.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
AUDIT_WORKFLOW = ROOT / ".github/workflows/phase0-readonly-audit.yml"
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def triggers(config: dict) -> dict:
    # PyYAML parses the bare key `on` as the boolean True.
    return config.get("on") if "on" in config else config.get(True)


def run_blocks(config: dict) -> list[str]:
    blocks = []
    for job in (config.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if isinstance(step.get("run"), str):
                blocks.append(step["run"])
    return blocks


def steps(config: dict) -> list[dict]:
    return [s for job in (config.get("jobs") or {}).values() for s in (job.get("steps") or [])]


@pytest.fixture(scope="module")
def audit() -> dict:
    return load(AUDIT_WORKFLOW)


def strip_comments(text: str) -> str:
    """Drop whole-line comments so assertions test the workflow, not its prose."""
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


@pytest.fixture(scope="module")
def audit_text() -> str:
    """The workflow's executable content, with explanatory comments removed."""
    return strip_comments(AUDIT_WORKFLOW.read_text())


@pytest.fixture(scope="module")
def audit_raw() -> str:
    return AUDIT_WORKFLOW.read_text()


# ------------------------------------------------------------------ triggers

def test_audit_workflow_is_manual_dispatch_only(audit):
    assert set(triggers(audit)) == {"workflow_dispatch"}


def test_audit_workflow_never_uses_pull_request_target(audit_text):
    assert "pull_request_target" not in audit_text


def test_audit_workflow_is_not_scheduled(audit):
    assert "schedule" not in triggers(audit)


def test_comments_document_the_security_posture(audit_raw):
    assert "public" in audit_raw.lower()
    assert "pull_request_target" in audit_raw, "the comment should explain the omission"


def test_audit_workflow_refuses_untrusted_refs(audit_text):
    assert "refs/heads/main" in audit_text
    assert "exit 1" in audit_text


# --------------------------------------------------------------- permissions

def test_audit_workflow_has_read_only_permissions(audit):
    assert audit["permissions"] == {"contents": "read"}


def test_audit_workflow_does_not_request_write_scopes(audit_text):
    for scope in ("contents: write", "packages: write", "id-token: write", "issues: write"):
        assert scope not in audit_text


def test_checkout_does_not_persist_credentials(audit):
    checkout = [s for s in steps(audit) if str(s.get("uses", "")).startswith("actions/checkout")]
    assert checkout, "expected a checkout step"
    assert all(s.get("with", {}).get("persist-credentials") is False for s in checkout)


# ------------------------------------------------------- no durable exfil path

def test_audit_workflow_uploads_no_artifacts(audit_text):
    assert "upload-artifact" not in audit_text


def test_audit_workflow_caches_nothing(audit_text):
    assert "actions/cache" not in audit_text
    assert "cache:" not in audit_text


def test_audit_workflow_does_not_commit_or_dispatch_downstream(audit_text):
    for forbidden in ("git commit", "git push", "repository_dispatch", "workflow_dispatch\n        with", "gh api", "peter-evans"):
        assert forbidden not in audit_text


def test_audit_workflow_writes_no_files(audit_text):
    for redirect in (" > ", ">>", "tee "):
        for block in (line for line in audit_text.splitlines() if line.strip().startswith("python")):
            assert redirect not in block


# ------------------------------------------------- cannot print raw fills

def test_audit_workflow_never_passes_the_sensitive_flag(audit_text):
    assert "--show-sensitive-details" not in audit_text


def test_audit_workflow_only_invokes_the_aggregate_audit(audit):
    invocations = [
        line.strip()
        for block in run_blocks(audit)
        for line in block.splitlines()
        if "kalshi_router" in line
    ]
    assert invocations, "expected the audit command to be invoked"
    for line in invocations:
        assert line.startswith("python -m kalshi_router.cli audit")
        assert "--show-sensitive-details" not in line


def test_secrets_are_only_exposed_to_the_audit_step(audit):
    exposing = [s for s in steps(audit) if "secrets." in str(s.get("env", {}))]
    assert len(exposing) == 1
    env = exposing[0]["env"]
    assert set(k for k, v in env.items() if "secrets." in str(v)) == {
        "KALSHI_API_KEY_ID",
        "KALSHI_PRIVATE_KEY",
    }


def test_secrets_are_never_echoed_or_placed_on_a_command_line(audit_text):
    for line in audit_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("echo", "python", "run:")):
            assert "secrets." not in stripped
            assert "KALSHI_PRIVATE_KEY" not in stripped
            assert "KALSHI_API_KEY_ID" not in stripped


# ------------------------------------------------------------- CI workflow

def test_ci_workflow_receives_no_secrets():
    text = strip_comments(CI_WORKFLOW.read_text())
    assert "secrets." not in text
    assert "KALSHI_PRIVATE_KEY" not in text
    assert "KALSHI_API_KEY_ID" not in text


def test_ci_workflow_is_read_only():
    assert load(CI_WORKFLOW)["permissions"] == {"contents": "read"}


def test_ci_workflow_runs_the_test_suite():
    assert any("pytest" in block for block in run_blocks(load(CI_WORKFLOW)))


# ============ the downstream delivery credential, and where it may appear =====

PROBE_WORKFLOW = ROOT / ".github/workflows/downstream-credential-probe.yml"
WORKFLOW_DIR = ROOT / ".github/workflows"

#: The one secret that can WRITE outside this repository. Everything about it is
#: blast radius, so the rules below are about where it may appear at all.
DOWNSTREAM_SECRET = "DOWNSTREAM_REPO_TOKEN"


def workflow_files() -> list[Path]:
    return sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))


def workflows_referencing(secret: str) -> list[str]:
    return [p.name for p in workflow_files() if secret in p.read_text()]


@pytest.fixture(scope="module")
def probe() -> dict:
    return load(PROBE_WORKFLOW)


@pytest.fixture(scope="module")
def probe_text() -> str:
    return strip_comments(PROBE_WORKFLOW.read_text())


def test_the_readonly_audit_never_receives_the_downstream_credential():
    """The research workflow reads Kalshi. It has no business writing anywhere.

    Stated as a property of the audit workflow rather than a review habit: a
    future edit that hands it the delivery token fails CI.
    """
    assert DOWNSTREAM_SECRET not in AUDIT_WORKFLOW.read_text()


def test_ci_never_receives_the_downstream_credential():
    # CI runs on pull requests, including from forks on a public repository.
    assert DOWNSTREAM_SECRET not in CI_WORKFLOW.read_text()


def test_only_credentialed_workflows_name_the_downstream_secret():
    """An allowlist, so a new workflow cannot quietly acquire write access.

    Adding a delivery workflow is a deliberate act; this test makes it one.
    """
    allowed = {
        "downstream-credential-probe.yml",
        "deliver-wagers.yml",
        # Phase 12 recovery. Added here deliberately -- this test failed the
        # moment the workflow was written, which is the point of the allowlist.
        "recover-wagers.yml",
    }
    assert set(workflows_referencing(DOWNSTREAM_SECRET)) <= allowed


def test_the_probe_is_dispatch_only_and_never_runs_on_a_pull_request(probe):
    events = triggers(probe)
    assert set(events) == {"workflow_dispatch"}
    # pull_request_target on a public repo would hand fork code the secret.
    assert "pull_request_target" not in events
    assert "pull_request" not in events
    assert "schedule" not in events


def test_the_probe_holds_no_write_permission_on_this_repository(probe):
    # The downstream token is what writes downstream. GITHUB_TOKEN stays read.
    assert probe["permissions"] == {"contents": "read"}


def test_the_probe_refuses_to_run_from_a_ref_other_than_main(probe_text):
    assert "refs/heads/main" in probe_text


def test_the_probe_never_puts_the_credential_in_a_url(probe_text):
    """A URL reaches server logs, proxy logs and any redirect target.

    The token must travel as an Authorization header and nowhere else.
    """
    assert "Authorization: Bearer" in probe_text
    for leak in ("://${DOWNSTREAM_REPO_TOKEN}", "@github.com", "x-access-token:"):
        assert leak not in probe_text


def test_the_probe_never_echoes_or_traces_the_credential(probe_text):
    assert "set -x" not in probe_text
    # Printing the value, a prefix or a suffix all identify the token.
    for leak in (
        "echo ${DOWNSTREAM_REPO_TOKEN}",
        'echo "${DOWNSTREAM_REPO_TOKEN}"',
        "${DOWNSTREAM_REPO_TOKEN:0:",
        "${DOWNSTREAM_REPO_TOKEN: -",
    ):
        assert leak not in probe_text


def test_the_probe_writes_nothing_anywhere(probe_text):
    """It proves a write credential by READING repository metadata.

    A probe that creates a branch to prove it can create a branch has to be
    trusted to clean up after itself, and the thing it would be writing next to
    is canonical wager data.
    """
    lowered = probe_text.lower()
    for mutation in (
        '-x post', '-x put', '-x patch', '-x delete',
        '--request post', '--request put', '--request delete',
        "git push", "git commit", "/git/refs", "/contents/", "/pulls",
    ):
        assert mutation not in lowered


def test_the_probe_uploads_no_artifact(probe):
    for step in steps(probe):
        assert "upload-artifact" not in (step.get("uses") or "")


def test_the_probe_covers_exactly_the_four_destinations(probe_text):
    for repo in (
        "chmoses98/edge-finder-api",
        "chmoses98/nfl-edge-finder",
        "chmoses98/cfb-edge-finder",
        "chmoses98/Tennis-Edge-Finder",
    ):
        assert repo in probe_text


def test_the_probe_reads_permissions_structurally_not_by_substring(probe_text):
    """`grep '"push": true'` would match that string anywhere in the body.

    Reporting write access the token does not have is the one failure a
    credential probe must not have, so the field is read with jq.
    """
    assert "jq -r '.permissions.push" in probe_text
    assert "grep" not in probe_text


# ================== the delivery workflow: the credentialed boundary ==========

DELIVER_WORKFLOW = ROOT / ".github/workflows/deliver-wagers.yml"


@pytest.fixture(scope="module")
def deliver() -> dict:
    return load(DELIVER_WORKFLOW)


@pytest.fixture(scope="module")
def deliver_text() -> str:
    return strip_comments(DELIVER_WORKFLOW.read_text())


def test_delivery_never_runs_on_a_pull_request(deliver):
    events = triggers(deliver)
    assert "pull_request" not in events
    assert "pull_request_target" not in events


def test_delivery_refuses_a_ref_other_than_main(deliver_text):
    assert "refs/heads/main" in deliver_text


def test_delivery_holds_no_write_permission_on_this_repository(deliver):
    # The downstream token writes downstream. GITHUB_TOKEN stays read.
    assert deliver["permissions"] == {"contents": "read"}


def test_delivery_never_cancels_itself_in_flight(deliver):
    """Cancellation between the destination's commit and this job's receipt
    would leave a canonical wager written with no record here that it was."""
    assert deliver["concurrency"]["cancel-in-progress"] is False


def test_delivery_uploads_no_artifact(deliver):
    """The payload is the owner's betting activity. An artifact publishes it."""
    for step in steps(deliver):
        assert "upload-artifact" not in (step.get("uses") or "")


def test_delivery_never_puts_the_credential_in_a_url_or_argv(deliver_text):
    assert "set -x" not in deliver_text
    for leak in (
        "://${DOWNSTREAM_REPO_TOKEN}",
        "x-access-token:${DOWNSTREAM_REPO_TOKEN}",
        "@github.com",
        "--password",
        "extraheader",
    ):
        assert leak not in deliver_text
    # The helper reads the token from the environment when a push happens.
    assert "credential.helper" in deliver_text


def test_delivery_writes_the_payload_outside_the_repository(deliver_text):
    """RUNNER_TEMP, never the workspace -- a payload in the workspace is one
    `git add -A` away from being committed into this public repository."""
    assert "${RUNNER_TEMP}/payloads" in deliver_text
    assert "--out-dir" in deliver_text


def test_delivery_never_prints_the_payload(deliver_text):
    for leak in ("cat ${payload}", 'cat "${payload}"', "cat $payload"):
        assert leak not in deliver_text


def test_delivery_runs_the_destinations_own_importer(deliver_text):
    """Bypassing it would bypass duplicate detection, ticker resolution and
    validation -- the properties that make a re-run safe."""
    assert "scripts/edgelab/import_bet_batch.py" in deliver_text
    assert "--receipts-out" in deliver_text
    # Never a hand-written ledger edit.
    assert "bets.jsonl" not in deliver_text


def test_delivery_defaults_to_a_dry_run(deliver):
    """Running it with no arguments must not push anything.

    (The first draft of this test asserted `x is False or True`, which is a
    tautology and asserted nothing at all.)
    """
    events = triggers(deliver)
    default = events["workflow_dispatch"]["inputs"]["dry_run"]["default"]
    assert default in (True, "true"), f"dry_run defaults to {default!r}"


def test_delivery_uses_a_deterministic_branch_name(deliver_text):
    """A retry must find the existing branch rather than open a second pull
    request for wagers that were already delivered."""
    assert "kalshi-router/${sport}-$(git -C \"${work}\" rev-parse --short HEAD)" in deliver_text
    for nondeterministic in ("$RANDOM", "date +%s", "${GITHUB_RUN_ID}"):
        assert nondeterministic not in deliver_text


def test_delivery_keeps_one_sports_failure_from_rolling_back_another(deliver_text):
    # Per-destination loop with a failure counter, not an all-or-nothing abort.
    assert "failures=$((failures + 1))" in deliver_text
    assert "continue" in deliver_text


def test_the_audit_workflow_still_never_receives_the_downstream_credential():
    assert DOWNSTREAM_SECRET not in AUDIT_WORKFLOW.read_text()


# ============ Phase 6: the historical shadow comparison ======================
#
# It holds the KALSHI credential and reads the owner's complete betting
# history, but it has no reason to write anywhere -- so these tests pin that it
# cannot, and that nothing it prints can name a market.

COMPARE_WORKFLOW = ROOT / ".github/workflows/historical-shadow-compare.yml"


@pytest.fixture(scope="module")
def compare() -> dict:
    return load(COMPARE_WORKFLOW)


@pytest.fixture(scope="module")
def compare_text() -> str:
    return strip_comments(COMPARE_WORKFLOW.read_text())


def test_the_comparison_never_receives_the_downstream_credential():
    """It validates. It does not deliver. So it gets no write credential."""
    assert DOWNSTREAM_SECRET not in COMPARE_WORKFLOW.read_text()


def test_the_comparison_is_dispatch_only(compare):
    assert set(triggers(compare)) == {"workflow_dispatch"}


def test_the_comparison_holds_read_only_permissions(compare):
    assert compare["permissions"] == {"contents": "read"}


def test_the_comparison_clones_the_ledger_without_a_credential(compare_text):
    """A public clone. A token in this step would be a token that can push."""
    assert "https://github.com/chmoses98/edge-finder-api" in compare_text
    for pattern in ("x-access-token", "credential.helper", "extraheader", "@github.com"):
        assert pattern not in compare_text


def test_the_comparison_never_pushes_or_commits(compare_text):
    for verb in ("git push", "git commit", "git add", "create_pull_request", "gh pr"):
        assert verb not in compare_text


def test_the_comparison_never_requests_sensitive_details(compare_text):
    assert "--show-sensitive-details" not in compare_text


def test_the_comparison_uploads_no_artifact(compare_text):
    """The ledger clone and the replay both hold the owner's betting history."""
    assert "upload-artifact" not in compare_text


def test_the_comparison_walks_the_full_history(compare_text):
    """A truncated walk would report the truncation as disagreement."""
    assert "--full-history" in compare_text
    assert "--shadow-wagers" in compare_text
    assert "--compare-ledger" in compare_text


# ---- and the enumeration, so a future workflow is covered by default --------


def kalshi_credentialed_workflows() -> list[Path]:
    return [p for p in workflow_files() if "KALSHI_PRIVATE_KEY" in p.read_text()]


def test_there_is_at_least_one_kalshi_credentialed_workflow():
    """Guards the enumeration below against silently testing nothing."""
    assert kalshi_credentialed_workflows()


@pytest.mark.parametrize(
    "path", kalshi_credentialed_workflows(), ids=lambda p: p.name
)
def test_no_kalshi_workflow_ever_requests_sensitive_details(path):
    """--show-sensitive-details prints individual markets. Never in Actions.

    The CLI refuses it in CI anyway; this is the second lock, on the side that
    a code change cannot quietly move.
    """
    assert "--show-sensitive-details" not in strip_comments(path.read_text())


@pytest.mark.parametrize(
    "path", kalshi_credentialed_workflows(), ids=lambda p: p.name
)
def test_no_kalshi_workflow_uploads_an_artifact(path):
    """Every one of these holds the owner's betting activity in memory."""
    assert "upload-artifact" not in path.read_text()


@pytest.mark.parametrize(
    "path", kalshi_credentialed_workflows(), ids=lambda p: p.name
)
def test_every_kalshi_workflow_refuses_to_run_off_main(path):
    """A credentialed workflow must not be runnable from an arbitrary ref."""
    assert "refs/heads/main" in path.read_text()


# ============ Phase 10: the schedule, and Phase 12: recovery =================

RECOVER_WORKFLOW = ROOT / ".github/workflows/recover-wagers.yml"


@pytest.fixture(scope="module")
def recover() -> dict:
    return load(RECOVER_WORKFLOW)


@pytest.fixture(scope="module")
def recover_text() -> str:
    return strip_comments(RECOVER_WORKFLOW.read_text())


# ---- the cadence -----------------------------------------------------------


def test_the_delivery_runs_on_a_schedule_within_the_five_to_fifteen_minute_band(deliver):
    crons = [entry["cron"] for entry in triggers(deliver)["schedule"]]
    assert crons == ["*/15 * * * *"], crons


def test_the_cadence_leaves_headroom_over_the_measured_run(deliver):
    """Run 2 built the payload in 5m19s over 1756 orders.

    A cadence at or under that would start each run before the previous one
    finished, and with cancel-in-progress: false they would queue without
    bound. This asserts the relationship, so a future cadence change has to
    confront the measurement rather than step over it.
    """
    MEASURED_PAYLOAD_BUILD_SECONDS = 319
    cron = triggers(deliver)["schedule"][0]["cron"]
    minutes = int(cron.split()[0].removeprefix("*/"))
    assert minutes * 60 >= 2 * MEASURED_PAYLOAD_BUILD_SECONDS


def test_the_delivery_is_still_dispatchable_by_hand(deliver):
    assert "workflow_dispatch" in triggers(deliver)


def test_a_delivery_in_flight_is_never_cancelled(deliver):
    """Cancellation between the destination's commit and this job's receipt
    would leave a canonical wager written with no record here that it was."""
    assert deliver["concurrency"]["cancel-in-progress"] is False


def test_recovery_and_delivery_share_a_concurrency_group(deliver, recover):
    """Two runs building payloads from one account at once is not a thing that
    should be possible."""
    assert recover["concurrency"]["group"] == deliver["concurrency"]["group"]
    assert recover["concurrency"]["cancel-in-progress"] is False


# ---- an empty dry-run choice must never mean "push" ------------------------


def test_a_scheduled_run_states_its_dry_run_value_explicitly(deliver_text):
    """`inputs.dry_run` is EMPTY on a schedule. Relying on `!= "true"` to mean
    "deliver" would make the live/dry choice depend on an absent value."""
    assert "github.event_name == 'schedule'" in deliver_text


@pytest.mark.parametrize("text_fixture", ["deliver_text", "recover_text"])
def test_an_unreadable_dry_run_value_is_refused(text_fixture, request):
    text = request.getfixturevalue(text_fixture)
    assert "DRY_RUN must be exactly" in text
    assert "true|false)" in text


# ---- recovery -------------------------------------------------------------


def test_recovery_is_never_scheduled(recover):
    """History must not be importable by a clock."""
    assert set(triggers(recover)) == {"workflow_dispatch"}


def test_recovery_defaults_to_a_dry_run(recover):
    default = triggers(recover)["workflow_dispatch"]["inputs"]["dry_run"]["default"]
    assert default in (True, "true"), f"dry_run defaults to {default!r}"


def test_pre_cutover_defaults_to_off_and_is_a_phrase_not_a_checkbox(recover):
    """A checkbox is one stray click. A phrase has to be typed."""
    field = triggers(recover)["workflow_dispatch"]["inputs"]["include_pre_cutover"]
    assert field["default"] == ""
    assert field["type"] == "string"


def test_the_scheduled_delivery_cannot_import_pre_cutover_history(deliver_text):
    """Not merely defaulted off there -- ABSENT. An input that does not exist
    cannot be set by a mistyped dispatch."""
    assert "include_pre_cutover" not in deliver_text
    assert "include-pre-cutover" not in deliver_text


def test_recovery_refuses_a_ref_other_than_main(recover_text):
    assert "refs/heads/main" in recover_text


def test_recovery_holds_read_only_repository_permissions(recover):
    assert recover["permissions"] == {"contents": "read"}


def test_recovery_never_puts_the_credential_in_a_url_or_argv(recover_text):
    for pattern in ("://${DOWNSTREAM_REPO_TOKEN}", "x-access-token:${DOWNSTREAM_REPO_TOKEN}",
                    "@github.com", "--password", "extraheader"):
        assert pattern not in recover_text
    assert "credential.helper" in recover_text


def test_recovery_sends_the_importers_receipts_to_dev_null(recover_text):
    """The receipts JSON carries marketTicker, stake and entryPrice on STDOUT.

    Measured against the real importer: stdout is the receipts, stderr is the
    one-line count. Only the count may reach a public log.
    """
    assert "--receipts-out" in recover_text
    assert ">/dev/null" in recover_text


def test_recovery_uploads_no_artifact(recover_text):
    assert "upload-artifact" not in recover_text
