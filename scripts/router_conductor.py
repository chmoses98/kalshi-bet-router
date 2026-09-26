#!/usr/bin/env python3
"""ROUTER CONDUCTOR: start delivery and settlement on their declared cadence instead of hoping cron fires.

    python scripts/router_conductor.py --minutes 330 --interval 120 --chain-workflow router-conductor.yml

WHY THIS EXISTS
    `deliver-wagers.yml` declares `*/15` and `settle-wagers.yml` declares `40 */4`. GitHub delivered neither:
    between 2026-09-23 and 2026-09-26 the */15 delivery ran every 3-5 HOURS (e.g. 00:44Z then 05:29Z on the night of
    Thursday's ATL@GB), and the owner dispatched deliver-wagers (05:11Z) and settle-wagers (05:03Z, 05:21Z) by hand
    after the game. The workflows were correct; they were not started. nfl-edge-finder's horizon conductor fixed
    the same scheduler for its horizons; this is that pattern for the router.

WHAT IT DOES
    Every `--interval` seconds, for each target: if no run of that workflow is queued or running, and the last run
    STARTED at least the target's cadence ago, dispatch one -- `gh workflow run <wf> --ref main -f dry_run=false`,
    which is exactly what the declared schedule does (a scheduled run is `dry_run=false` by the workflows' own
    `github.event_name == 'schedule' && 'false'` rule). ~12 minutes before its own end it dispatches its
    successor, which waits as the concurrency group's one pending run; the hourly cron only restarts a lost chain.

WHAT IT NEVER DOES
    It holds no secret and builds, imports, pushes and merges nothing: the dispatched workflow does, through the
    same gates, importers, idempotency re-run, merge gate and reconciliation as a scheduled run. It never stacks a
    second run behind a running one (that would only make GitHub cancel the older pending run), and a red run is
    never retried in a tight loop: the next attempt waits the target's cadence, and a permanent refusal (a CONFLICT
    row) simply stays red on every attempt, as it would under cron.

STATES (one JSON line per target per pass, and a table in the job summary)
    RUNNING             a run is queued or in progress
    COMPLETE            the last run succeeded
    BLOCKED             the last run failed -- red stays red; it needs a person or a new fact, and the next
                        cadence tick tries again (deliveries for the other destinations still land in that run)
    WAITING_FOR_SOURCE  no run recorded yet
    PARTIAL_COMPLETE    the last run was cancelled or timed out mid-way (the destinations' idempotent importers
                        make the next run finish the rest)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

#: Minimum minutes between the STARTS of two runs of each target. Delivery matches its declared */15 plus
#: headroom over the measured 13-16 minute run; settlement is hourly rather than the declared four-hourly because
#: Sunday's games end at ~20:15Z, ~23:30Z and ~03:30Z and a settlement should not wait up to four hours (Thursday's
#: were dispatched by hand). Both are dispatch-only; the workflows' own concurrency groups serialise them.
TARGETS = {
    "deliver-wagers.yml": {"cadence_min": 20.0, "inputs": {"dry_run": "false"}},
    "settle-wagers.yml": {"cadence_min": 60.0, "inputs": {"dry_run": "false"}},
}
CHAIN_LEAD_MIN = 12.0
ACTIVE = ("queued", "in_progress", "waiting", "pending", "requested")

STATES = ("WAITING_FOR_SOURCE", "RUNNING", "PARTIAL_COMPLETE", "COMPLETE", "BLOCKED")


def _dt(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def state_of(runs: list) -> str:
    """Pure: the target's state from its most recent runs (newest first, `gh run list --json` shape)."""
    if not runs:
        return "WAITING_FOR_SOURCE"
    if any((r.get("status") or "") in ACTIVE for r in runs):
        return "RUNNING"
    c = (runs[0].get("conclusion") or "").lower()
    if c == "success":
        return "COMPLETE"
    if c in ("cancelled", "timed_out", "stale"):
        return "PARTIAL_COMPLETE"
    return "BLOCKED"


