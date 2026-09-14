"""CLI behaviour: exit codes, output modes, and the local sensitive mode."""

from __future__ import annotations

import io
import json

import pytest

from kalshi_router import cli
from kalshi_router.client import KalshiReadOnlyClient
from kalshi_router.config import AuditConfig
from kalshi_router.safety import CI_ENV_MARKERS

from .synthetic import FAKE_KEY_ID, FakeTransport, SENSITIVE_TOKENS, make_fill, paged_fills_handler
from .test_audit import build_metadata, market_for


@pytest.fixture()
def local_env(monkeypatch, fake_private_key_pem):
    """A private-terminal environment with valid-shaped synthetic credentials."""
    for marker in CI_ENV_MARKERS:
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.setenv("KALSHI_API_KEY_ID", FAKE_KEY_ID)
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", fake_private_key_pem)


def install_fake_api(monkeypatch, pages, metadata=None):
    """Replace the client's transport so no network call is ever made."""
    handler = paged_fills_handler(pages, metadata if metadata is not None else build_metadata())

    def factory(signer, config):
        return KalshiReadOnlyClient(
            signer=signer,
            config=AuditConfig(
                base_url=config.base_url,
                max_fills=config.max_fills,
                page_limit=config.page_limit,
                max_retries=0,
            ),
            transport=FakeTransport(handler),
            sleep=lambda _: None,
        )

    monkeypatch.setattr(cli, "KalshiReadOnlyClient", factory)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


SAMPLE = [[
    make_fill(1, ticker=market_for("MLB")),
    make_fill(2, ticker=market_for("NFL"), action="sell", side="no"),
    make_fill(3, ticker=market_for("AMB")),
]]


def test_audit_prints_aggregate_counts_and_exits_zero(monkeypatch, local_env):
    install_fake_api(monkeypatch, SAMPLE)
    code, out, err = run(["audit", "--max-fills", "50"])
    assert code == cli.EXIT_OK
    assert "fills fetched: 3" in out
    assert "unique fills: 3" in out
    assert "  MLB: 1" in out and "  NFL: 1" in out and "  UNRESOLVED: 1" in out
    assert err == ""


def test_audit_output_never_names_a_market_or_identifier(monkeypatch, local_env):
    install_fake_api(monkeypatch, SAMPLE)
    _, out, _ = run(["audit"])
    for token in SENSITIVE_TOKENS:
        assert token not in out
    assert market_for("MLB") not in out


def test_json_mode_emits_counts_only(monkeypatch, local_env):
    install_fake_api(monkeypatch, SAMPLE)
    code, out, _ = run(["audit", "--json"])
    data = json.loads(out)
    assert code == cli.EXIT_OK
    assert data["fills_fetched"] == 3
    assert data["classification_MLB"] == 1
    assert all(isinstance(v, (int, bool)) for v in data.values())


def test_empty_account_exits_zero_and_says_so(monkeypatch, local_env):
    install_fake_api(monkeypatch, [[]])
    code, out, _ = run(["audit"])
    assert code == cli.EXIT_OK
    assert "fills fetched: 0" in out
    assert "valid empty state, not a failure" in out


def test_api_failure_is_distinguished_from_an_empty_account(monkeypatch, local_env):
    install_fake_api(monkeypatch, [[]], metadata={})
    monkeypatch.setattr(
        cli,
        "KalshiReadOnlyClient",
        lambda signer, config: KalshiReadOnlyClient(
            signer=signer,
            config=AuditConfig(max_retries=0),
            transport=FakeTransport(lambda m, p, q: (500, {"error": "boom"})),
            sleep=lambda _: None,
        ),
    )
    code, out, err = run(["audit"])
    assert code == cli.EXIT_API
    assert out == ""
    assert "HttpStatusError" in err and "500" in err


def test_missing_credentials_exit_before_any_request(monkeypatch):
    for name in ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY"):
        monkeypatch.delenv(name, raising=False)
    code, out, err = run(["audit"])
    assert code == cli.EXIT_CONFIG
    assert out == ""
    assert "KALSHI_API_KEY_ID" in err


def test_out_of_range_max_fills_is_a_configuration_error(monkeypatch, local_env):
    code, _, err = run(["audit", "--max-fills", "5000"])
    assert code == cli.EXIT_CONFIG
    assert "max_fills" in err


