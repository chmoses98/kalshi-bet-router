#!/usr/bin/env python3
"""Publish the router's app-facing health: ``app/latest/router_health.json`` and ``recent_deliveries.json``.

    python scripts/publish_router_health.py --out-dir app/latest [--repo chmoses98/kalshi-bet-router]

WHERE THE FACTS COME FROM, AND WHY THAT IS SAFE
    This repository is public and persists no wager. The delivery and settlement workflows already print, into
    their PUBLIC run logs, exactly the counts a UI needs: the ``HEALTH=`` token, one ``ROUTER_STATUS_JSON=`` line
    (counts by refusal reason and payload rows per sport), one ``ROUTER_DELIVERY_JSON=`` line per destination, and
    ``::error::<SPORT>: ...`` annotations whose text never names a market, price or key. This script reads those
    logs back through the GitHub API (``gh``, with a token that can only READ actions) and restates them as the
    contract's ``router_health`` and ``recent_deliveries`` documents. By construction it can publish nothing that
    is not already public, and it needs no Kalshi credential and no downstream token.

    Everything that touches the network is in :func:`fetch_runs` / :func:`fetch_log`; everything else is pure and
    tested (tests/test_router_health_publisher.py).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "contract"))

from edge_finder_contract import SCHEMA_VERSION, SPORTS  # noqa: E402
from edge_finder_contract.freshness import DEFAULT_THRESHOLDS, classify  # noqa: E402
from edge_finder_contract.publish import dumps  # noqa: E402
from edge_finder_contract.timeutil import age_seconds, now_utc, parse_ts, to_iso, to_iso_or_none  # noqa: E402
from edge_finder_contract.validate import validate  # noqa: E402
from kalshi_router.destinations import PROFILES  # noqa: E402
from kalshi_router.sports import ROUTABLE_SPORTS  # noqa: E402

DELIVER_WORKFLOW = "deliver-wagers.yml"
SETTLE_WORKFLOW = "settle-wagers.yml"
RECENT_LIMIT = 200
_TICKER = re.compile(r"\bKX[A-Z0-9]{2,}(?:-[A-Z0-9]+)*\b")
_KEY = re.compile(r"kalshi:v\d+:[0-9a-f]{8,}")
_MONEY = re.compile(r"\$\s?\d+(?:\.\d+)?")


def scrub(text: str) -> str:
    """Belt and braces: the workflows never print these, and the publisher never forwards them either."""
    text = _TICKER.sub("<ticker>", text)
    text = _KEY.sub("<source-key>", text)
    text = _MONEY.sub("<amount>", text)
    return text[:240]


# ---------------------------------------------------------------------------------------------- log parsing

def parse_log(text: str) -> dict:
    """Everything the publisher reads out of one run's log. Tolerates the ``job\\tstep\\ttimestamp `` prefixes
    ``gh run view --log`` adds, and the absence of any line."""
    out: dict = {"health": None, "status": None, "deliveries": [], "errors": [], "settlement_rows": {},
                 "settlements_ready": None, "reconciled": {}, "settlement_parents": {}}
    in_settlement_block = False
    for raw in text.splitlines():
        line = raw.split("\t")[-1] if "\t" in raw else raw
        line = re.sub(r"^\d{4}-\d{2}-\d{2}T[0-9:.]+Z\s+", "", line.strip())
        if line.startswith("HEALTH="):
            out["health"] = line[len("HEALTH="):].strip()
        elif line.startswith("ROUTER_STATUS_JSON="):
            try:
                out["status"] = json.loads(line[len("ROUTER_STATUS_JSON="):])
            except json.JSONDecodeError:
                out["errors"].append({"sport": None, "message": "unparseable ROUTER_STATUS_JSON line"})
        elif line.startswith("ROUTER_DELIVERY_JSON="):
            try:
                out["deliveries"].append(json.loads(line[len("ROUTER_DELIVERY_JSON="):]))
            except json.JSONDecodeError:
                out["errors"].append({"sport": None, "message": "unparseable ROUTER_DELIVERY_JSON line"})
        elif line.startswith("ROUTER_RECONCILE_JSON="):
            # One per destination and kind (scripts/reconcile_delivery.py): where every eligible row IS.
            try:
                row = json.loads(line[len("ROUTER_RECONCILE_JSON="):])
                if row.get("sport") in SPORTS:
                    out["reconciled"][(row["sport"], row.get("kind") or "wagers")] = row
            except (json.JSONDecodeError, KeyError, AttributeError):
                out["errors"].append({"sport": None, "message": "unparseable ROUTER_RECONCILE_JSON line"})
        elif line.startswith("ROUTER_SETTLEMENT_PARENTS_JSON="):
            try:
                row = json.loads(line[len("ROUTER_SETTLEMENT_PARENTS_JSON="):])
                if row.get("sport") in SPORTS:
                    out["settlement_parents"][row["sport"]] = row
            except (json.JSONDecodeError, AttributeError):
                out["errors"].append({"sport": None, "message": "unparseable ROUTER_SETTLEMENT_PARENTS_JSON line"})
        elif line.startswith("::error::") or line.startswith("##[error]"):
            # A workflow command reaches the raw job log rewritten by the runner (`##[error]...`); the
            # `::error::` form survives only in the step's own echo. Both mean the same thing.
            body = line.split("]", 1)[1] if line.startswith("##[") else line[len("::error::"):]
            m = re.match(r"([A-Z]+):\s*(.*)", body)
            sport = m.group(1) if m and m.group(1) in SPORTS else None
            out["errors"].append({"sport": sport, "message": scrub(m.group(2) if m else body)})
        elif line.startswith("settlement payloads written"):
            in_settlement_block = True
        elif in_settlement_block:
            m = re.match(r"([A-Z]+):\s*(\d+)$", line)
            if m and m.group(1) in SPORTS:
                out["settlement_rows"][m.group(1)] = int(m.group(2))
            elif line and not line.startswith("none"):
                in_settlement_block = False
        if line.startswith("settlements ready:"):
            try:
                out["settlements_ready"] = int(line.split(":", 1)[1])
            except ValueError:
                pass
    return out


# ------------------------------------------------------------------------------------------------- building

def _run_ref(run: dict | None, health_state: str | None = None) -> dict:
    if not run:
        return {"run_id": None, "url": None, "started_at": None, "concluded_at": None, "conclusion": None,
                "health_state": health_state}
    return {"run_id": str(run.get("databaseId") or run.get("id") or ""), "url": run.get("url"),
            "started_at": to_iso_or_none(run.get("createdAt")), "concluded_at": to_iso_or_none(run.get("updatedAt")),
            "conclusion": run.get("conclusion") or run.get("status"), "health_state": health_state}


def _settlement_status(sport: str, settle_parsed: dict | None, auto_merge: bool | None) -> dict | None:
    """One sport's outcome in the last SETTLEMENT run, or None when that run did not consider it.

    FAILED only for a genuine failure (an ``::error::`` for the sport: an importer refusal, a validator or gate
    refusal, an unaccounted row). WAITING_FOR_PARENT_WAGER when rows were withheld because their wager is on the
    open, unmerged proposal -- a sequencing state, not a failure. AWAITING_MANUAL_MERGE when settlements were
    proposed to an observation-period destination and wait for a person."""
    if not settle_parsed:
        return None
    rec = (settle_parsed.get("reconciled") or {}).get((sport, "settlements"))
    rows = (settle_parsed.get("settlement_rows") or {}).get(sport)
    if rec is None and rows is None:
        return None
    failed = any(e.get("sport") == sport for e in settle_parsed.get("errors", []))
    rec = rec or {}
    waiting = rec.get("waiting_for_parent_wager") or 0
    proposed = rec.get("proposed_not_merged") or 0
    if failed:
        status = "FAILED"
    elif not rec:
        status = "UNKNOWN"
    elif waiting:
        status = "WAITING_FOR_PARENT_WAGER"
    elif proposed and auto_merge is False:
        status = "AWAITING_MANUAL_MERGE"
    elif rows:
        status = "SETTLED"
    else:
        status = "NO_OP"
    return {"status": status, "rows": rows if rows is not None else rec.get("payload_rows"),
            "on_ledger": rec.get("on_ledger"), "proposed_not_merged": rec.get("proposed_not_merged"),
            "waiting_for_parent_wager": rec.get("waiting_for_parent_wager"), "refused": rec.get("refused"),
            "unaccounted": rec.get("unaccounted")}


def _sport_status(sport: str, parsed: dict, dry_run_default: bool, settle_parsed: dict | None = None) -> dict:
    prof = PROFILES.get(next((s for s in PROFILES if s.value == sport), None))
    rows = (parsed.get("status") or {}).get("payload_rows", {}).get(sport)
    delivery = next((d for d in parsed.get("deliveries", []) if d.get("sport") == sport), None)
    errors = [e for e in parsed.get("errors", []) if e.get("sport") == sport]
    rec = (parsed.get("reconciled") or {}).get((sport, "wagers")) or {}
    if prof is None:
        status = "NOT_ROUTABLE"
    elif delivery is not None:
        if delivery.get("verdict") == "FAIL":
            status = "FAILED"
        elif delivery.get("dry_run") or dry_run_default:
            status = "DRY_RUN"
        elif (rec.get("proposed_not_merged") or 0) > 0 and prof.auto_merge is False:
            # Delivered to the router's open proposal, every gate condition passed, held for a person by the
            # destination's observation period. Pending by design -- not a failure.
            status = "AWAITING_MANUAL_MERGE"
        else:
            status = "DELIVERED"
    elif errors:
        status = "FAILED"
    elif rows:
        status = "UNKNOWN"
    else:
        status = "NO_OP"
    delivered = rows if status in ("DELIVERED", "AWAITING_MANUAL_MERGE") else (0 if status in ("NO_OP",) else None)
    return {
        "routable": prof is not None, "classification": "SUPPORTED" if sport in {s.value for s in ROUTABLE_SPORTS} else "UNSUPPORTED",
        "destination_repo": prof.repo if prof else None, "ledger_branch": prof.ledger_branch if prof else None,
        "auto_merge": prof.auto_merge if prof else None,
        "eligible": rows, "delivered": delivered, "failed": (rows or 1) if status == "FAILED" else (0 if status != "UNKNOWN" else None),
        "status": status,
        "last_error_type": (errors[0]["message"].split(" ")[0].strip(":;,.") if errors else None),
        "on_ledger": rec.get("on_ledger"), "proposed_not_merged": rec.get("proposed_not_merged"),
        "settlement": _settlement_status(sport, settle_parsed, prof.auto_merge if prof else None),
    }


def build_documents(*, deliver_run: dict | None, deliver_parsed: dict | None, settle_run: dict | None,
                    settle_parsed: dict | None, previous_recent: list[dict] | None, now: object | None = None,
                    commit_sha: str | None = None, run_id: str = "run_router00000000000000") -> tuple[dict, dict]:
    now = now or now_utc()
    deliver_parsed = deliver_parsed or {}
    settle_parsed = settle_parsed or {}
    th = {"router": DEFAULT_THRESHOLDS["router"], "settlement": DEFAULT_THRESHOLDS["settlement"]}
    last_poll = to_iso_or_none((deliver_run or {}).get("createdAt"))
    poll_age = age_seconds(last_poll, now) if last_poll else None
    freshness = classify(poll_age, th["router"])
    health_state = deliver_parsed.get("health") or "unknown"
    production = (deliver_parsed.get("status") or {}).get("production", {})
    dry_run = any(d.get("dry_run") for d in deliver_parsed.get("deliveries", []))
    by_sport = {s: _sport_status(s, deliver_parsed, dry_run, settle_parsed if settle_run else None)
                for s in SPORTS}
    warnings: list[str] = []
    errors: list[str] = [f"{e['sport'] or 'router'}: {e['message']}" for e in deliver_parsed.get("errors", [])]
    errors += [f"settle {e['sport'] or 'router'}: {e['message']}" for e in settle_parsed.get("errors", [])]
    conclusion = (deliver_run or {}).get("conclusion")
    settle_conclusion = (settle_run or {}).get("conclusion")
    # A GENUINE settlement failure degrades the router: a red settlement run, or a sport whose settlement the
    # destination refused. A settlement WAITING_FOR_PARENT_WAGER does not -- its run is green by construction.
    settlement_failed = settle_conclusion not in ("success", None) or any(
        (v.get("settlement") or {}).get("status") == "FAILED" for v in by_sport.values())
    if deliver_run is None:
        overall = "UNAVAILABLE"
        warnings.append("no delivery run found")
    elif freshness == "STALE":
        overall = "STALE"
    elif (conclusion not in ("success", None) or health_state == "blocked" or settlement_failed
          or any(v["status"] == "FAILED" for v in by_sport.values())):
        overall = "DEGRADED"
    elif health_state == "unknown":
        overall = "DEGRADED"
        warnings.append("the latest delivery run printed no HEALTH token")
    else:
        overall = "HEALTHY"
    if health_state == "blocked":
        warnings.append("router health is BLOCKED: a wager exists that no destination can record; see the refusal counts")
    if settlement_failed:
        warnings.append("settlement: a destination refused or could not record a settlement; see errors")
    for s, v in by_sport.items():
        if not v["routable"]:
            continue
        if v["status"] == "AWAITING_MANUAL_MERGE":
            warnings.append(f"{s}: {v['proposed_not_merged']} wager(s) delivered to the open proposal, awaiting a "
                            f"person's merge (observation period, auto_merge off) -- pending by design, not a failure")
        elif v["auto_merge"] is False:
            warnings.append(f"{s}: observation period (auto_merge off); deliveries open a pull request a person merges")
        settled = v.get("settlement") or {}
        if settled.get("waiting_for_parent_wager"):
            warnings.append(f"{s}: {settled['waiting_for_parent_wager']} settlement(s) WAITING_FOR_PARENT_WAGER -- "
                            "their wagers are on the open, unmerged proposal; withheld, re-offered every run, "
                            "imported once the wager merges")
    eligible = production.get("eligible")
    awaiting_merge = sum(v.get("proposed_not_merged") or 0 for v in by_sport.values()
                         if v["status"] == "AWAITING_MANUAL_MERGE")
    waiting_parent = sum((v.get("settlement") or {}).get("waiting_for_parent_wager") or 0
                         for v in by_sport.values())
    health = {
        "schema_version": SCHEMA_VERSION, "kind": "router_health", "sport": "ALL", "run_id": run_id,
        "generated_at": to_iso(now),
        "overall_status": overall,
        "router_health_state": health_state if health_state in ("healthy_no_op", "delivered", "not_routable", "deferred", "blocked") else "unknown",
        "last_poll_at": last_poll, "poll_age_seconds": None if poll_age is None else round(poll_age, 1),
        "last_delivery_run": _run_ref(deliver_run, health_state),
        "last_settlement_run": _run_ref(settle_run, None),
        "bets_discovered": eligible, "delivered": sum((v["delivered"] or 0) for v in by_sport.values()) if eligible is not None else None,
        "failed": sum((v["failed"] or 0) for v in by_sport.values()) if deliver_run else None,
        "blocked": production.get("blocked_orders"), "deferred": production.get("deferred_orders"),
        "awaiting_manual_merge": awaiting_merge if deliver_run else None,
        "waiting_for_parent_wager": waiting_parent if settle_run else None,
        "by_sport": by_sport,
        "thresholds": {k: v.as_dict() for k, v in th.items()},
        "warnings": warnings, "errors": errors, "commit_sha": commit_sha,
    }
    validate(health, "router_health")

    items = list(previous_recent or [])
    seen = {(i.get("run_id"), i.get("sport"), i.get("status")) for i in items}
    for sport, v in by_sport.items():
        if deliver_run and (v["eligible"] or v["status"] == "FAILED"):
            rec = {"run_id": str(deliver_run.get("databaseId")), "run_url": deliver_run.get("url"),
                   "started_at": last_poll, "sport": sport, "destination": v["destination_repo"],
                   "status": (v["status"] if v["status"] in ("DELIVERED", "FAILED", "DRY_RUN", "NO_OP", "PARTIAL",
                                                             "AWAITING_MANUAL_MERGE") else "NO_OP"),
                   "rows": v["eligible"], "attempt": None, "error_type": v["last_error_type"],
                   "error_message": next((e["message"] for e in deliver_parsed.get("errors", []) if e.get("sport") == sport), None),
                   "wager_ids": [], "first_failed_at": last_poll if v["status"] == "FAILED" else None,
                   "last_attempt_at": last_poll,
                   "retry_status": ("WILL_RETRY" if v["status"] == "FAILED" else
                                    "RESOLVED" if v["status"] == "DELIVERED" else "NOT_APPLICABLE")}
            key = (rec["run_id"], rec["sport"], rec["status"])
            if key not in seen:
                items.append(rec)
                seen.add(key)
    if settle_run:
        s_started = to_iso_or_none(settle_run.get("createdAt"))
        for sport, rows in settle_parsed.get("settlement_rows", {}).items():
            failed = any(e.get("sport") == sport for e in settle_parsed.get("errors", []))
            waiting = ((by_sport.get(sport) or {}).get("settlement") or {}).get("status") == "WAITING_FOR_PARENT_WAGER"
            rec = {"run_id": str(settle_run.get("databaseId")), "run_url": settle_run.get("url"), "started_at": s_started,
                   "sport": sport, "destination": by_sport.get(sport, {}).get("destination_repo"),
                   "status": "FAILED" if failed else ("WAITING_FOR_PARENT_WAGER" if waiting else "SETTLED"),
                   "rows": rows, "attempt": None,
                   "error_type": None, "error_message": next((e["message"] for e in settle_parsed.get("errors", []) if e.get("sport") == sport), None),
                   "wager_ids": [], "first_failed_at": s_started if failed else None, "last_attempt_at": s_started,
                   "retry_status": "WILL_RETRY" if (failed or waiting) else "RESOLVED"}
            key = (rec["run_id"], rec["sport"], rec["status"])
            if key not in seen:
                items.append(rec)
                seen.add(key)
    items.sort(key=lambda i: (i.get("started_at") or "", i.get("sport") or ""), reverse=True)
    items = items[:RECENT_LIMIT]
    recent = {"schema_version": SCHEMA_VERSION, "kind": "recent_deliveries", "sport": "ALL", "run_id": run_id,
              "generated_at": to_iso(now), "count": len(items), "items": items}
    validate(recent, "recent_deliveries")
    return health, recent


# --------------------------------------------------------------------------------------------------- fetching

def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def fetch_runs(repo: str, workflow: str, limit: int = 3) -> list[dict]:
    text = gh("run", "list", "--repo", repo, "--workflow", workflow, "--branch", "main", "--limit", str(limit),
              "--json", "databaseId,status,conclusion,createdAt,updatedAt,url,event,headSha")
    return json.loads(text or "[]")


def fetch_log(repo: str, run_id: str) -> str:
    """Every job's raw log for one run, concatenated. Per job through the REST API first (one plain-text
    document per job, which `gh api` follows through the storage redirect); `gh run view --log` as the
    fallback. An unreadable log yields an empty string, which the builder reports as an unknown state
    rather than inventing one."""
    texts: list[str] = []
    try:
        jobs = json.loads(gh("api", f"repos/{repo}/actions/runs/{run_id}/jobs", "--jq", "[.jobs[].id]"))
        for job_id in jobs:
            try:
                texts.append(gh("api", f"repos/{repo}/actions/jobs/{job_id}/logs"))
            except subprocess.CalledProcessError as exc:
                texts.append(exc.stdout or "")
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        pass
    if any(t.strip() for t in texts):
        return "\n".join(texts)
    try:
        return gh("run", "view", "--repo", repo, str(run_id), "--log")
    except subprocess.CalledProcessError as exc:
        return exc.stdout or ""


def latest_completed(runs: list[dict]) -> dict | None:
    for run in runs:
        if run.get("status") == "completed":
            return run
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "chmoses98/kalshi-bet-router"))
    ap.add_argument("--commit-sha", default=os.environ.get("GITHUB_SHA"))
    a = ap.parse_args(argv)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    deliver_runs = fetch_runs(a.repo, DELIVER_WORKFLOW)
    settle_runs = fetch_runs(a.repo, SETTLE_WORKFLOW)
    deliver_run = latest_completed(deliver_runs)
    settle_run = latest_completed(settle_runs)
    deliver_parsed = parse_log(fetch_log(a.repo, deliver_run["databaseId"])) if deliver_run else None
    settle_parsed = parse_log(fetch_log(a.repo, settle_run["databaseId"])) if settle_run else None
    previous = []
    prev_path = out / "recent_deliveries.json"
    if prev_path.exists():
        try:
            previous = json.loads(prev_path.read_text(encoding="utf-8")).get("items", [])
        except (json.JSONDecodeError, AttributeError):
            previous = []
    now = now_utc()
    run_id = "run_" + re.sub(r"[^0-9a-f]", "", now.strftime("%Y%m%d%H%M%S")).ljust(20, "0")[:20]
    health, recent = build_documents(deliver_run=deliver_run, deliver_parsed=deliver_parsed, settle_run=settle_run,
                                     settle_parsed=settle_parsed, previous_recent=previous, now=now,
                                     commit_sha=a.commit_sha, run_id=run_id)
    (out / "router_health.json").write_text(dumps(health, compact=False), encoding="utf-8")
    (out / "recent_deliveries.json").write_text(dumps(recent, compact=False), encoding="utf-8")
    print(f"router health: {health['overall_status']} (router state {health['router_health_state']}); "
          f"recent deliveries: {recent['count']}")
    for sport, v in health["by_sport"].items():
        settled = v.get("settlement") or {}
        print(f"  {sport}: {v['status']} eligible={v['eligible']} delivered={v['delivered']} "
              f"on_ledger={v.get('on_ledger')} proposed_not_merged={v.get('proposed_not_merged')} "
              f"settlement={settled.get('status')} waiting_for_parent={settled.get('waiting_for_parent_wager')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