def decide(runs: list, now: datetime, cadence_min: float) -> tuple[bool, str]:
    """Pure: dispatch this target now? `runs` newest first."""
    active = [r for r in runs if (r.get("status") or "") in ACTIVE]
    if active:
        return False, f"{len(active)} run(s) already queued or running"
    last = max((_dt(r.get("createdAt")) for r in runs if _dt(r.get("createdAt"))), default=None)
    if last is not None:
        age = (now - last).total_seconds() / 60.0
        if age < cadence_min:
            return False, f"last run started {age:.0f} min ago; cadence {cadence_min:.0f} min"
    return True, "due" if last is not None else "due (no run on record)"


def should_chain(now_epoch: float, end_epoch: float, already: bool, lead_min: float = CHAIN_LEAD_MIN) -> bool:
    return (not already) and (end_epoch - now_epoch) <= lead_min * 60


def dispatch_argv(workflow: str, ref: str, inputs: dict) -> list:
    argv = ["gh", "workflow", "run", workflow, "--ref", ref]
    for k, v in sorted((inputs or {}).items()):
        argv += ["-f", f"{k}={v}"]
    return argv


def recent_runs(workflow: str, limit: int = 5) -> list:
    r = subprocess.run(["gh", "run", "list", "--workflow", workflow, "--limit", str(limit),
                        "--json", "status,conclusion,createdAt,event,databaseId"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:200])
    return json.loads(r.stdout or "[]")


def run_cmd(argv: list) -> tuple[bool, str]:
    r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    return r.returncode == 0, (r.stderr or r.stdout).strip()[:300]


def one_pass(now: datetime, *, ref="main", dry_run=False, lister=None, runner=None, targets=None) -> list:
    lister = lister or recent_runs
    runner = runner or run_cmd
    lines = []
    for wf, cfg in (targets or TARGETS).items():
        line = {"at": now.isoformat(), "workflow": wf}
        try:
            runs = lister(wf)
        except Exception as exc:  # noqa: BLE001 -- an unreadable run list is a skipped pass, never a dispatch
            line.update(decision="error", reason=f"cannot list runs: {exc}"[:300], state="UNKNOWN")
            lines.append(line)
            continue
        go, why = decide(runs, now, cfg["cadence_min"])
        line.update(state=state_of(runs), decision="dispatch" if go else "wait", reason=why)
        if go:
            ok, msg = (True, "dry run") if dry_run else runner(dispatch_argv(wf, ref, cfg.get("inputs")))
            line.update(dispatched=ok, message=msg)
        lines.append(line)
    return lines


def summary_table(last_lines: dict) -> str:
    out = ["## Router conductor", "", "| workflow | state | last decision |", "|---|---|---|"]
    for wf, ln in sorted(last_lines.items()):
        out.append(f"| {wf} | {ln.get('state')} | {ln.get('decision')}: {ln.get('reason')} |")
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=330.0)
    ap.add_argument("--interval", type=float, default=120.0)
    ap.add_argument("--ref", default="main")
    ap.add_argument("--chain-workflow", default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    end = time.time() + a.minutes * 60
    chained = False
    last: dict = {}
    while time.time() < end:
        t0 = time.time()
        now = datetime.now(timezone.utc)
        try:
            for line in one_pass(now, ref=a.ref, dry_run=a.dry_run):
                last[line["workflow"]] = line
                print(json.dumps(line), flush=True)
        except Exception as exc:  # noqa: BLE001 -- one bad pass must not end the conductor
            print(json.dumps({"at": now.isoformat(), "decision": "error", "reason": str(exc)[:200]}), flush=True)
        if a.chain_workflow and should_chain(time.time(), end, chained):
            ok, msg = (True, "dry run") if a.dry_run else run_cmd(dispatch_argv(a.chain_workflow, a.ref, {}))
            print(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "decision": "chain",
                              "workflow": a.chain_workflow, "dispatched": ok, "message": msg}), flush=True)
            chained = ok                            # refused -> retried next pass; the hourly cron is the backstop
        time.sleep(max(5.0, min(a.interval - (time.time() - t0), end - time.time())))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(summary_table(last))
    return 0


if __name__ == "__main__":
    sys.exit(main())
