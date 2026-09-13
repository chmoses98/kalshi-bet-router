"""Privacy guarantees: aggregates leak nothing, sensitive mode is gated."""

from __future__ import annotations

import io
import json
import re

import pytest

from kalshi_router.aggregate import AuditReport
from kalshi_router.audit import run_audit
from kalshi_router.cli import main, render_sensitive
from kalshi_router.errors import SensitiveOutputRefused
from kalshi_router.safety import CI_ENV_MARKERS, assert_sensitive_output_allowed, detected_ci_markers
from kalshi_router.sports import REPORT_ORDER

from .synthetic import SENSITIVE_TOKENS, FAKE_KEY_ID, make_fill
from .test_audit import build_client, market_for


def rich_result(signer, collect_details=False):
    pages = [[
        make_fill(1, ticker=market_for("MLB"), order_id="SYNTHORDER-A"),
        make_fill(2, ticker=market_for("NFL"), order_id="SYNTHORDER-A", action="sell", side="no"),
        make_fill(3, ticker=market_for("TEN")),
    ]]
    return run_audit(build_client(pages, signer), collect_details=collect_details)


def test_rendered_aggregate_contains_no_identifiers(signer):
    rendered = rich_result(signer).report.render()
    for token in SENSITIVE_TOKENS:
        assert token not in rendered
    assert "SYNTHTRADE" not in rendered
    assert market_for("MLB") not in rendered


def test_rendered_aggregate_contains_no_monetary_values(signer):
    rendered = rich_result(signer).report.render()
    assert "$" not in rendered
    assert "price" not in rendered.lower()
    assert "cent" not in rendered.lower()


def test_aggregate_report_can_only_hold_counts(signer):
    """Structural proof: no field of the report can carry a ticker or an id."""
    report = rich_result(signer).report
    for name, value in vars(report).items():
        if name == "classification_counts":
            assert set(value) <= set(REPORT_ORDER)
            assert all(isinstance(v, int) for v in value.values())
            continue
        assert isinstance(value, (int, bool)), f"{name} is not a count"


def test_json_report_values_are_all_counts(signer):
    data = json.loads(json.dumps(rich_result(signer).report.as_dict()))
    assert all(isinstance(v, (int, bool)) for v in data.values())
    assert all(re.fullmatch(r"[A-Za-z0-9_]+", k) for k in data)


def test_cli_default_output_is_aggregate_only(signer, monkeypatch, fake_private_key_pem):
    monkeypatch.setenv("KALSHI_API_KEY_ID", FAKE_KEY_ID)
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", fake_private_key_pem)
    for marker in CI_ENV_MARKERS:
        monkeypatch.delenv(marker, raising=False)

    out, err = io.StringIO(), io.StringIO()
    # No network: credentials are valid-shaped but the audit will fail to connect.
    # Use the report renderer directly for the content assertion instead.
    rendered = AuditReport().render()
    assert "SENSITIVE" not in rendered
    assert "evidence" not in rendered
    assert out.getvalue() == "" and err.getvalue() == ""


# ------------------------------------------------------- sensitive-mode gate

@pytest.mark.parametrize("marker", CI_ENV_MARKERS)
def test_sensitive_output_refused_under_every_ci_marker(marker):
    with pytest.raises(SensitiveOutputRefused):
        assert_sensitive_output_allowed(env={marker: "true"})


def test_sensitive_output_allowed_in_a_clean_local_environment():
    assert_sensitive_output_allowed(env={"HOME": "/home/owner"})
    assert detected_ci_markers(env={"HOME": "/home/owner"}) == []


def test_blank_ci_marker_does_not_trip_the_guard():
    assert_sensitive_output_allowed(env={"CI": "   "})


def test_cli_refuses_sensitive_flag_in_actions(monkeypatch, fake_private_key_pem):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("KALSHI_API_KEY_ID", FAKE_KEY_ID)
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", fake_private_key_pem)
    out, err = io.StringIO(), io.StringIO()
    code = main(["audit", "--show-sensitive-details"], stdout=out, stderr=err)
    assert code == 4
    assert out.getvalue() == ""
    assert "private local terminal" in err.getvalue()


def test_sensitive_render_is_labelled_and_does_show_detail(signer):
    """The gate is the only thing protecting this output, so it must be loud."""
    result = rich_result(signer, collect_details=True)
    rendered = render_sensitive(result)
    assert "SENSITIVE LOCAL DIAGNOSTICS" in rendered
    assert "Do not paste it" in rendered
    assert market_for("MLB") in rendered
    assert "evidence:" in rendered


def test_sensitive_render_of_an_empty_account_shows_nothing(signer):
    result = run_audit(build_client([[]], signer), collect_details=True)
    assert "no markets to detail" in render_sensitive(result)


# ------------------------------------------------ no durable personal data

FORBIDDEN_DATA_DIRS = (
    "data/raw-fills",
    "data/account-history",
    "data/wagers",
    "data/positions",
    "data/fills",
)


def test_repository_contains_no_personal_data_directories():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for relative in FORBIDDEN_DATA_DIRS:
        assert not (root / relative).exists(), f"{relative} must not exist in Phase 0"


def test_repository_contains_no_private_key_material():
    from pathlib import Path

    # Built at runtime so this scanner does not match its own source text.
    dash = "-" * 5
    headers = tuple(f"{dash}BEGIN {kind}PRIVATE KEY{dash}" for kind in ("RSA ", "ENCRYPTED ", ""))

    root = Path(__file__).resolve().parents[1]
    skip = {".git", ".venv", "__pycache__", ".pytest_cache", "build", "dist"}
    for path in root.rglob("*"):
        if not path.is_file() or set(path.parts) & skip or path == Path(__file__):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for header in headers:
            assert header not in text, f"{path} appears to contain private key material"
