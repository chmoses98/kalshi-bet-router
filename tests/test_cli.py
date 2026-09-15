"""CLI behaviour: exit codes, output modes, and the local sensitive mode."""

from __future__ import annotations

import io
import json

import pytest

from kalshi_router import cli
from kalshi_router.client import KalshiReadOnlyClient
from kalshi_router.config import AuditConfig
from kalshi_router.safety import CI_ENV_MARKERS, safe_schema_name

from .synthetic import FAKE_KEY_ID, FakeTransport, SENSITIVE_TOKENS, make_fill, paged_fills_handler
from .test_audit import build_metadata, market_for


@pytest.fixture()
def local_env(monkeypatch, fake_private_key_pem):
    """A private-terminal environment with valid-shaped synthetic credentials."""
    for marker in CI_ENV_MARKERS:
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.setenv("KALSHI_API_KEY_ID", FAKE_KEY_ID)
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", fake_private_key_pem)


def install_fake_api(monkeypatch, pages, metadata=None, taxonomy=None):
    """Replace the client's transport so no network call is ever made."""
    handler = paged_fills_handler(
        pages,
        metadata if metadata is not None else build_metadata(),
        taxonomy=taxonomy,
    )

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
    for key, value in data.items():
        if key in ("taxonomy_observed_keys", "taxonomy_observed_entry_keys",
                   "milestone_entry_keys"):
            # The one vetted exception: public schema names, allowlisted at the
            # source.  Still never a free-form string.
            assert all(safe_schema_name(e.partition(":")[0]) for e in value)
            continue
        assert isinstance(value, (int, bool)), f"{key} is not a count"


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
    assert "fill history is complete: False" in out
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


# ================= Phase C: opt-in reconciliation measurement ================

def test_reconciliation_is_off_by_default(monkeypatch, local_env):
    """It walks two more paginated collections, so it must be asked for."""
    install_fake_api(monkeypatch, SAMPLE)
    _, out, _ = run(["audit"])
    assert "reconciliation probe" not in out


def test_reconcile_flag_renders_the_measurement(monkeypatch, local_env):
    install_fake_api(monkeypatch, SAMPLE)
    _, out, _ = run(["audit", "--reconcile"])
    assert "reconciliation probe" in out
    assert "not a reconciliation verdict" in out


def test_reconciliation_output_leaks_nothing(monkeypatch, local_env):
    install_fake_api(monkeypatch, SAMPLE)
    _, out, _ = run(["audit", "--reconcile"])
    for token in SENSITIVE_TOKENS:
        assert token not in out
    assert "KX" not in out
    assert "$" not in out


def test_settlement_replay_output_leaks_nothing(monkeypatch, local_env):
    install_fake_api(monkeypatch, SAMPLE)
    _, out, _ = run(["audit", "--reconcile"])
    for token in SENSITIVE_TOKENS:
        assert token not in out
    assert "KX" not in out
    assert "$" not in out
    assert "settled by the exchange" in out


# ============ full history: claiming COMPLETE must be earned =================

def test_history_evidence_is_always_rendered(monkeypatch, local_env):
    install_fake_api(monkeypatch, SAMPLE)
    _, out, _ = run(["audit"])
    assert "history completeness evidence:" in out


def test_a_default_audit_never_claims_a_complete_history(monkeypatch, local_env):
    """It never walks the archive, so it cannot know what predates the cutoff."""
    install_fake_api(monkeypatch, SAMPLE)
    _, out, _ = run(["audit"])
    assert "archive skipped: True" in out
    assert "HISTORY IS COMPLETE: False" in out


def test_a_full_history_walk_that_exhausts_both_routes_claims_complete(
    monkeypatch, local_env
):
    install_fake_api(monkeypatch, SAMPLE)
    _, out, _ = run(["audit", "--full-history"])
    assert "HISTORY IS COMPLETE: True" in out


def test_full_history_ignores_the_fill_budget_entirely(monkeypatch, local_env):
    """A budget and a completeness claim are mutually exclusive.

    --full-history used to be subject to the 1-500 max_fills cap, which made
    COMPLETE unreachable by construction: a capped walk can never report
    `exhausted`. It now walks to exhaustion and ignores the budget, rather than
    making the operator guess a number large enough to be safe.
    """
    install_fake_api(monkeypatch, SAMPLE)
    _, out, _ = run(["audit", "--full-history", "--max-fills", "1"])
    assert "walk truncated (budget ran out): False" in out
    assert "HISTORY IS COMPLETE: True" in out


