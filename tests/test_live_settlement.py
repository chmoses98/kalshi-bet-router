"""The PRODUCTION settlement path, and the gap it closes.

*** WHAT WAS MISSING ***
`deliver-wagers.yml` recorded what the owner BET, every fifteen minutes, from
the cutover onwards. Nothing recorded what it PAID. `backfill-settle.yml`
settles, and its window ends at `PRODUCTION_CUTOVER_ISO` STRUCTURALLY -- there
is no input for it and no flag that widens it. So every wager delivered after
the cutover sat downstream with a null result forever, and a postmortem asking
"how did we do" had nothing to read.

`settle-live` is the live path with the opposite bound. These tests pin that
the two cannot become each other, and that the refusals that make settlement
safe are the destination's and the router's, not this command's.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from kalshi_router import cli
from kalshi_router.backfill import BackfillWindow
from kalshi_router.destination import write_settlement_payloads
from kalshi_router.destinations import PROFILES
from kalshi_router.production import PRODUCTION_CUTOVER_ISO
from kalshi_router.sports import Sport

ROOT = Path(__file__).resolve().parents[1]
SEASON_SCRIPT = ROOT / "scripts/settlement_season.py"
WORKFLOW = ROOT / ".github/workflows/settle-wagers.yml"


class FakeSettlement:
    def __init__(self, key, ticker="KXNCAAFSPREAD-26SEP19LSUMISS-LSU10"):
        self.source_bet_key = key
        self.market_ticker = ticker
        self.side = "YES"
        self.settlement_status = "SETTLED"
        self.settled_at = "2026-09-20T02:28:48Z"
        self.result = "WON"
        self.gross_return = 65.6
        self.net_profit_loss = 34.473
        self.refusals = ()


# ------------------------------------------- the two windows cannot meet


def test_the_backfill_window_still_ends_at_the_cutover():
    """The property the one-time catch-up rests on, re-asserted here because
    the obvious way to build a live path would have been to widen it."""
    window = BackfillWindow("2026-09-11T00:00:00Z")
    assert window.end_iso == PRODUCTION_CUTOVER_ISO


def test_settle_live_takes_no_since_argument_at_all():
    """It cannot be pointed backwards. There is nothing to point."""
    parser = cli.build_parser()
    args = parser.parse_args(["settle-live", "--out-dir", "/tmp/x"])
    assert not hasattr(args, "since")
    with pytest.raises(SystemExit):
        parser.parse_args(["settle-live", "--since", "2026-01-01T00:00:00Z"])


def test_settle_live_takes_no_destination_argument_either():
    """Which destinations receive settlements is a property of the profile
    table, not of what a caller passes. MLB's exclusion must not depend on
    somebody remembering to leave it out."""
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["settle-live", "--destination", "MLB"])


# ---------------------------------------- MLB is excluded structurally


def test_mlb_cannot_receive_a_settlement_from_this_router():
    assert PROFILES[Sport.MLB].settlement_importer is None


def test_only_destinations_with_a_settlement_importer_are_emitted_for():
    eligible = {
        sport.value
        for sport, profile in PROFILES.items()
        if profile.settlement_importer is not None
    }
    # MLB settles its own bets and must never receive a second authority.
    assert eligible == {"CFB", "NFL"}


def test_the_workflow_refuses_a_destination_that_settles_its_own():
    """Unreachable given the above, and checked anyway: the one failure mode
    here is writing a second authority on a fact a destination already owns,
    and that must not depend on a property of the caller."""
    text = WORKFLOW.read_text()
    assert "settles_its_own" in text
    assert "refusing to push a second authority" in text


# ------------------------------------------- orphans and the sport reuse


def test_a_settlement_whose_wager_has_no_known_sport_is_refused(tmp_path):
    """A payout with no home. Sending it to a destination chosen by guesswork
    is worse than not sending it."""
    with pytest.raises(ValueError, match="would have to be guessed"):
        write_settlement_payloads([FakeSettlement("kalshi:v1:orphan")], str(tmp_path), {})


def test_the_sport_is_reused_from_the_wager_not_derived_from_the_ticker(tmp_path):
    """`KXNCAAF...` looks like a CFB ticker right up until it is not one. The
    wager was classified on Kalshi's own competition evidence; that answer is
    carried forward rather than re-derived from a prefix."""
    counts = write_settlement_payloads(
        [FakeSettlement("kalshi:v1:aaa")], str(tmp_path), {"kalshi:v1:aaa": "CFB"}
    )
    assert counts == {"CFB": 1}
    payload = json.loads((tmp_path / "CFB-settlements.json").read_text())
    assert payload["settlements"][0]["source_bet_key"] == "kalshi:v1:aaa"

    # ...and the SAME ticker, with the wager classified elsewhere, goes
    # elsewhere. Nothing about the ticker decided it.
    other = tmp_path / "other"
    counts = write_settlement_payloads(
        [FakeSettlement("kalshi:v1:aaa")], str(other), {"kalshi:v1:aaa": "MLB"}
    )
    assert counts == {"MLB": 1}


def test_a_settlement_payload_is_byte_identical_however_the_rows_arrive(tmp_path):
    rows = [FakeSettlement("kalshi:v1:bbb"), FakeSettlement("kalshi:v1:aaa")]
    sports = {"kalshi:v1:aaa": "CFB", "kalshi:v1:bbb": "CFB"}
    first = tmp_path / "1"
    second = tmp_path / "2"
    write_settlement_payloads(list(rows), str(first), sports)
    write_settlement_payloads(list(reversed(rows)), str(second), sports)
    assert (first / "CFB-settlements.json").read_bytes() == (
        second / "CFB-settlements.json"
    ).read_bytes()


# ------------------------------------------------- which season's ledger


def ledger(tmp_path, season, keys):
    root = tmp_path / "wagers"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{season}.jsonl").write_text(
        "\n".join(json.dumps({"source_bet_key": k, "season": season}) for k in keys) + "\n"
    )
    return tmp_path


def settlement_payload(tmp_path, keys):
    path = tmp_path / "CFB-settlements.json"
    path.write_text(json.dumps({"settlements": [{"source_bet_key": k} for k in keys]}))
    return path


def run_season(payload, ledger_dir):
    return subprocess.run(
        [sys.executable, str(SEASON_SCRIPT), "--payload", str(payload),
         "--ledger-dir", str(ledger_dir)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_season_comes_from_the_wager_not_from_the_settlement_date(tmp_path):
    """A bowl game played on 2027-01-02 settles in 2027 and belongs to the
    2026 season. The wager it settles is already filed correctly."""
    dest = ledger(tmp_path / "dest", 2026, ["kalshi:v1:aaa", "kalshi:v1:bbb"])
    payload = settlement_payload(tmp_path, ["kalshi:v1:aaa"])
    result = run_season(payload, dest)
    assert result.returncode == 0
    assert result.stdout.strip() == "2026"


def test_an_orphan_settlement_is_refused_before_anything_is_written(tmp_path):
    dest = ledger(tmp_path / "dest", 2026, ["kalshi:v1:aaa"])
    payload = settlement_payload(tmp_path, ["kalshi:v1:nobody"])
    result = run_season(payload, dest)
    assert result.returncode == 2
    assert "no record of" in result.stderr
    assert not result.stdout.strip()


def test_a_batch_spanning_two_seasons_is_refused_rather_than_misfiled(tmp_path):
    dest = tmp_path / "dest"
    ledger(dest, 2025, ["kalshi:v1:old"])
    ledger(dest, 2026, ["kalshi:v1:new"])
    payload = settlement_payload(tmp_path, ["kalshi:v1:old", "kalshi:v1:new"])
    result = run_season(payload, dest)
    assert result.returncode == 2
    assert "[2025, 2026]" in result.stderr


def test_a_settlement_with_no_source_key_is_refused(tmp_path):
    dest = ledger(tmp_path / "dest", 2026, ["kalshi:v1:aaa"])
    path = tmp_path / "CFB-settlements.json"
    path.write_text(json.dumps({"settlements": [{"market_ticker": "X"}]}))
    result = run_season(path, dest)
    assert result.returncode == 2
    assert "no source key" in result.stderr


def test_a_ledger_with_no_wagers_refuses_rather_than_guessing(tmp_path):
    dest = tmp_path / "empty"
    dest.mkdir()
    payload = settlement_payload(tmp_path, ["kalshi:v1:aaa"])
    result = run_season(payload, dest)
    assert result.returncode == 2
    assert "before the wager it settles" in result.stderr


def test_the_season_script_prints_no_key_or_payout(tmp_path):
    dest = ledger(tmp_path / "dest", 2026, ["kalshi:v1:deadbeef"])
    payload = settlement_payload(tmp_path, ["kalshi:v1:feedface"])
    result = run_season(payload, dest)
    for secret in ("kalshi:v1", "deadbeef", "feedface"):
        assert secret not in result.stdout
        assert secret not in result.stderr


# --------------------------------------------------- the workflow itself


def test_the_settlement_workflow_proves_idempotency_every_run():
    """A market settles once, so a second observation is a duplicate rather
    than a correction. That is a claim about SOMEBODY ELSE'S code, which this
    repository re-measures rather than trusts."""
    text = WORKFLOW.read_text()
    assert "write-tree" in text
    assert "a second identical settlement import changed nothing" in text
    assert "Refusing to push a non-idempotent import" in text


def test_the_settlement_workflow_never_prints_a_row():
    text = WORKFLOW.read_text()
    assert "--stat" in text, "a bare diff would print the added ledger lines"
    assert "upload-artifact" not in text
    assert "report_receipts.py" in text


def test_the_settlement_workflow_runs_the_destinations_own_importer():
    """Writing the ledger file directly would bypass the ORPHAN refusal, which
    is the property that makes a settlement safe."""
    text = WORKFLOW.read_text()
    assert "--importer settlement" in text
    assert "import_argv[@]" in text, "the rendered command must be EXECUTED, not printed"
    # And never a hand-written ledger edit: nothing here opens a ledger file.
    for banned in ("jsonl", ">> \"${work}", "cat >"):
        assert banned not in text, f"the settlement workflow touches the ledger directly ({banned})"


def test_the_settlement_workflow_is_main_only_and_never_cancels_in_flight():
    text = WORKFLOW.read_text()
    assert 'refs/heads/main' in text
    assert "cancel-in-progress: false" in text


def test_the_settlement_workflow_defaults_a_dispatch_to_a_dry_run():
    import yaml

    config = yaml.safe_load(WORKFLOW.read_text())
    events = config[True] if True in config else config["on"]
    assert events["workflow_dispatch"]["inputs"]["dry_run"]["default"] in (True, "true")


def test_the_settlement_workflow_is_scheduled_but_not_every_minute():
    import yaml

    config = yaml.safe_load(WORKFLOW.read_text())
    events = config[True] if True in config else config["on"]
    crons = [entry["cron"] for entry in events["schedule"]]
    assert crons
    for cron in crons:
        minute, hour = cron.split()[0], cron.split()[1]
        assert minute != "*", f"{cron} runs every minute"
        assert "/" not in minute, f"{cron} runs several times an hour"
        # Every four hours or less often.
        assert hour in ("*/4", "*/6", "*/8", "*/12") or hour.isdigit(), cron


def test_every_settlement_orphaned_names_the_likely_cause(tmp_path, capsys):
    """Measured in production: the first live settle-live run reached 41 real
    settled CFB positions and refused all 41, because the wagers were built by
    a DRY RUN delivery that deliberately pushed nothing.

    That is the system working. But "41 orphans" and "the wagers have not been
    delivered yet" are the same output, and only one of them tells an operator
    where to look. The refusal is unchanged; only the diagnosis is added."""
    import subprocess
    import sys as _sys

    # The production shape exactly: a ledger that DOES hold wagers (the
    # cutover rows), and settlements that refer to none of them because the
    # wagers they belong to were built by a dry run and never pushed.
    ledger = tmp_path / "ledger"
    (ledger / "wagers").mkdir(parents=True)
    (ledger / "wagers" / "2026.jsonl").write_text(
        '{"source_bet_key": "an-older-cutover-row", "game_date": "2026-09-06"}\n'
    )
    payload = tmp_path / "settlements.json"
    payload.write_text(
        '{"settlements": [{"source_bet_key": "never-delivered-1"}, '
        '{"source_bet_key": "never-delivered-2"}]}'
    )
    result = subprocess.run(
        [_sys.executable, "scripts/settlement_season.py",
         "--payload", str(payload), "--ledger-dir", str(ledger)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0, "a settlement must never be filed before its wager"
    assert "have not been delivered yet" in result.stderr
    assert "DRY RUN" in result.stderr


def test_a_partial_orphan_batch_does_not_claim_the_wagers_are_undelivered(tmp_path):
    """Some orphans among matched rows is a DIFFERENT problem, and guessing
    'not delivered yet' would send the operator to the wrong place.

    It is also NOT a reason to refuse the batch (the 2026-09-26 incident): the
    matched row proves the season, and the unmatched one goes on to the
    destination's importer, which refuses it per row. See
    tests/test_settlement_unmatched_parents.py."""
    import subprocess
    import sys as _sys

    ledger = tmp_path / "ledger"
    (ledger / "wagers").mkdir(parents=True)
    (ledger / "wagers" / "2026.jsonl").write_text(
        '{"source_bet_key": "delivered-1", "gameDate": "2026-09-26"}\n'
    )
    payload = tmp_path / "settlements.json"
    payload.write_text(
        '{"settlements": [{"source_bet_key": "delivered-1"}, {"source_bet_key": "orphan-1"}]}'
    )
    result = subprocess.run(
        [_sys.executable, "scripts/settlement_season.py",
         "--payload", str(payload), "--ledger-dir", str(ledger)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2026"
    assert "have not been delivered yet" not in result.stderr
    assert "unmatched settlement parents: 1" in result.stderr
