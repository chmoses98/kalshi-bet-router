"""The router conductor: delivery and settlement start on their declared cadence, without cron, and without ever
holding a credential, stacking runs, or storming a red workflow.

Evidence it exists for: `deliver-wagers.yml` declares */15 and ran every 3-5 hours 2026-09-23..26; after Thursday's
ATL@GB the owner dispatched deliver-wagers (05:11Z) and settle-wagers (05:03Z, 05:21Z) by hand.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github/workflows/router-conductor.yml"
spec = importlib.util.spec_from_file_location("router_conductor", ROOT / "scripts/router_conductor.py")
RC = importlib.util.module_from_spec(spec)
spec.loader.exec_module(RC)

NOW = datetime(2026, 9, 27, 20, 30, tzinfo=timezone.utc)


def run(minutes_ago, status="completed", conclusion="success", event="schedule"):
    return {"status": status, "conclusion": conclusion, "event": event,
            "createdAt": (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")}


def triggers(doc):
    return doc.get("on") if "on" in doc else doc.get(True)


# ---------------------------------------------------------------- decisions

def test_a_target_with_no_run_on_record_is_dispatched():
    assert RC.decide([], NOW, 20)[0] is True


def test_a_running_or_queued_run_is_never_stacked():
    for status in ("queued", "in_progress", "waiting", "pending", "requested"):
        go, why = RC.decide([run(1, status=status, conclusion=None), run(300)], NOW, 20)
        assert go is False and "already queued or running" in why


def test_the_cadence_counts_from_the_last_start_whoever_started_it():
    """A scheduled or manual run satisfies the cadence too: the conductor adds starts, never duplicates."""
    assert RC.decide([run(10, event="schedule")], NOW, 20)[0] is False
    assert RC.decide([run(10, event="workflow_dispatch")], NOW, 20)[0] is False
    assert RC.decide([run(21)], NOW, 20)[0] is True


def test_a_red_run_is_retried_on_the_cadence_not_in_a_tight_loop():
    """A permanent CONFLICT stays red on every attempt -- as under cron -- but is never hammered."""
    assert RC.decide([run(5, conclusion="failure")], NOW, 20)[0] is False
    assert RC.decide([run(25, conclusion="failure")], NOW, 20)[0] is True


def test_states_are_explicit():
    assert RC.state_of([]) == "WAITING_FOR_SOURCE"
    assert RC.state_of([run(1, status="in_progress", conclusion=None)]) == "RUNNING"
    assert RC.state_of([run(1)]) == "COMPLETE"
    assert RC.state_of([run(1, conclusion="failure")]) == "BLOCKED"
    assert RC.state_of([run(1, conclusion="cancelled")]) == "PARTIAL_COMPLETE"
    assert set(RC.STATES) == {"WAITING_FOR_SOURCE", "RUNNING", "PARTIAL_COMPLETE", "COMPLETE", "BLOCKED"}


def test_a_dispatch_is_exactly_what_the_schedule_does():
    argv = RC.dispatch_argv("deliver-wagers.yml", "main", RC.TARGETS["deliver-wagers.yml"]["inputs"])
    assert argv == ["gh", "workflow", "run", "deliver-wagers.yml", "--ref", "main", "-f", "dry_run=false"]
    assert RC.TARGETS["settle-wagers.yml"]["inputs"] == {"dry_run": "false"}
    assert set(RC.TARGETS) == {"deliver-wagers.yml", "settle-wagers.yml"}


def test_one_pass_dispatches_only_what_is_due_and_an_unreadable_run_list_dispatches_nothing():
    calls = []
    runs = {"deliver-wagers.yml": [run(30)], "settle-wagers.yml": [run(10)]}
    lines = RC.one_pass(NOW, lister=lambda wf: runs[wf], runner=lambda argv: (calls.append(argv) or (True, "ok")))
    assert [c[3] for c in calls] == ["deliver-wagers.yml"]
    assert {ln["workflow"]: ln["decision"] for ln in lines} == {"deliver-wagers.yml": "dispatch",
                                                                "settle-wagers.yml": "wait"}

    def broken(_wf):
        raise RuntimeError("API down")
    calls.clear()
    lines = RC.one_pass(NOW, lister=broken, runner=lambda argv: (calls.append(argv) or (True, "ok")))
    assert calls == [] and all(ln["decision"] == "error" for ln in lines)


def test_the_successor_is_chained_once_near_the_end():
    end = 10_000.0
    assert RC.should_chain(end - 60 * 60, end, False) is False
    assert RC.should_chain(end - 11 * 60, end, False) is True
    assert RC.should_chain(end - 11 * 60, end, True) is False


# ---------------------------------------------------------------- the workflow

def test_the_conductor_holds_no_secret_and_only_the_permission_to_dispatch():
    text = WF.read_text()
    doc = yaml.safe_load(text)
    assert doc["permissions"] == {"contents": "read", "actions": "write"}
    assert "secrets." not in text
    for name in ("DOWNSTREAM_REPO_TOKEN", "KALSHI_PRIVATE_KEY", "KALSHI_API_KEY_ID"):
        assert name not in text


def test_the_conductor_is_main_only_serialised_chained_and_has_an_off_switch():
    doc = yaml.safe_load(WF.read_text())
    assert doc["concurrency"] == {"group": "router-conductor", "cancel-in-progress": False}
    job = doc["jobs"]["conduct"]
    assert "github.ref == 'refs/heads/main'" in job["if"] and "vars.ROUTER_CONDUCTOR != 'off'" in job["if"]
    body = "\n".join(s.get("run") or "" for s in job["steps"])
    assert "refs/heads/main" in body and "--chain-workflow router-conductor.yml" in body
    assert set(triggers(doc)) == {"schedule", "workflow_dispatch"}
    minutes = float(body.split("--minutes ")[1].split()[0])
    assert minutes + RC.CHAIN_LEAD_MIN < job["timeout-minutes"], "the loop must end inside the job limit"
    assert "upload-artifact" not in WF.read_text()


def test_the_dispatched_workflows_accept_the_dispatch_and_keep_their_own_safety():
    """The conductor relies on these properties of its targets; if one moves, this fails here, not in production."""
    for name in ("deliver-wagers.yml", "settle-wagers.yml"):
        doc = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
        text = (ROOT / ".github/workflows" / name).read_text()
        assert "dry_run" in triggers(doc)["workflow_dispatch"]["inputs"]
        assert "github.event_name == 'schedule' && 'false' || inputs.dry_run" in text
        assert doc["concurrency"]["cancel-in-progress"] is False, "a re-dispatch must queue, never cancel"
        assert "refs/heads/main" in text
        # idempotency: a second identical import must change nothing, or the batch does not merge / push
        assert "write-tree" in text and "idempotency" in text