def test_a_bounded_audit_still_reports_its_budget_truncation(monkeypatch, local_env):
    """The budget still applies, and is still reported, without --full-history."""
    install_fake_api(monkeypatch, SAMPLE)
    _, out, _ = run(["audit", "--max-fills", "1"])
    assert "HISTORY IS COMPLETE: False" in out


def test_history_evidence_leaks_nothing(monkeypatch, local_env):
    install_fake_api(monkeypatch, SAMPLE)
    _, out, _ = run(["audit", "--full-history"])
    for token in SENSITIVE_TOKENS:
        assert token not in out
    assert "KX" not in out
    assert "$" not in out


def test_an_exchange_contradiction_suppresses_the_authority_claim(monkeypatch, local_env):
    """A complete fill history plus a contradicted position state must not read
    as authoritative. The exhaustive live run hit exactly this: both fill walks
    exhausted, and the replay still held markets the exchange does not report.
    """
    install_fake_api(monkeypatch, SAMPLE)
    _, out, _ = run(["audit", "--full-history", "--reconcile"])
    if "absent from positions, UNEXPLAINED: 0" not in out:
        assert "position state claimed as authoritative: False" in out
        assert "CONTRADICTED BY THE EXCHANGE" in out


# ---------------------------------------------------------------------------
# Every subcommand must survive the shared setup in main().
#
# The first release of `deliver` did not: it declared no --max-fills, so
# AuditConfig evaluated `1 <= None` and the command died before making a single
# request. The bug was invisible to the unit tests because each of them
# exercised one subcommand's handler directly, never the shared prologue.
#
# These tests enumerate the subparsers instead of naming them, so a subcommand
# added later is covered the day it is added rather than the day it breaks.
# ---------------------------------------------------------------------------


def _subcommand_argvs(tmp_path):
    """Minimal valid argv for every registered subcommand.

    Discovering the names from the parser (rather than hard-coding them) is the
    point: a new subcommand that this mapping does not cover fails the guard
    test below, which is a far better failure than a crash in production.
    """
    required = {
        "audit": [],
        "deliver": ["--out-dir", str(tmp_path / "payloads")],
        "series-probe": [],
        "backfill": ["--since", "2026-09-11T00:00:00Z"],
    }
    return required


def _registered_subcommands():
    parser = cli.build_parser()
    actions = [
        action
        for action in parser._actions  # noqa: SLF001 - argparse exposes no public API
        if isinstance(action, __import__("argparse")._SubParsersAction)
    ]
    assert len(actions) == 1, "the CLI is expected to have exactly one subcommand group"
    return sorted(actions[0].choices)


def test_every_subcommand_has_a_smoke_argv(tmp_path):
    """A new subcommand must be added to the coverage below, not forgotten."""
    assert set(_registered_subcommands()) == set(_subcommand_argvs(tmp_path))


@pytest.mark.parametrize("command", _registered_subcommands())
def test_every_subcommand_builds_a_usable_client_config(command, tmp_path):
    """The shared prologue must not crash on a subcommand's parsed arguments.

    This is the exact failure that reached a live run: AuditConfig received
    None for max_fills and compared it against an int.
    """
    argv = [command] + _subcommand_argvs(tmp_path)[command]
    args = cli.build_parser().parse_args(argv)

    config = cli.config_from_args(args)

    # Constructing it at all is most of the assertion -- AuditConfig validates
    # in __post_init__ -- but assert the values are usable rather than merely
    # present, so a future "fix" that passes 0 or a negative also fails here.
    assert config.max_fills >= 1
    assert config.page_limit >= 1


def test_deliver_runs_end_to_end_through_main(monkeypatch, local_env, tmp_path):
    """Run the exact command the delivery workflow runs, against a fake API.

    Every other deliver test calls the handler directly, which is why a crash
    in the shared prologue reached a live run. This one goes through main(),
    so the prologue is on the path.
    """
    install_fake_api(monkeypatch, SAMPLE)
    out_dir = tmp_path / "payloads"
    code, out, err = run(["deliver", "--out-dir", str(out_dir), "--allow-stabilization"])

    assert code == cli.EXIT_OK, err
    assert "payloads written" in out
    # And it is still counts-only: no ticker, no identifier.
    for token in SENSITIVE_TOKENS:
        assert token not in out
    assert market_for("MLB") not in out


def test_deliver_walks_without_a_sampling_ceiling(tmp_path):
    """`deliver` must not carry a budget that could truncate the order history.

    The ceiling on its config is inert (the walk is unbounded), and that has to
    stay true: a truncated walk would silently under-report the account's
    orders, and an under-reported order is a wager that never gets recorded.
    """
    args = cli.build_parser().parse_args(
        ["deliver", "--out-dir", str(tmp_path / "payloads")]
    )
    # The parsed namespace says "no budget"; the config supplies a default only
    # so the client has a legal shape.
    assert args.max_fills is None


