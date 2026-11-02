"""Machine-readable status of the 2026-11-02 postmortem deliverables, and a one-screen dashboard.

The report is DATA the owner/CI fills in per repository; this module only validates its shape and renders it, so a
deliverable can never read "done" without evidence (a PR URL or a test reference) and an unresolved item always
names its blocker.  It holds no wager, ticker, price or balance -- statuses and links only -- so it is public-safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

SCHEMA_VERSION = "postmortem_report.v1"
STATUSES = ("DONE", "PARTIAL", "BLOCKED", "NOT_STARTED", "NEEDS_OWNER_DECISION")

DELIVERABLES = {
    1: "Immutable per-contract reconciliation (recommendation, model snapshot, fills/fees, ontology, settlement, CLV)",
    2: "Stale-input / after-kickoff guards; price provenance; fee-inclusive break-even; calibrated fair value + bet-up-to",
    3: "Game-thesis correlation aggregation; >5U/game and >15U/day flags",
    4: "CFB/NFL exact-key-number distributions (1Q/1H/TT/total) with opponent-adjusted pace/EPA",
    5: "Point-in-time backtest of market families and expressions vs price-only baselines and CLV",
    6: "NBA router PR #48 merge / settlement parent linkage repaired safely",
    7: "Regression tests, machine-readable report, dashboard summary, focused PRs",
}


@dataclass
class Item:
    deliverable: int
    repo: str
    status: str
    evidence: list[str] = field(default_factory=list)  # PR URLs / test ids
    blocker: str | None = None


def validate(items: Iterable[Item]) -> list[str]:
    problems: list[str] = []
    for item in items:
        tag = f"{item.repo}#D{item.deliverable}"
        if item.deliverable not in DELIVERABLES:
            problems.append(f"{tag}: unknown deliverable")
        if item.status not in STATUSES:
            problems.append(f"{tag}: unknown status {item.status!r}")
        if item.status == "DONE" and not item.evidence:
            problems.append(f"{tag}: DONE without evidence")
        if item.status in ("PARTIAL", "BLOCKED", "NEEDS_OWNER_DECISION") and not item.blocker:
            problems.append(f"{tag}: {item.status} without a stated blocker/remaining work")
    return problems


def to_report(items: list[Item]) -> dict[str, Any]:
    problems = validate(items)
    return {
        "schema_version": SCHEMA_VERSION,
        "valid": not problems,
        "problems": problems,
        "items": [{"deliverable": i.deliverable, "title": DELIVERABLES.get(i.deliverable), "repo": i.repo,
                   "status": i.status, "evidence": i.evidence, "blocker": i.blocker} for i in items],
        "counts": {s: sum(1 for i in items if i.status == s) for s in STATUSES},
    }


def render_dashboard(report: dict[str, Any]) -> str:
    lines = ["| # | Deliverable | Repo | Status | Evidence / blocker |", "|---|---|---|---|---|"]
    for it in sorted(report["items"], key=lambda x: (x["deliverable"], x["repo"])):
        note = ", ".join(it["evidence"]) if it["evidence"] else (it["blocker"] or "")
        if it["status"] != "DONE" and it["blocker"]:
            note = f"{note} — {it['blocker']}" if note else it["blocker"]
        lines.append(f"| {it['deliverable']} | {it['title']} | {it['repo']} | {it['status']} | {note} |")
    return "\n".join(lines)
