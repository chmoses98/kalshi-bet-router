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