# ---------------------------------------------------------------------------
# Phase 6: the historical shadow comparison is validation, never backfill.
# ---------------------------------------------------------------------------


def test_compare_ledger_without_shadow_wagers_is_refused(monkeypatch, local_env, tmp_path):
    """Comparing against an empty set would read as total disagreement."""
    ledger = tmp_path / "bets.jsonl"
    ledger.write_text("", encoding="utf-8")
    install_fake_api(monkeypatch, SAMPLE)

    code, out, err = run(["audit", "--compare-ledger", str(ledger)])

    assert code == cli.EXIT_CONFIG
    assert "--shadow-wagers" in err
    assert "COMPARISON" not in out


def test_compare_ledger_reports_counts_and_names_no_market(monkeypatch, local_env, tmp_path):
    ledger = tmp_path / "bets.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "marketTicker": market_for("MLB"),
                "side": "YES",
                "stake": 5.0,
                "entryPrice": 0.5,
                "result": "WIN",
                "sport": "MLB",
                "platform": "KALSHI",
                "recordStatus": "ACTIVE",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    install_fake_api(monkeypatch, SAMPLE)

    code, out, err = run(
        ["audit", "--shadow-wagers", "--compare-ledger", str(ledger)]
    )

    assert code == cli.EXIT_OK, err
    assert "HISTORICAL SHADOW COMPARISON" in out
    assert "ledger rows read: 1" in out
    # The ledger row it just read names a real market. That must not reach the log.
    assert market_for("MLB") not in out
    for token in SENSITIVE_TOKENS:
        assert token not in out


def test_no_comparison_section_appears_without_the_flag(monkeypatch, local_env):
    install_fake_api(monkeypatch, SAMPLE)
    _, out, _ = run(["audit", "--shadow-wagers"])
    assert "HISTORICAL SHADOW COMPARISON" not in out


# ---------------------------------------------------------------------------
# Phase 12: the pre-cutover acknowledgement.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase",
    [
        "true",
        "yes",
        "I have decided to import pre-cutover history.",  # trailing period
        "i have decided to import pre-cutover history",   # wrong case
        "",
    ],
)
def test_a_wrong_pre_cutover_acknowledgement_is_refused(
    phrase, monkeypatch, local_env, tmp_path
):
    """Refused, not silently downgraded to the safe behaviour.

    Someone who typed the flag intends to import history; quietly doing the
    normal thing would look like it worked.
    """
    install_fake_api(monkeypatch, SAMPLE)
    code, out, err = run(
        [
            "deliver",
            "--out-dir", str(tmp_path / "payloads"),
            "--include-pre-cutover", phrase,
        ]
    )

    assert code == cli.EXIT_CONFIG
    assert "acknowledgement" in err
    assert "payloads written" not in out


def test_the_exact_acknowledgement_is_accepted(monkeypatch, local_env, tmp_path):
    install_fake_api(monkeypatch, SAMPLE)
    code, out, err = run(
        [
            "deliver",
            "--out-dir", str(tmp_path / "payloads"),
            "--include-pre-cutover", cli.PRE_CUTOVER_ACKNOWLEDGEMENT,
        ]
    )

    assert code == cli.EXIT_OK, err
    assert "payloads written" in out


def test_deliver_without_the_flag_never_requests_pre_cutover(tmp_path):
    args = cli.build_parser().parse_args(
        ["deliver", "--out-dir", str(tmp_path / "payloads")]
    )
    assert args.include_pre_cutover is None


# ---------------------------------------------------------------------------
# The backfill command cannot be pointed at production's range.
# ---------------------------------------------------------------------------


def test_backfill_refuses_a_window_that_does_not_precede_the_cutover(
    monkeypatch, local_env
):
    install_fake_api(monkeypatch, SAMPLE)

    code, out, err = run(["backfill", "--since", "2026-09-20T00:00:00Z"])

    assert code == cli.EXIT_CONFIG
    assert "precede" in err


def test_backfill_has_no_flag_for_the_window_end(tmp_path):
    """The end is the cutover structurally. A settable end is the one way this
    command could quietly become a second production path."""
    parser = cli.build_parser()
    args = parser.parse_args(["backfill", "--since", "2026-09-11T00:00:00Z"])

    assert not hasattr(args, "until")
    assert not hasattr(args, "end")
    assert not hasattr(args, "through")


def test_backfill_rejects_a_malformed_ledger_spec(monkeypatch, local_env):
    install_fake_api(monkeypatch, SAMPLE)

    code, _out, err = run(
        ["backfill", "--since", "2026-09-11T00:00:00Z", "--ledger", "MLB"]
    )

    assert code == cli.EXIT_CONFIG
    assert "SPORT=PATH" in err