def test_sensitive_mode_prints_detail_locally(monkeypatch, local_env):
    install_fake_api(monkeypatch, SAMPLE)
    code, out, _ = run(["audit", "--show-sensitive-details"])
    assert code == cli.EXIT_OK
    assert "SENSITIVE LOCAL DIAGNOSTICS" in out
    assert market_for("MLB") in out
    # The aggregate block is still printed first.
    assert out.index("fills fetched: 3") < out.index("SENSITIVE LOCAL DIAGNOSTICS")


def test_sensitive_mode_is_off_by_default(monkeypatch, local_env):
    install_fake_api(monkeypatch, SAMPLE)
    _, out, _ = run(["audit"])
    assert "SENSITIVE" not in out


@pytest.mark.parametrize("marker", ["GITHUB_ACTIONS", "CI", "GITHUB_RUN_ID"])
def test_sensitive_mode_refuses_in_automation(monkeypatch, local_env, marker):
    install_fake_api(monkeypatch, SAMPLE)
    monkeypatch.setenv(marker, "true")
    code, out, err = run(["audit", "--show-sensitive-details"])
    assert code == cli.EXIT_SENSITIVE_REFUSED
    assert out == ""
    assert "private local terminal" in err


def test_workflow_invocation_under_actions_emits_only_aggregates(monkeypatch, local_env):
    """The exact command the workflow runs, in the workflow's environment."""
    install_fake_api(monkeypatch, SAMPLE)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    code, out, err = run(["audit", "--max-fills", "200"])
    assert code == cli.EXIT_OK
    assert "SENSITIVE" not in out
    for token in SENSITIVE_TOKENS:
        assert token not in out
    assert "KX" not in out  # no Kalshi ticker of any kind reaches the log
    assert err == ""


def test_unknown_subcommand_is_rejected():
    with pytest.raises(SystemExit):
        cli.main(["route"], stdout=io.StringIO(), stderr=io.StringIO())


# ---------------------------- Phase 1A: shadow accounting in the live audit

def test_audit_prints_the_shadow_accounting_block(monkeypatch, local_env):
    install_fake_api(monkeypatch, SAMPLE)
    code, out, _ = run(["audit"])
    assert code == cli.EXIT_OK
    assert "shadow accounting diagnostics (no routing, no persistence)" in out
    assert "orders with partial fills" in out
    assert "position episodes observed" in out


def test_audit_never_claims_position_state_from_a_bounded_window(monkeypatch, local_env):
    install_fake_api(monkeypatch, SAMPLE)
    _, out, _ = run(["audit"])
    assert "history supplied is complete: False" in out
    assert "position state claimed as authoritative: False" in out
    assert "NOT the account's position state" in out


def test_accounting_block_leaks_no_identifiers_or_values(monkeypatch, local_env):
    install_fake_api(monkeypatch, SAMPLE)
    _, out, _ = run(["audit"])
    for token in SENSITIVE_TOKENS:
        assert token not in out
    assert "KX" not in out
    assert "$" not in out


def test_json_mode_includes_accounting_counts_only(monkeypatch, local_env):
    install_fake_api(monkeypatch, SAMPLE)
    _, out, _ = run(["audit", "--json"])
    data = json.loads(out)
    accounting_keys = [k for k in data if k.startswith("accounting_")]
    assert accounting_keys
    assert all(isinstance(data[k], (int, bool)) for k in accounting_keys)
    assert data["accounting_claims_complete_position_state"] is False


def test_undated_fills_are_rejected_at_ingestion_not_mid_replay(monkeypatch, local_env):
    """An unorderable fill is excluded and counted, leaving accounting intact."""
    undated = [dict(f) for f in SAMPLE[0]]
    for raw in undated:
        raw.pop("created_time", None)
        raw.pop("ts", None)
    install_fake_api(monkeypatch, [undated])
    code, out, _ = run(["audit"])
    assert code == cli.EXIT_OK
    assert "fills seen: 3" in out
    assert "fills rejected (excluded from accounting): 3" in out
    assert "    timestamp: 3" in out
    # Accounting still ran cleanly on what remained (nothing).
    assert "accounting schema failures: 0" in out


def test_live_schema_coverage_block_is_printed(monkeypatch, local_env):
    install_fake_api(monkeypatch, SAMPLE)
    _, out, _ = run(["audit"])
    assert "live schema coverage (counts only):" in out
    assert "with outcome_side (canonical):" in out
    assert "both present and DISAGREEING:" in out
    assert "present but malformed:" in out


def test_schema_coverage_leaks_nothing(monkeypatch, local_env):
    install_fake_api(monkeypatch, SAMPLE)
    _, out, _ = run(["audit"])
    for token in SENSITIVE_TOKENS:
        assert token not in out
    assert "$" not in out
