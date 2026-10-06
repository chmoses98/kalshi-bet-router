"""The app-facing router health publisher: it reads public run logs and emits valid contract documents that
carry counts only, in every state the delivery workflow can be in."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "contract"))
spec = importlib.util.spec_from_file_location("publish_router_health", ROOT / "scripts" / "publish_router_health.py")
prh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prh)

from edge_finder_contract.validate import validate  # noqa: E402

NOW = "2026-10-02T15:00:00Z"
STATUS = {"health": "delivered", "production": {"orders_considered": 20, "eligible": 3, "blocked_orders": 0,
                                                 "deferred_orders": 1}, "payload_rows": {"NFL": 2, "MLB": 1},
          "import_batch_id": "kalshi-router-v1"}
LOG = "\n".join([
    "Deliver eligible wagers\tBuild payloads\t2026-10-02T14:50:01.000Z production filter (counts only)",
    "Deliver eligible wagers\tBuild payloads\t2026-10-02T14:50:01.000Z HEALTH=delivered",
    "Deliver eligible wagers\tBuild payloads\t2026-10-02T14:50:01.000Z ROUTER_STATUS_JSON=" + json.dumps(STATUS),
    "Deliver eligible wagers\tDeliver each destination\t2026-10-02T14:52:00.000Z ROUTER_DELIVERY_JSON=" + json.dumps({"sport": "NFL", "verdict": "PASS", "dry_run": False, "note": "merged"}),
    "Deliver eligible wagers\tDeliver each destination\t2026-10-02T14:53:00.000Z ::error::MLB: could not open a pull request (HTTP 403). The wagers are on kalshi-router/MLB but are NOT recorded.",
    "Deliver eligible wagers\tDeliver each destination\t2026-10-02T14:53:00.000Z ROUTER_DELIVERY_JSON=" + json.dumps({"sport": "MLB", "verdict": "FAIL", "dry_run": False, "note": "pull request 403"}),
])
SETTLE_LOG = "\n".join([
    "settle\tSettle\t2026-10-02T12:40:00.000Z settlement payloads written (rows per destination; rows are NOT printed):",
    "settle\tSettle\t2026-10-02T12:40:00.000Z   CFB: 75",
    "settle\tSettle\t2026-10-02T12:40:00.000Z   NFL: 56",
    "settle\tSettle\t2026-10-02T12:40:00.000Z ",
    "settle\tSettle\t2026-10-02T12:41:00.000Z settlements ready: 131",
])
RUN = {"databaseId": 37001, "status": "completed", "conclusion": "failure", "createdAt": "2026-10-02T14:50:00Z",
       "updatedAt": "2026-10-02T14:55:00Z", "url": "https://github.com/chmoses98/kalshi-bet-router/actions/runs/37001"}
SRUN = {"databaseId": 36990, "status": "completed", "conclusion": "success", "createdAt": "2026-10-02T12:40:00Z",
        "updatedAt": "2026-10-02T12:45:00Z", "url": "https://github.com/chmoses98/kalshi-bet-router/actions/runs/36990"}


def test_parse_log_reads_every_machine_line():
    p = prh.parse_log(LOG)
    assert p["health"] == "delivered"
    assert p["status"]["payload_rows"] == {"NFL": 2, "MLB": 1}
    assert [d["sport"] for d in p["deliveries"]] == ["NFL", "MLB"]
    assert p["errors"][0]["sport"] == "MLB" and "403" in p["errors"][0]["message"]
    s = prh.parse_log(SETTLE_LOG)
    assert s["settlement_rows"] == {"CFB": 75, "NFL": 56} and s["settlements_ready"] == 131


def test_documents_validate_and_name_the_failed_destination():
    health, recent = prh.build_documents(deliver_run=RUN, deliver_parsed=prh.parse_log(LOG), settle_run=SRUN,
                                         settle_parsed=prh.parse_log(SETTLE_LOG), previous_recent=[], now=NOW,
                                         commit_sha="abc")
    validate(health, "router_health")
    validate(recent, "recent_deliveries")
    assert health["overall_status"] == "DEGRADED" and health["router_health_state"] == "delivered"
    assert health["by_sport"]["NFL"]["status"] == "DELIVERED" and health["by_sport"]["NFL"]["delivered"] == 2
    assert health["by_sport"]["MLB"]["status"] == "FAILED" and health["by_sport"]["MLB"]["failed"] == 1
    assert health["by_sport"]["NBA"]["status"] == "NO_OP" and health["by_sport"]["NBA"]["routable"] is True
    assert set(health["by_sport"]) == set(prh.SPORTS)
    # CBB (contract 1.2.0) is an app sport only: the router neither classifies nor routes it
    cbb = health["by_sport"]["CBB"]
    assert cbb["status"] == "NOT_ROUTABLE" and cbb["routable"] is False and cbb["classification"] == "UNSUPPORTED"
    assert health["bets_discovered"] == 3 and health["deferred"] == 1 and health["poll_age_seconds"] == 600.0
    statuses = {(i["sport"], i["status"]) for i in recent["items"]}
    assert ("MLB", "FAILED") in statuses and ("NFL", "DELIVERED") in statuses and ("CFB", "SETTLED") in statuses
    mlb = next(i for i in recent["items"] if i["sport"] == "MLB")
    assert mlb["retry_status"] == "WILL_RETRY" and mlb["first_failed_at"] == "2026-10-02T14:50:00Z"


def test_states_healthy_stale_and_unavailable():
    ok_log = LOG.replace("::error::MLB", "::notice::MLB").replace('"verdict": "FAIL"', '"verdict": "PASS"')
    ok_run = dict(RUN, conclusion="success")
    health, _ = prh.build_documents(deliver_run=ok_run, deliver_parsed=prh.parse_log(ok_log), settle_run=None,
                                    settle_parsed=None, previous_recent=[], now=NOW)
    assert health["overall_status"] == "HEALTHY" and health["last_settlement_run"]["run_id"] is None
    stale, _ = prh.build_documents(deliver_run=ok_run, deliver_parsed=prh.parse_log(ok_log), settle_run=None,
                                   settle_parsed=None, previous_recent=[], now="2026-10-02T20:00:00Z")
    assert stale["overall_status"] == "STALE"
    none, _ = prh.build_documents(deliver_run=None, deliver_parsed=None, settle_run=None, settle_parsed=None,
                                  previous_recent=[], now=NOW)
    assert none["overall_status"] == "UNAVAILABLE"
    blocked_log = ok_log.replace("HEALTH=delivered", "HEALTH=blocked")
    blocked, _ = prh.build_documents(deliver_run=ok_run, deliver_parsed=prh.parse_log(blocked_log), settle_run=None,
                                     settle_parsed=None, previous_recent=[], now=NOW)
    assert blocked["overall_status"] == "DEGRADED" and any("BLOCKED" in w for w in blocked["warnings"])


def test_recent_window_is_deduplicated_and_bounded():
    _, first = prh.build_documents(deliver_run=RUN, deliver_parsed=prh.parse_log(LOG), settle_run=None,
                                   settle_parsed=None, previous_recent=[], now=NOW)
    _, again = prh.build_documents(deliver_run=RUN, deliver_parsed=prh.parse_log(LOG), settle_run=None,
                                   settle_parsed=None, previous_recent=first["items"], now=NOW)
    assert again["count"] == first["count"]
    big = [dict(first["items"][0], run_id=str(i), started_at=f"2026-09-{(i % 28) + 1:02d}T00:00:00Z") for i in range(500)]
    _, bounded = prh.build_documents(deliver_run=RUN, deliver_parsed=prh.parse_log(LOG), settle_run=None,
                                     settle_parsed=None, previous_recent=big, now=NOW)
    assert bounded["count"] == prh.RECENT_LIMIT


def test_raw_runner_log_form_is_parsed():
    """The REST job log has `TIMESTAMP line` rows and rewrites workflow commands to `##[error]...`."""
    raw = "\n".join([
        "2026-10-02T23:10:11.1234567Z HEALTH=blocked",
        "2026-10-02T23:10:11.2234567Z ROUTER_STATUS_JSON=" + json.dumps(STATUS),
        "2026-10-02T23:10:16.5936254Z ##[error]MLB: could not open a pull request (HTTP 403). The wagers are on kalshi-router/MLB but are NOT recorded.",
        "2026-10-02T23:10:16.5936254Z ##[warning]BLOCKED -- a wager was placed that this system cannot record.",
    ])
    p = prh.parse_log(raw)
    assert p["health"] == "blocked" and p["status"]["payload_rows"] == {"NFL": 2, "MLB": 1}
    assert p["errors"] == [{"sport": "MLB", "message": "could not open a pull request (HTTP 403). The wagers are on kalshi-router/MLB but are NOT recorded."}]


def test_publisher_scrubs_anything_wager_shaped():
    leaky = LOG + "\n::error::NFL: refused KXNFLSPREAD-26OCT01PITCLE-CLE10 key kalshi:v1:0123456789abcdef stake $74.99"
    health, recent = prh.build_documents(deliver_run=RUN, deliver_parsed=prh.parse_log(leaky), settle_run=None,
                                         settle_parsed=None, previous_recent=[], now=NOW)
    text = json.dumps(health) + json.dumps(recent)
    assert "KXNFLSPREAD" not in text and "0123456789abcdef" not in text and "74.99" not in text
    assert "<ticker>" in text
