"""Whether each settled row's PARENT WAGER is canonical, waiting on a valid proposal, or nowhere.

THE STATE THIS NAMES
--------------------
A wager goes *built -> delivered onto the router's proposal -> merged onto the canonical ledger*. A destination
held for OBSERVATION (``DestinationProfile.auto_merge = False``: NHL and SOCCER on 2026-10-08) stops at the middle
step on purpose -- the gate passes, the pull request is open, a person merges it. Its markets settle meanwhile.

Until now every such settlement went to the destination's settlement importer, which correctly refused it as an
orphan (no parent wager on the canonical ledger). The importer exited non-zero, the run went red, the app-facing
health read FAILED and DEGRADED -- for 19 NHL and 9 SOCCER settlements whose wagers were imported cleanly, are on a
pull request every gate condition passes, and are absent from the ledger only because a person has not merged it
yet. That is a sequencing state, and reporting it as a failure trains an operator to ignore the red that matters.

So before any importer runs, each settled row is put in exactly one of three states:

``CANONICAL_PARENT``
    its wager's source key is on the canonical WAGER ledger. Imported exactly as before.

``WAITING_FOR_PARENT_WAGER``
    its wager is NOT canonical, but it is on the router's own wager proposal for this destination, and that
    proposal is VALID: an open, non-draft pull request from the router's branch, in the destination's own
    repository, into the ledger branch, whose head is the commit just read -- and exactly one wager record there
    carries this source key, with the SAME market ticker and side as the settlement. The row is WITHHELD from the
    importer: not written, not recorded as settled, not refused, not dropped. Every run rebuilds the payload from
    the exchange, so it is re-offered on the next run, and the first run after the wager merges imports it
    normally (the importer's own idempotency makes that exactly once).

``NO_VALID_PARENT``
    anything else -- no source key, no proposal branch, no open pull request, a draft, a pull request from or to
    the wrong place, a branch that moved, the parent absent from the proposal, two parents, or a parent whose
    ticker or side disagrees. The row goes to the importer exactly as before, which refuses it, and the run is
    red. FAIL CLOSED: nothing here can make a settlement land, and nothing here can make a refusal quiet unless
    every fact above is proven.

Canonical parent-child integrity is untouched: the destination's importer still writes a settlement only for a
wager on its ledger. This module only decides which rows are worth asking it about this run.

Pure. The git and GitHub reads are in ``scripts/settlement_parents.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CANONICAL_PARENT = "CANONICAL_PARENT"
WAITING_FOR_PARENT_WAGER = "WAITING_FOR_PARENT_WAGER"
NO_VALID_PARENT = "NO_VALID_PARENT"

KEY_FIELDS = ("source_bet_key", "sourceBetKey")
TICKER_FIELDS = ("market_ticker", "marketTicker")


def _first(row: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


@dataclass(frozen=True)
class Proposal:
    """The router's wager proposal for one destination, as evidenced by git and the GitHub API.

    ``pull`` is the pull request object GitHub returned for the router's branch (or None when there is none);
    ``fetched_sha`` is the commit the branch pointed at when its wager records were read; ``records`` are those
    records. Holds wager rows, so it is never printed.
    """

    branch: str
    fetched_sha: str | None
    pull: dict[str, Any] | None
    records: tuple[dict[str, Any], ...] = ()
    #: Set when the pull request could not be read at all (no token, an API error). Never treated as valid.
    unverifiable: str | None = None


def proposal_problem(proposal: Proposal | None, *, repo: str, ledger_branch: str) -> str | None:
    """None when the proposal is valid for deferring a settlement on; otherwise the reason it is not."""
    if proposal is None or not proposal.fetched_sha:
        return "no_proposal_branch"
    if proposal.unverifiable:
        return "pull_request_unverifiable"
    pull = proposal.pull
    if not pull:
        return "no_open_pull_request"
    if pull.get("state") != "open":
        return "pull_request_not_open"
    if pull.get("draft"):
        return "pull_request_is_draft"
    head = pull.get("head") or {}
    if head.get("ref") != proposal.branch:
        return "pull_request_head_is_not_the_router_branch"
    if ((head.get("repo") or {}).get("full_name") or "").lower() != repo.lower():
        return "pull_request_from_another_repository"
    if (pull.get("base") or {}).get("ref") != ledger_branch:
        return "pull_request_targets_another_branch"
    if head.get("sha") != proposal.fetched_sha:
        return "proposal_moved_since_it_was_read"
    return None


@dataclass
class Partition:
    """The payload, split. ``ready`` goes to the importer (canonical parents AND rows with no valid parent, which
    it refuses); ``waiting`` is withheld. ``reasons`` counts why rows had no valid parent."""

    ready: list[dict[str, Any]] = field(default_factory=list)
    waiting: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=lambda: {
        CANONICAL_PARENT: 0, WAITING_FOR_PARENT_WAGER: 0, NO_VALID_PARENT: 0})
    reasons: dict[str, int] = field(default_factory=dict)
    proposal_problem: str | None = None

    def waiting_keys(self) -> list[str]:
        return [_first(row, KEY_FIELDS) for row in self.waiting]

    def summary(self) -> dict[str, Any]:
        """Counts and reason codes only -- safe for a public log."""
        return {
            "canonical_parent": self.counts[CANONICAL_PARENT],
            "waiting_for_parent_wager": self.counts[WAITING_FOR_PARENT_WAGER],
            "no_valid_parent": self.counts[NO_VALID_PARENT],
            "no_valid_parent_reasons": dict(sorted(self.reasons.items())),
            "proposal_problem": self.proposal_problem,
        }


def partition(
    rows: list[dict[str, Any]],
    canonical_parent_keys: set[str],
    proposal: Proposal | None,
    *,
    repo: str,
    ledger_branch: str,
) -> Partition:
    """Put every settled row in exactly one state. Order within ``ready`` and ``waiting`` follows ``rows``."""
    out = Partition()
    problem = proposal_problem(proposal, repo=repo, ledger_branch=ledger_branch)
    out.proposal_problem = problem
    by_key: dict[str, list[dict[str, Any]]] = {}
    if problem is None and proposal is not None:
        for record in proposal.records:
            key = _first(record, KEY_FIELDS)
            if key:
                by_key.setdefault(key, []).append(record)

    def no_parent(row: dict[str, Any], reason: str) -> None:
        out.ready.append(row)
        out.counts[NO_VALID_PARENT] += 1
        out.reasons[reason] = out.reasons.get(reason, 0) + 1

    for row in rows:
        key = _first(row, KEY_FIELDS)
        if not key:
            no_parent(row, "no_source_key")
            continue
        if key in canonical_parent_keys:
            out.ready.append(row)
            out.counts[CANONICAL_PARENT] += 1
            continue
        if problem is not None:
            no_parent(row, problem)
            continue
        parents = by_key.get(key, [])
        if not parents:
            no_parent(row, "parent_not_on_proposal")
            continue
        if len(parents) > 1:
            no_parent(row, "parent_ambiguous_on_proposal")
            continue
        parent = parents[0]
        if (_first(parent, TICKER_FIELDS) != _first(row, TICKER_FIELDS)
                or parent.get("side") != row.get("side")):
            no_parent(row, "parent_identity_mismatch")
            continue
        out.waiting.append(row)
        out.counts[WAITING_FOR_PARENT_WAGER] += 1
    return out
