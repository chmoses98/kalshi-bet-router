"""Proof that the public workflow cannot disclose individual wagers.

These tests read the committed workflow YAML, so a future edit that adds an
artifact upload, a sensitive flag, a write permission, or a fork-PR trigger
fails CI rather than leaking the owner's betting activity.
"""

from __future__ import annotations

import re
import shlex
import subprocess
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
        # The one-time historical catch-up. Added deliberately for the same
        # reason, and it failed here first too.
        "backfill-deliver.yml",
        # The settlement half of that catch-up. Also added only after this test
        # went red for it.
        "backfill-settle.yml",
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


def test_the_probe_creates_nothing_anywhere(probe_text):
    """It proves a write credential without writing.

    This began as a blanket ban on every mutating verb, which was the stronger
    and simpler guarantee. It has been NARROWED, deliberately, and the reason
    is worth stating because the narrowing is the risky part:

    delivery ENDS in a pull request, and `.permissions` on the repository
    object does not report Pull requests:write. A token with Contents:write and
    without it would push the branch and then fail on the FIRST REAL WAGER. The
    only way to ask GitHub that question is to attempt the call, so exactly one
    POST is now permitted -- and only the one that CANNOT create anything,
    because its head branch does not exist.

    Everything else stays banned outright, and the exception is asserted rather
    than assumed: a POST to any other path, or to /pulls without the
    impossible head, fails here.
    """
    lowered = probe_text.lower()

    # Still absolutely forbidden: nothing that could modify or delete.
    for mutation in (
        '-x put', '-x patch', '-x delete',
        '--request put', '--request patch', '--request delete',
        "git push", "git commit", "/git/refs", "/contents/",
    ):
        assert mutation not in lowered, f"the probe must never {mutation}"

    # POST is permitted only as the pull-request permission probe.
    joined = probe_text.replace("\\\n", " ")
    posts = [
        line for line in joined.splitlines()
        if "-X POST" in line or "--request POST" in line
    ]
    for line in posts:
        assert "/pulls" in line, f"POST to something other than /pulls: {line.strip()}"

    assert len(posts) <= 1, "exactly one POST is permitted"
    if posts:
        # The head must be the branch that cannot exist, so nothing can be
        # created even if authorization succeeds.
        assert "__probe_branch_that_does_not_exist__" in joined, (
            "the pull-request probe must use a head branch that cannot exist"
        )


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


def _delivering_workflow_texts():
    return [
        (p.name, strip_comments(p.read_text()))
        for p in workflow_files()
        if "kalshi_router.cli deliver" in p.read_text()
    ]


@pytest.mark.parametrize("name,text", _delivering_workflow_texts(), ids=lambda v: v if isinstance(v, str) and len(v) < 40 else "")
def test_the_branch_name_is_stable_over_TIME_not_just_within_one_run(name, text):
    """The first version derived it from the DESTINATION's HEAD, which moves.

    Same undelivered wager, two destination commits, two branches -- and
    because the branch is never merged, every run re-imports as NEW. If the
    destination had NOT moved it was worse: the same name with a different
    commit, rejected non-fast-forward, red every 15 minutes.

    The old test forbade $RANDOM, date and GITHUB_RUN_ID. None of those
    appeared, so it passed while the name was still unstable -- it was testing
    the wrong property. This one requires the name to depend on NOTHING that
    can change between runs.
    """
    branch_lines = [line for line in text.splitlines() if line.strip().startswith("branch=")]
    assert branch_lines, f"{name} sets no branch"
    for line in branch_lines:
        for unstable in ("$RANDOM", "date +%s", "GITHUB_RUN_ID", "rev-parse", "$(git"):
            assert unstable not in line, f"{name}: branch name depends on {unstable}: {line.strip()}"


@pytest.mark.parametrize("name,text", _delivering_workflow_texts(), ids=lambda v: v if isinstance(v, str) and len(v) < 40 else "")
def test_a_delivery_that_cannot_open_a_pull_request_is_a_FAILURE(name, text):
    """A BRANCH IS NOT THE LEDGER.

    The first version pushed a branch and stopped, so a delivered wager sat
    somewhere nobody looks and was never recorded -- which is the one thing
    this system exists to do. Opening the pull request is part of delivery.
    """
    assert "/pulls" in text, f"{name} never opens a pull request"
    assert "are NOT recorded" in text, f"{name} does not fail loudly when it cannot"
    # 422 means "already open for this head", which is the NORMAL case on every
    # run after the first, because the branch is long-lived.
    assert "422)" in text, f"{name} would treat an already-open PR as an error"


@pytest.mark.parametrize("name,text", _delivering_workflow_texts(), ids=lambda v: v if isinstance(v, str) and len(v) < 40 else "")
def test_the_destination_default_branch_is_never_pushed_to(name, text):
    """The router opens a pull request. It does not write the ledger itself."""
    # Join shell line-continuations first: the refspec of a wrapped `git push`
    # is on the NEXT line, and checking line by line would read the command as
    # having no destination at all.
    joined = text.replace("\\\n", " ")

    pushes = [line for line in joined.splitlines() if "git" in line and " push" in line]
    assert pushes, f"{name} never pushes"
    for line in pushes:
        # The target is the branch VARIABLE, so the two halves are checked
        # separately: every push goes to ${branch}, and ${branch} is only ever
        # set to a router-owned name (below).
        assert 'HEAD:refs/heads/${branch}' in line, f"{name}: {line.strip()}"
        assert ":refs/heads/main" not in line
        assert "--force " not in line, "force-with-lease, never bare force"

    assignments = [line.strip() for line in joined.splitlines() if line.strip().startswith("branch=")]
    assert assignments, f"{name} sets no branch"
    for line in assignments:
        assert line.startswith('branch="kalshi-router/'), (
            f"{name} could push outside the router's own namespace: {line}"
        )


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


# ============ Phase 13: the health signal reaches the log ====================


def _health_workflows():
    return [
        p for p in workflow_files()
        if "kalshi_router.cli deliver" in p.read_text()
    ]