#: Fills inside the backfill window, so the report has a non-zero count to get
#: wrong. The first version of the test below used the default fixture, whose
#: fills fall outside the window -- so both numbers were 0 and it passed with
#: the wiring removed. A test that cannot fail is worse than no test.
IN_WINDOW = [[
    make_fill(1, ticker=market_for("MLB"), created_time="2026-09-12T18:00:00Z"),
    make_fill(2, ticker=market_for("MLB"), created_time="2026-09-13T18:00:00Z"),
    make_fill(3, ticker=market_for("MLB"), created_time="2026-09-14T18:00:00Z"),
]]


def test_backfill_reports_the_orders_it_actually_walked(monkeypatch, local_env):
    """The defect was in the WIRING, not in reconcile().

    `orders in the window: 0` printed above `reconciled: 42`, because the CLI
    never passed the count it already had.
    """
    install_fake_api(monkeypatch, IN_WINDOW)

    code, out, err = run(["backfill", "--since", "2026-09-11T00:00:00Z"])

    assert code == cli.EXIT_OK, err
    assert "orders in the window: 3" in out, out
    assert "INCONSISTENT" not in out, out


#: TWO orders on ONE unresolvable market. `AMB` in the audit scenario carries no
#: competition at all, so it stays UNRESOLVED, and distinct order ids make these
#: two orders rather than two fills of one.
TWO_ORDERS_ONE_MARKET = [[
    make_fill(11, ticker=market_for("AMB"), order_id="SYNTHORDER-A",
              created_time="2026-09-12T18:00:00Z", fee_cost="0.0100"),
    make_fill(12, ticker=market_for("AMB"), order_id="SYNTHORDER-B",
              created_time="2026-09-13T18:00:00Z", fee_cost="0.0100"),
]]


def test_the_report_says_which_counts_are_orders_and_which_are_markets(monkeypatch, local_env):
    """A live run printed `refused: 34` over reasons summing to 32.

    Both numbers were right. Refusals are counted per ORDER and unresolved
    reasons per distinct MARKET, because a market is classified once however
    many orders were placed on it. Under one heading with no unit stated, the
    only available reading was that the report could not add up -- which is the
    same defect as the contradictory window count, and just as corrosive: a
    report nobody can reconcile is not evidence, whatever its numbers.

    Two orders, one unresolvable market. The asymmetry must be visible AND
    explained.
    """
    install_fake_api(monkeypatch, TWO_ORDERS_ONE_MARKET)

    code, out, err = run(["backfill", "--since", "2026-09-11T00:00:00Z"])

    assert code == cli.EXIT_OK, err
    assert "orders in the window: 2" in out, out
    assert "sport unresolved: 2" in out, out
    # ...and ONE market behind them. This pair of numbers IS the asymmetry:
    # both are correct, and they differ because they count different things.
    assert "ambiguous family without a league: 1" in out, out
    # The heading has to say so, or the two numbers read as a contradiction.
    assert "distinct MARKETS, not orders" in out, out


def test_the_backfill_report_does_not_describe_its_window_as_post_cutover(monkeypatch, local_env):
    """The backfill window ENDS at the cutover, so every order in it is
    pre-cutover. The renderer is shared with the production path, which is why
    it used to call these markets "post-cutover" in a report about history."""
    install_fake_api(monkeypatch, TWO_ORDERS_ONE_MARKET)

    code, out, err = run(["backfill", "--since", "2026-09-11T00:00:00Z"])

    assert code == cli.EXIT_OK, err
    assert "post-cutover markets were unresolved" not in out, out


def test_the_series_probe_names_the_collisions_it_counts(monkeypatch, local_env):
    """The probe's counts and its names must both reach the log.

    Removing the naming call left every other CLI test passing -- the same
    untested-wiring shape as the `deliver` prologue crash and the window count
    that printed zero. The measurement being correct is not the property that
    matters here; the property is that its answer is READABLE by the person who
    has to act on it.
    """
    from .synthetic import make_taxonomy

    install_fake_api(
        monkeypatch,
        [[]],
        taxonomy=make_taxonomy({
            "Baseball": ["Pro Baseball"],
            "Football": ["Pro Baseball"],
        }),
    )

    code, out, err = run(["series-probe"])

    assert code == cli.EXIT_OK, err
    # the count...
    assert "collisions: 1" in out, out
    # ...and which one it is.
    assert "competition 'pro baseball'" in out, out
    assert "claimed by: baseball, football" in out, out