def test_there_is_at_least_one_delivering_workflow():
    assert _health_workflows()


@pytest.mark.parametrize("path", _health_workflows(), ids=lambda p: p.name)
def test_every_delivering_workflow_annotates_its_health(path):
    text = strip_comments(path.read_text())
    assert "HEALTH=" in text
    for state in ("healthy_no_op", "delivered", "deferred", "not_routable", "blocked"):
        assert state in text, f"{path.name} does not handle {state}"


@pytest.mark.parametrize("path", _health_workflows(), ids=lambda p: p.name)
def test_a_missing_health_state_is_itself_a_failure(path):
    """A run that reports no health is not a healthy run."""
    text = strip_comments(path.read_text())
    assert "reported no health state" in text


@pytest.mark.parametrize("path", _health_workflows(), ids=lambda p: p.name)
def test_a_blocked_run_warns_rather_than_failing(path):
    """A REFUSAL IS NOT A FAILURE.

    BLOCKED means the system correctly refused a wager it cannot record.
    Failing a scheduled job every 15 minutes for something only the owner can
    fix would train them to ignore it.
    """
    text = strip_comments(path.read_text())
    blocked_line = next(
        line for line in text.splitlines()
        if "BLOCKED --" in line
    )
    assert "::warning::" in blocked_line
    assert "::error::" not in blocked_line


@pytest.mark.parametrize("path", _health_workflows(), ids=lambda p: p.name)
def test_the_health_token_never_carries_a_count_or_a_market(path):
    """It is one word from a closed set, read out of the report by grep."""
    text = strip_comments(path.read_text())
    assert "grep -E '^HEALTH=' " in text


@pytest.mark.parametrize("path", _health_workflows(), ids=lambda p: p.name)
def test_only_the_health_annotation_may_claim_a_health_verdict(path):
    """Run 3 printed "HEALTHY NO-OP" and "HEALTH: blocked" in the same log.

    Both came from the same job. The delivery step hardcoded the first whenever
    no payload file existed, but "no payload" can mean a quiet account OR a
    wager this system refused to record, and that step cannot tell them apart.
    The health annotation can. So the verdict words belong to it alone.
    """
    text = strip_comments(path.read_text())

    verdicts = ("HEALTHY NO-OP", "BLOCKED", "DEFERRED", "NOT ROUTABLE", "DELIVERED")
    for line in text.splitlines():
        if any(verdict in line for verdict in verdicts):
            assert "::notice::" in line or "::warning::" in line, (
                f"{path.name} states a health verdict outside the annotation: {line.strip()}"
            )


@pytest.mark.parametrize("name,text", _delivering_workflow_texts(), ids=lambda v: v if isinstance(v, str) and len(v) < 40 else "")
def test_a_router_commit_may_only_touch_the_data_directory(name, text):
    """`git add -A` writes into SOMEONE ELSE'S repository every 15 minutes.

    It is safe today only because the destination's own .gitignore covers what
    the importer leaves behind -- measured on a real clone, three __pycache__
    directories and a bets.jsonl.lock, all ignored. That is a property of THEIR
    repository, which can change without telling this workflow.

    So the staged set is checked before committing, and anything outside data/
    stops the delivery rather than riding along.
    """
    assert "diff --cached --name-only" in text, f"{name} commits without checking what"
    assert "grep -v '^data/'" in text
    assert "dirtied files outside data/" in text


@pytest.mark.parametrize("name,text", _delivering_workflow_texts(), ids=lambda v: v if isinstance(v, str) and len(v) < 40 else "")
def test_an_unexpected_staged_file_is_a_failure_not_a_warning(name, text):
    """Committing a lock file into the destination every 15 minutes is worse
    than a red job."""
    joined = text.replace("\\\n", " ")
    lines = joined.splitlines()
    for index, line in enumerate(lines):
        if "dirtied files outside data/" in line:
            window = "\n".join(lines[index : index + 6])
            assert "failures=$((failures + 1))" in window, f"{name}: does not count as a failure"
            assert "::error::" in line
            break
    else:  # pragma: no cover - the assertion above already covers absence
        raise AssertionError(f"{name} has no guard")


# ---- the probe must stay a PROBE ------------------------------------------


def test_the_probe_checks_pull_request_permission_not_just_contents(probe_text):
    """Contents:write is not enough. Delivery ENDS in a pull request, and a
    token without Pull requests:write would push the branch and then fail on
    the first real wager -- the worst moment to find out."""
    assert "/pulls" in probe_text
    assert "pr_ok" in probe_text


def test_the_pull_request_probe_cannot_create_anything(probe_text):
    """It asks to open a pull request from a head branch that does not exist,
    so GitHub has nothing to create from. The only question is which error
    comes back."""
    assert "__probe_branch_that_does_not_exist__" in probe_text


def test_a_created_pull_request_is_treated_as_an_error(probe_text):
    """Impossible from a non-existent head -- and if it ever happens, the probe
    wrote to someone's repository and that must not pass silently."""
    joined = probe_text.replace("\\\n", " ")
    lines = joined.splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith("201)"):
            window = "\n".join(lines[index : index + 5])
            assert "::error::" in window
            assert 'pr_ok="false"' in window
            return
    raise AssertionError("the probe does not handle a 201 at all")


def test_an_unrecognised_probe_status_is_unknown_rather_than_guessed(probe_text):
    """Reporting an uninterpreted status as either pass or fail would be an
    opinion dressed as a measurement."""
    assert 'pr_ok="unknown"' in probe_text
    assert "is not interpreted" in probe_text


def test_a_destination_is_usable_only_if_every_check_passed(probe_text):
    """pr_ok must gate the verdict, not merely be printed beside it."""
    joined = probe_text.replace("\\\n", " ")
    verdict = [line for line in joined.splitlines() if "NOT USABLE" in line]
    assert verdict
    condition = [
        line for line in joined.splitlines()
        if 'if [ "${status}" != "200" ]' in line
    ]
    assert condition, "the verdict condition moved"
    assert '${pr_ok}' in condition[0], "pr_ok is printed but does not gate the verdict"


@pytest.mark.parametrize("name,text", _delivering_workflow_texts(), ids=lambda v: v if isinstance(v, str) and len(v) < 40 else "")
def test_the_lease_carries_an_explicit_expected_value(name, text):
    """A bare --force-with-lease fails from the SECOND run onward.

    Measured against a real shallow clone: `git clone --depth 1` is
    SINGLE-BRANCH, its refspec is +refs/heads/main:refs/remotes/origin/main and
    nothing else, so there is no remote-tracking ref for the router's own
    branch. A bare lease has nothing to form an expectation against and git
    rejects the push as "stale info".

    The first run succeeds (the branch does not exist yet), which is precisely
    why this would have looked fine exactly once.
    """
    joined = text.replace("\\\n", " ")
    pushes = [line for line in joined.splitlines() if "git" in line and " push" in line]
    assert pushes
    for line in pushes:
        assert "--force-with-lease=" in line, (
            f"{name}: a bare --force-with-lease is rejected as stale info from a "
            f"shallow clone: {line.strip()}"
        )
        assert '${expected}' in line


@pytest.mark.parametrize("name,text", _delivering_workflow_texts(), ids=lambda v: v if isinstance(v, str) and len(v) < 40 else "")
def test_the_expected_value_comes_from_an_explicitly_fetched_ref(name, text):
    """Fetching is NECESSARY and not sufficient -- but without it, rev-parse
    finds nothing and the lease would always read "must not exist", which would
    silently turn the lease off rather than fail."""
    joined = text.replace("\\\n", " ")
    fetches = [line for line in joined.splitlines() if "fetch" in line and "refs/remotes/origin" in line]
    assert fetches, f"{name} never fetches the branch it leases against"
    assert any("expected=" in line for line in joined.splitlines())


@pytest.mark.parametrize("name,text", _delivering_workflow_texts(), ids=lambda v: v if isinstance(v, str) and len(v) < 40 else "")
def test_a_missing_branch_yields_an_empty_expectation_not_a_failure(name, text):
    """On the first run the branch does not exist. rev-parse must fall back to
    an empty string, which the lease reads as "must not exist yet"."""
    joined = text.replace("\\\n", " ")
    assert any(
        "rev-parse" in line and 'echo ""' in line
        for line in joined.splitlines()
    ), f"{name}: the first run would fail on a missing ref"


# ============ the receipts block is code, and it was untested ================
#
# Each delivering workflow embeds a Python heredoc that summarises the
# destination's import receipts. It is real code on the delivery path, it had no
# test, and it can only run when a payload exists -- so it would first have
# executed on the first real wager, printing into a public log.
#
# These tests extract it from the YAML exactly as bash would see it and run it.


def _receipts_block(path):
    """The heredoc body, as the shell hands it to python.

    YAML strips the block scalar's own indentation, which is why the body
    reaches python at column zero even though it is indented in the file. That
    is load-bearing -- an indented top-level statement is a SyntaxError -- so
    it is extracted rather than retyped.
    """
    import re

    config = load(path)
    for job in (config.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            script = step.get("run")
            if not isinstance(script, str) or "PYEOF" not in script:
                continue
            match = re.search(r"<<'PYEOF'\n(.*?)\n\s*PYEOF", script, re.S)
            if match:
                return match.group(1)
    return None


@pytest.mark.parametrize("path", _health_workflows(), ids=lambda p: p.name)
def test_the_receipts_block_is_valid_python_at_column_zero(path):
    body = _receipts_block(path)
    assert body is not None, f"{path.name} has no receipts block"
    assert not body.splitlines()[0].startswith(" "), (
        "the block reaches python indented, which is a SyntaxError"
    )
    compile(body, "receipts", "exec")


@pytest.mark.parametrize("path", _health_workflows(), ids=lambda p: p.name)
def test_the_receipts_block_prints_counts_and_verdicts_only(path, tmp_path, capfd):
    """Receipts carry marketTicker, stake, entryPrice and the source key. The
    summary must carry none of them into a public Actions log."""
    import json as _j
    import subprocess
    import sys as _sys

    receipts = tmp_path / "r.json"
    receipts.write_text(_j.dumps([
        {
            "duplicateStatus": "NEW", "betId": "abc123", "success": True,
            "sourceBetKey": "kalshi:v1:deadbeef", "stake": 5.37, "entryPrice": 0.53,
            "market": {"marketTicker": "KXMLBGAME-26SEP14NYYBOS-NYY", "side": "YES"},
        },
        {
            "duplicateStatus": "CONFLICT", "betId": "def456", "success": False,
            "sourceBetKey": "kalshi:v1:feedface", "stake": 1.23, "entryPrice": 0.11,
            "market": {"marketTicker": "KXMLBGAME-26SEP14NYYBOS-BOS", "side": "NO"},
        },
    ]))
    script = tmp_path / "block.py"
    script.write_text(_receipts_block(path) + "\n")

    result = subprocess.run(
        [_sys.executable, str(script), str(receipts)],
        capture_output=True, text=True, check=True,
    )

    assert "rows: 2" in result.stdout
    assert "NEW: 1" in result.stdout
    assert "CONFLICT: 1" in result.stdout
    assert "failed rows: 1" in result.stdout

    for secret in (
        "KXMLB", "5.37", "0.53", "1.23", "0.11",
        "kalshi:v1", "deadbeef", "feedface", "YES", "NO",
    ):
        assert secret not in result.stdout, f"the receipts summary leaked {secret!r}"


@pytest.mark.parametrize("path", _health_workflows(), ids=lambda p: p.name)
def test_the_receipts_block_survives_an_empty_receipts_file(path, tmp_path):
    """An import that wrote nothing must not crash the summary and turn a
    clean run red."""
    import subprocess
    import sys as _sys

    receipts = tmp_path / "r.json"
    receipts.write_text("[]")
    script = tmp_path / "block.py"
    script.write_text(_receipts_block(path) + "\n")

    result = subprocess.run(
        [_sys.executable, str(script), str(receipts)],
        capture_output=True, text=True, check=True,
    )

    assert "rows: 0" in result.stdout


# ============ the credential helper, executed rather than described ==========
#
# The whole credential posture rests on one claim: the token reaches git
# through a helper that reads it from the ENVIRONMENT at push time, so it is
# never in a URL, never in argv, and never written into .git/config.
#
# Every other test here checks that by absence -- no "://$TOKEN", no
# "--password". Absence is necessary and proves nothing about whether the
# mechanism WORKS. A helper with a shell typo satisfies every one of those
# tests and fails to authenticate on the first real wager.
#
# So these run it.


def _credential_helper(path):
    """The helper string exactly as the workflow configures it."""
    import re

    for job in (load(path).get("jobs") or {}).values():
        for step in job.get("steps") or []:
            script = step.get("run")
            if not isinstance(script, str) or "credential.helper" not in script:
                continue
            match = re.search(r"credential\.helper\s*\\?\s*\n?\s*'(!.*?)'", script, re.S)
            if match:
                return match.group(1)
    return None


@pytest.mark.parametrize("path", _health_workflows(), ids=lambda p: p.name)
def test_the_credential_helper_actually_produces_a_credential(path, monkeypatch):
    """Run it the way git does: a '!'-prefixed helper is handed to the shell
    with the operation appended."""
    import subprocess

    helper = _credential_helper(path)
    assert helper is not None, f"{path.name} configures no credential helper"

    result = subprocess.run(
        ["sh", "-c", f"{helper[1:]} get"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        env={"DOWNSTREAM_REPO_TOKEN": "TOKEN-UNDER-TEST", "PATH": "/usr/bin:/bin"},
    )

    assert result.returncode == 0, result.stderr
    assert "username=x-access-token" in result.stdout
    assert "password=TOKEN-UNDER-TEST" in result.stdout


@pytest.mark.parametrize("path", _health_workflows(), ids=lambda p: p.name)
def test_the_helper_reads_the_token_at_invocation_not_at_configuration(path, tmp_path):
    """Configure it into a REAL repository and check what lands on disk.

    This is the claim that matters: .git/config must hold the variable NAME,
    never its value. A helper written with double quotes in the workflow would
    expand at configuration time and write the secret into a file.
    """
    import subprocess

    helper = _credential_helper(path)
    repo = tmp_path / "r"
    repo.mkdir()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "DOWNSTREAM_REPO_TOKEN": "TOKEN-UNDER-TEST"}

    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "config", "credential.helper", helper],
        check=True, env=env,
    )

    on_disk = (repo / ".git" / "config").read_text()
    assert "TOKEN-UNDER-TEST" not in on_disk, "the token VALUE was written to .git/config"
    assert "DOWNSTREAM_REPO_TOKEN" in on_disk, "the variable name should be what is stored"

    # ...and git still resolves a real credential from it.
    filled = subprocess.run(
        ["git", "-C", str(repo), "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, env=env,
    )
    assert "password=TOKEN-UNDER-TEST" in filled.stdout, filled.stderr


@pytest.mark.parametrize("name,text", _delivering_workflow_texts(), ids=lambda v: v if isinstance(v, str) and len(v) < 40 else "")
def test_the_dry_run_prints_a_diffstat_never_the_diff(name, text):
    """One word is the difference between a count and a publication.

    The dry-run branch shows what the import changed. With --stat that is:

        data/edgelab/bets/bets.jsonl | 1 +
        1 file changed, 1 insertion(+)

    Without it, the same command prints the added ledger lines -- ticker,
    stake, entry price, source key -- into a PUBLIC Actions log. Measured on a
    scratch repository, not assumed.

    The workflow is correct today. Nothing was stopping it from stopping being
    correct.
    """
    import re

    joined = text.replace("\\\n", " ")
    # Match a git DIFF COMMAND, not any line containing both words -- the pull
    # request body says "the rows are in the diff" and the API host contains
    # "git", and the first draft of this test flagged both.
    invocation = re.compile(r"(?:^|\$\()\s*git\b[^|;]*?\sdiff\b")
    diffs = [
        line for line in joined.splitlines()
        if invocation.search(line) and "--cached --name-only" not in line
    ]
    assert diffs, f"{name} shows the dry run nothing at all"
    for line in diffs:
        assert "--stat" in line, (
            f"{name}: this would print the wager rows themselves: {line.strip()}"
        )


@pytest.mark.parametrize("name,text", _delivering_workflow_texts(), ids=lambda v: v if isinstance(v, str) and len(v) < 40 else "")
def test_no_command_in_the_delivery_step_cats_the_ledger(name, text):
    """The payload and the ledger both carry the owner's betting activity.

    A `cat`, a `head`, or a `jq .` over either would put it in the log as
    surely as a bad diff. None of them has any business here.
    """
    joined = text.replace("\\\n", " ")
    for forbidden in ("cat ${payload}", "cat \"${payload}\"", "jq . ", "head ", "tail "):
        assert forbidden not in joined, f"{name} would print a payload or ledger: {forbidden}"


# ============ the backfill inspection writes nothing =========================

BACKFILL_WORKFLOW = ROOT / ".github/workflows/backfill-inspect.yml"


@pytest.fixture(scope="module")
def backfill_inspect() -> dict:
    return load(BACKFILL_WORKFLOW)


@pytest.fixture(scope="module")
def backfill_inspect_text() -> str:
    return strip_comments(BACKFILL_WORKFLOW.read_text())


def test_the_inspection_holds_no_downstream_credential():
    """It answers a question. It must not be ABLE to act on the answer."""
    assert DOWNSTREAM_SECRET not in BACKFILL_WORKFLOW.read_text()


def test_the_inspection_cannot_be_pointed_past_the_cutover(backfill_inspect):
    """There is a `since` input and deliberately no `until`.

    A settable end is the one way this could quietly become a second
    production path, reaching forward instead of back.
    """
    inputs = triggers(backfill_inspect)["workflow_dispatch"]["inputs"]
    assert "since" in inputs
    for forbidden in ("until", "end", "through", "to"):
        assert forbidden not in inputs


def test_the_inspection_never_pushes_or_commits(backfill_inspect_text):
    for verb in ("git push", "git commit", "git add", "/pulls", "import_bet_batch"):
        assert verb not in backfill_inspect_text


def test_the_inspection_clones_the_ledger_without_a_credential(backfill_inspect_text):
    assert "https://github.com/chmoses98/edge-finder-api" in backfill_inspect_text
    for pattern in ("x-access-token", "credential.helper", "extraheader"):
        assert pattern not in backfill_inspect_text


# ------------------------------------------------- the push lease, executed
#
# The scheduled run of 2026-09-15T16:43Z reconstructed the wager, classified it
# MLB, and the destination importer returned NEW with a canonical bet id -- and
# then the delivery step exited 129 without pushing anything:
#
#   fatal: couldn't find remote ref refs/heads/kalshi-router/MLB
#   error: cannot parse expected object name 'refs/remotes/origin/kalshi-router/MLB'
#
# The workflow computes the lease's expected value with a bare `git rev-parse`,
# whose comment states that a missing branch yields an EMPTY value. It does not.
# Without `--verify`, rev-parse ECHOES ITS ARGUMENT TO STDOUT before failing, so
# `expected` became the literal ref name and the lease could not be parsed. The
# `|| echo ""` never had a chance to run usefully.
#
# The first-run path -- branch absent -- was the one case never exercised, and
# it is the only case that ever runs on a brand-new destination branch.
#
# These tests EXECUTE the committed shell rather than asserting on its text, so
# they test the production line itself and not a copy of it that can drift.

LEASE_EXPECTED_LINE = re.compile(r'^\s*(expected="\$\(git .*rev-parse.*\)")\s*$', re.M)

RECOVER_WORKFLOW = ROOT / ".github/workflows/recover-wagers.yml"

#: Every workflow that pushes to a destination under a lease. `recover-wagers`
#: carried the identical broken line, and it is the path a controlled recovery
#: would use -- so fixing only the workflow that happened to fail would have
#: left the repair itself standing on the defect it was repairing.
PUSHING_WORKFLOWS = (DELIVER_WORKFLOW, RECOVER_WORKFLOW)


def expected_assignment(workflow: Path) -> str:
    """A workflow's own `expected=...` line, lifted verbatim."""
    match = LEASE_EXPECTED_LINE.search(workflow.read_text())
    assert match, f"{workflow.name} no longer computes a lease expectation"
    return match.group(1)


def deliver_expected_assignment() -> str:
    return expected_assignment(DELIVER_WORKFLOW)


def shallow_destination_clone(tmp_path):
    """A bare remote plus a genuinely SHALLOW, SINGLE-BRANCH clone of it.

    Both properties are asserted rather than assumed. An earlier attempt at this
    fixture produced an EMPTY clone -- the bare repo's HEAD pointed at a branch
    that did not exist -- and an empty clone would have made the rejection come
    from somewhere else entirely, turning a real bug into a false finding.
    """
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    # Without this the clone is empty and proves nothing.
    subprocess.run(
        ["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
    )

    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", str(seed)], check=True)
    for key, value in (("user.email", "t@example.invalid"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(seed), "config", key, value], check=True)
    (seed / "data").mkdir()
    (seed / "data" / "bets.jsonl").write_text('{"seed": true}\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-qm", "seed"], check=True)
    subprocess.run(["git", "-C", str(seed), "branch", "-M", "main"], check=True)
    subprocess.run(
        ["git", "-C", str(seed), "push", "-q", f"file://{remote}", "main"],
        check=True, capture_output=True,
    )

    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{remote}", str(work)], check=True
    )
    assert (work / ".git" / "shallow").exists(), "fixture is not a shallow clone"
    refspec = subprocess.run(
        ["git", "-C", str(work), "config", "--get", "remote.origin.fetch"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert refspec == "+refs/heads/main:refs/remotes/origin/main", (
        f"fixture is not single-branch; refspec is {refspec!r}"
    )
    for key, value in (("user.email", "t@example.invalid"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(work), "config", key, value], check=True)
    return remote, work


@pytest.mark.parametrize(
    "workflow", PUSHING_WORKFLOWS, ids=lambda path: path.name
)
def test_the_lease_expectation_is_empty_when_the_branch_does_not_exist(workflow, tmp_path):
    """The first run, which is the run that failed in production.

    Runs each workflow's own assignment against a real shallow clone whose
    destination branch does not exist. An empty result is what the surrounding
    code documents and requires; the ref name is what a bare rev-parse returns.
    """
    _remote, work = shallow_destination_clone(tmp_path)
    branch = "kalshi-router/MLB"

    subprocess.run(
        ["git", "-C", str(work), "fetch", "-q", "--depth", "1", "origin",
         f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
        capture_output=True,
    )
    script = f'work={shlex.quote(str(work))}; branch={shlex.quote(branch)}\n' \
             f'{expected_assignment(workflow)}\nprintf "%s" "${{expected}}"'
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    )

    assert result.stdout == "", (
        f"{workflow.name}: the lease expectation must be EMPTY for a branch that "
        f"does not exist; got {result.stdout!r} -- git push cannot parse that as "
        "an object name"
    )


def test_the_first_delivery_to_a_new_branch_actually_pushes(tmp_path):
    """End to end: the exact failure, at the exact step, with a real push.

    The assertion above pins the value; this pins the CONSEQUENCE. Production
    did not fail on a variable, it failed on `git push` exiting 129 with the
    wager already imported and nothing sent.
    """
    remote, work = shallow_destination_clone(tmp_path)
    branch = "kalshi-router/MLB"

    with (work / "data" / "bets.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"imported": true}\n')
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "-b", branch], check=True)
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "import"], check=True)
    subprocess.run(
        ["git", "-C", str(work), "fetch", "-q", "--depth", "1", "origin",
         f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
        capture_output=True,
    )

    script = (
        f'set -euo pipefail\n'
        f'work={shlex.quote(str(work))}; branch={shlex.quote(branch)}\n'
        f'{deliver_expected_assignment()}\n'
        f'git -C "${{work}}" push -q '
        f'--force-with-lease="refs/heads/${{branch}}:${{expected}}" '
        f'origin "HEAD:refs/heads/${{branch}}"'
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, (
        f"the first delivery to a new branch failed (exit {result.returncode}): "
        f"{result.stderr.strip()}"
    )
    landed = subprocess.run(
        ["git", "--git-dir", str(remote), "for-each-ref", "--format=%(refname)"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert f"refs/heads/{branch}" in landed, "the branch never reached the remote"


def test_the_lease_still_refuses_when_someone_else_moved_the_branch(tmp_path):
    """The property the lease exists for, which the fix must not cost.

    A fix that simply forced the push would make the first run pass and quietly
    destroy a concurrent writer's commit. So a second clone pushes in between,
    and the delivery push must be REJECTED.
    """
    remote, work = shallow_destination_clone(tmp_path)
    branch = "kalshi-router/MLB"

    # A first delivery, so the branch exists and the lease is non-empty.
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "-b", branch], check=True)
    (work / "data" / "bets.jsonl").write_text('{"first": true}\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "first"], check=True)
    subprocess.run(
        ["git", "-C", str(work), "push", "-q", "origin", f"HEAD:refs/heads/{branch}"],
        check=True, capture_output=True,
    )

    # A new run reads the lease...
    later = tmp_path / "later"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{remote}", str(later)], check=True
    )
    for key, value in (("user.email", "t@example.invalid"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(later), "config", key, value], check=True)
    subprocess.run(["git", "-C", str(later), "checkout", "-q", "-b", branch], check=True)
    (later / "data" / "bets.jsonl").write_text('{"later": true}\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(later), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(later), "commit", "-qm", "later"], check=True)
    subprocess.run(
        ["git", "-C", str(later), "fetch", "-q", "--depth", "1", "origin",
         f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
        capture_output=True,
    )

    # ...and somebody else moves the branch before it pushes.
    intruder = tmp_path / "intruder"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{remote}", str(intruder)], check=True
    )
    for key, value in (("user.email", "x@example.invalid"), ("user.name", "x")):
        subprocess.run(["git", "-C", str(intruder), "config", key, value], check=True)
    subprocess.run(["git", "-C", str(intruder), "checkout", "-q", "-b", branch], check=True)
    (intruder / "data" / "bets.jsonl").write_text('{"intruder": true}\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(intruder), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(intruder), "commit", "-qm", "intruder"], check=True)
    subprocess.run(
        ["git", "-C", str(intruder), "push", "-q", "--force", f"file://{remote}",
         f"HEAD:refs/heads/{branch}"],
        check=True, capture_output=True,
    )

    script = (
        f'set -euo pipefail\n'
        f'work={shlex.quote(str(later))}; branch={shlex.quote(branch)}\n'
        f'{deliver_expected_assignment()}\n'
        f'git -C "${{work}}" push -q '
        f'--force-with-lease="refs/heads/${{branch}}:${{expected}}" '
        f'origin "HEAD:refs/heads/${{branch}}"'
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    assert result.returncode != 0, (
        "the lease accepted a push over a branch someone else had moved"
    )
    assert "stale info" in result.stderr, result.stderr


# ============ The one-time historical catch-up ==============================
#
# `backfill-inspect.yml` answers "what did we miss" and holds no write
# credential. This is the workflow that acts on that answer, and the properties
# below are what keep a catch-up from becoming a second production path.

BACKFILL_DELIVER = ROOT / ".github/workflows/backfill-deliver.yml"


@pytest.fixture(scope="module")
def backfill() -> dict:
    return load(BACKFILL_DELIVER)


@pytest.fixture(scope="module")
def backfill_text() -> str:
    return strip_comments(BACKFILL_DELIVER.read_text())


def test_the_catch_up_is_never_scheduled(backfill):
    """A one-time catch-up on a timer is not one-time.

    The inspection workflow is safe to re-run; this one writes history into
    three public ledgers.
    """
    assert set(triggers(backfill)) == {"workflow_dispatch"}


def test_the_catch_up_defaults_to_a_dry_run(backfill):
    default = triggers(backfill)["workflow_dispatch"]["inputs"]["dry_run"]["default"]
    assert default in (True, "true"), f"dry_run defaults to {default!r}"


def test_pushing_requires_a_typed_acknowledgement(backfill_text):
    """Not a checkbox. The phrase has to be typed, because the act it authorises
    -- writing the owner's betting history into three public repositories -- is
    not one this workflow can undo."""
    assert "I have decided to import pre-cutover history" in backfill_text
    assert "not reversible" in backfill_text


def test_the_acknowledgement_is_only_required_for_a_real_push(backfill_text):
    """A dry run must stay easy, or nobody will do one before the real thing."""
    assert '[ "${DRY_RUN}" = "false" ]' in backfill_text


def test_an_unreadable_dry_run_value_is_refused_by_the_catch_up(backfill_text):
    """Empty must never fall through a `!= "true"` test and read as 'push'."""
    assert "dry_run must be exactly" in backfill_text


def test_the_cfb_season_must_be_a_year_because_it_names_a_file(backfill_text):
    """`wagers/<season>.jsonl`. A non-numeric value would put real wagers in a
    file no report reads."""
    assert "[0-9][0-9][0-9][0-9]" in backfill_text
    assert "four-digit year" in backfill_text


def test_the_catch_up_refuses_a_ref_other_than_main(backfill_text):
    assert "refs/heads/main" in backfill_text
    assert "exit 1" in backfill_text


def test_the_catch_up_holds_no_write_permission_on_this_repository(backfill):
    assert backfill["permissions"] == {"contents": "read"}


def test_the_catch_up_is_never_cancelled_in_flight(backfill):
    """Cancellation between a destination's commit and this job's receipt would
    leave canonical wagers written with no record here that they were."""
    assert backfill["concurrency"]["cancel-in-progress"] is False


def test_the_catch_up_uploads_no_artifact(backfill_text):
    assert "upload-artifact" not in backfill_text


def test_the_catch_up_never_puts_the_credential_in_a_url_or_argv(backfill_text):
    """The token reaches git through a helper that reads it from the
    environment at push time."""
    assert "x-access-token:${DOWNSTREAM_REPO_TOKEN}" not in backfill_text
    assert "credential.helper" in backfill_text
    for clone in re.findall(r"git clone[^\n]*", backfill_text):
        assert "TOKEN" not in clone, clone


def test_the_catch_up_reads_every_destination_it_writes_to(backfill_text):
    """A destination whose existing rows were never read must not be written
    to: that is precisely how a backfill duplicates a ledger. Every sport the
    delivery loop knows how to push to must also appear as a --ledger."""
    written = set(re.findall(r"^\s*(MLB|NFL|CFB)\) repo=", backfill_text, re.M))
    read = set(re.findall(r'--ledger "(MLB|NFL|CFB)=', backfill_text))
    assert written, "the delivery loop routes nothing"
    assert written == read, f"written {sorted(written)} but read {sorted(read)}"


def test_the_catch_up_runs_each_destinations_own_importer(backfill_text):
    """Never a hand-written ledger edit. Bypassing the importer would bypass
    its duplicate detection and its validation, which are the properties that
    make a re-run safe."""
    for importer in ("scripts/edgelab/import_bet_batch.py",
                     "scripts/handicap/import_routed_wagers.py",
                     "scripts/import_routed_wagers.py"):
        assert importer in backfill_text


def test_the_catch_up_never_pushes_to_a_destinations_own_branch(backfill_text):
    """The router proposes; the owner merges. This matters more here than in
    production, because two of the three destinations keep their ledger on a
    data branch that a bad push would corrupt directly."""
    joined = backfill_text.replace("\\\n", " ")
    pushes = [line for line in joined.splitlines() if "git" in line and " push" in line]
    assert pushes, "the catch-up never pushes"
    for line in pushes:
        assert 'HEAD:refs/heads/${branch}' in line, line
        assert "--force " not in line, "force-with-lease, never bare force"

    assignments = [line.strip() for line in joined.splitlines() if line.strip().startswith("branch=")]
    assert assignments, "the catch-up sets no branch"
    for line in assignments:
        assert line.startswith('branch="kalshi-router/'), line
        for unstable in ("$RANDOM", "date +%s", "GITHUB_RUN_ID", "rev-parse", "$(git"):
            assert unstable not in line, f"branch name depends on {unstable}: {line}"


def test_a_catch_up_that_cannot_open_a_pull_request_is_a_FAILURE(backfill_text):
    assert "/pulls" in backfill_text
    assert "are NOT recorded" in backfill_text
    assert "422)" in backfill_text


def test_one_destinations_failure_does_not_roll_back_another(backfill_text):
    assert "failures=$((failures + 1))" in backfill_text
    assert "continue" in backfill_text


def test_a_sport_with_nowhere_to_go_is_a_failure_not_a_silent_skip(backfill_text):
    """A wager this router reconstructed and then dropped is the one outcome
    that looks like success and is not."""
    assert "has no configured destination repository" in backfill_text


def test_the_catch_up_never_prints_the_payload(backfill_text):
    """Only counts reach a public Actions log, and the payload carries market,
    side, contracts, price, stake and fees for every wager in the window."""
    assert "--show-sensitive-details" not in backfill_text

    mentions = [line.strip() for line in backfill_text.splitlines() if "${payload}" in line]
    assert mentions, "the payload is never used; this test would prove nothing"
    for line in mentions:
        assert not line.startswith(("echo", "cat", "printf", "tee")), line


def test_the_catch_up_cannot_widen_its_own_window(backfill_text):
    """The end is PRODUCTION_CUTOVER_ISO structurally, inside BackfillWindow.

    Checked as "the only window argument passed is --since" rather than as
    "no input is named end", because the second would pass just as happily
    against a workflow that hard-coded a later cutover on the command line.
    """
    joined = backfill_text.replace("\\\n", " ")
    invocations = [line for line in joined.splitlines() if "kalshi_router.cli backfill" in line]
    assert invocations, "the catch-up never runs the backfill command"
    for line in invocations:
        assert "--since" in line
        for widening in ("--until", "--end", "--cutover", "--include-pre-cutover"):
            assert widening not in line, f"{widening} would move the window's end: {line}"


def test_an_unreadable_ledger_is_never_read_as_an_empty_one(backfill_text):
    """"NOT THE LEDGER" AND "THE LEDGER IS EMPTY" PRODUCE THE SAME NUMBER.

    Only one of them is safe to act on. Zero existing rows is exactly the input
    that makes every wager in the window MISSING_IMPORTABLE, so a renamed
    directory, a wrong branch or a clone that landed somewhere else would
    duplicate a ledger on a run whose log said "0 rows" and looked correct.

    Each destination therefore proves the checkout IS its ledger before any
    absence is allowed to mean "nothing written yet".
    """
    assert "does not look like the handicap ledger" in backfill_text
    assert "it is not the ledger" in backfill_text
    assert "refusing to read it as empty" in backfill_text


def test_each_destinations_existing_row_count_reaches_the_log(backfill_text):
    """Three numbers a human can sanity-check before authorising a push. A
    delivery whose reconciliation was formed against a ledger nobody looked at
    is a delivery nobody can check."""
    for sport in ("MLB", "NFL", "CFB"):
        assert f"{sport} ledger rows" in backfill_text


def test_a_refused_row_does_not_cost_the_rows_that_succeeded(backfill_text):
    """One wager whose week cannot be resolved is one refusal. The other
    twenty-three are already written into the working tree.

    Skipping the commit would throw all of them away over one bad row -- and
    would keep doing it on every re-run for as long as that row stayed
    unfixable, so the delivery could never complete. The failure is COUNTED so
    the job still exits non-zero; the accepted rows are still proposed.
    """
    lines = backfill_text.splitlines()
    refusal = [i for i, line in enumerate(lines) if "refused at least one row" in line]
    assert refusal, "the refusal branch is gone"
    branch = "\n".join(lines[refusal[0]:refusal[0] + 6])

    # Abandoning the destination is reachable for MLB ONLY, and reaching it
    # requires naming MLB outright -- a destination added later cannot inherit
    # the bail-out by accident.
    assert 'if [ "${sport}" = "MLB" ]; then' in branch, branch
    assert "continue" in branch, branch


def test_a_refusal_still_fails_the_job(backfill_text):
    """Committing what succeeded must not turn a refusal into a quiet success.
    The two are separate decisions and both are made here."""
    lines = backfill_text.splitlines()
    refusal = [i for i, line in enumerate(lines) if "refused at least one row" in line]
    assert refusal, "the refusal branch is gone"
    following = "\n".join(lines[refusal[0]:refusal[0] + 6])
    assert "failures=$((failures + 1))" in following, following


# ============ The settlement half of the catch-up ===========================

BACKFILL_SETTLE = ROOT / ".github/workflows/backfill-settle.yml"


@pytest.fixture(scope="module")
def settle() -> dict:
    return load(BACKFILL_SETTLE)


@pytest.fixture(scope="module")
def settle_text() -> str:
    return strip_comments(BACKFILL_SETTLE.read_text())


def test_the_settlement_pass_is_never_scheduled(settle):
    assert set(triggers(settle)) == {"workflow_dispatch"}


def test_the_settlement_pass_defaults_to_a_dry_run(settle):
    default = triggers(settle)["workflow_dispatch"]["inputs"]["dry_run"]["default"]
    assert default in (True, "true"), f"dry_run defaults to {default!r}"


def test_the_settlement_pass_requires_the_same_typed_acknowledgement(settle_text):
    assert "I have decided to import pre-cutover history" in settle_text
    assert "not reversible" in settle_text


def test_the_settlement_pass_refuses_a_ref_other_than_main(settle_text):
    assert "refs/heads/main" in settle_text
    assert "exit 1" in settle_text


def test_the_settlement_pass_holds_no_write_permission_here(settle):
    assert settle["permissions"] == {"contents": "read"}


def test_the_settlement_pass_is_never_cancelled_in_flight(settle):
    assert settle["concurrency"]["cancel-in-progress"] is False


def test_the_settlement_pass_uploads_no_artifact(settle_text):
    assert "upload-artifact" not in settle_text


def test_mlb_is_not_a_settlement_destination(settle_text):
    """edge-finder-api settles its own bets: settle_markets.py re-derives every
    outcome from the MLB Stats API. A settlement pushed there by this router
    would be a SECOND authority on the same fact, and the two would disagree the
    first time one of them was wrong.

    Checked on the DELIVERY LOOP's routing table rather than on the whole file,
    because the header explains at length why MLB is absent and a substring
    search would find it there.
    """
    routed = set(re.findall(r"^\s*(MLB|NFL|CFB)\) repo=", settle_text, re.M))

    assert routed == {"NFL", "CFB"}, routed
    assert "--destination NFL" in settle_text
    assert "--destination CFB" in settle_text
    assert "--destination MLB" not in settle_text


def test_the_settlement_pass_writes_only_to_the_settlement_ledger(settle_text):
    """A settlement import that touched a WAGER file would be rewriting a record
    of money that already moved, which both destinations forbid."""
    assert "^settlements/" in settle_text
    assert "^data/wager_settlements/" in settle_text
    assert "outside the settlement ledger" in settle_text


def test_the_settlement_pass_never_pushes_to_a_destinations_own_branch(settle_text):
    joined = settle_text.replace("\\\n", " ")
    pushes = [line for line in joined.splitlines() if "git" in line and " push" in line]
    assert pushes, "the settlement pass never pushes"
    for line in pushes:
        assert 'HEAD:refs/heads/${branch}' in line, line
        assert "--force " not in line

    assignments = [line.strip() for line in joined.splitlines() if line.strip().startswith("branch=")]
    assert assignments, "the settlement pass sets no branch"
    for line in assignments:
        assert line.startswith('branch="kalshi-router/'), line
        for unstable in ("$RANDOM", "date +%s", "GITHUB_RUN_ID", "rev-parse", "$(git"):
            assert unstable not in line, line


def test_a_settlement_pass_that_cannot_open_a_pull_request_is_a_FAILURE(settle_text):
    assert "/pulls" in settle_text
    assert "are NOT recorded" in settle_text
    assert "422)" in settle_text


def test_a_refused_settlement_does_not_cost_the_ones_that_succeeded(settle_text):
    """Same rule as the wager delivery, for the same reason: both importers
    screen per row, and the accepted rows are already in the working tree."""
    lines = settle_text.splitlines()
    refusal = [i for i, line in enumerate(lines) if "refused at least one settlement" in line]
    assert refusal, "the refusal branch is gone"
    branch = "\n".join(lines[refusal[0]:refusal[0] + 4])
    assert "failures=$((failures + 1))" in branch, branch
    assert "continue" not in branch, branch


def test_the_settlement_window_cannot_be_widened(settle_text):
    joined = settle_text.replace("\\\n", " ")
    invocations = [line for line in joined.splitlines() if "kalshi_router.cli settle" in line]
    assert invocations, "the pass never runs the settle command"
    for line in invocations:
        assert "--since" in line
        for widening in ("--until", "--end", "--cutover", "--include-pre-cutover"):
            assert widening not in line, line


def test_the_settlement_pass_never_prints_the_payload(settle_text):
    assert "--show-sensitive-details" not in settle_text
    mentions = [line.strip() for line in settle_text.splitlines() if "${payload}" in line]
    assert mentions
    for line in mentions:
        assert not line.startswith(("echo", "cat", "printf", "tee")), line
