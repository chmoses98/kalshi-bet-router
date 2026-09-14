"""Assembling a history the replay is allowed to call complete.

Every audit so far has reported:

    position episodes observed: 155
      provable from supplied history: 0
      with an importable identity:    0

Those zeros are correct, and they are the gate. An episode is a candidate wager
only if its OPENING is provable, and a bounded recent window cannot prove where
flat was: back-filling older fills can merge an episode into an older one and
change its opening fill. So nothing is importable until the history behind it is
known to be whole.

What "whole" requires
---------------------
Two walks, both run to exhaustion:

* ``GET /portfolio/fills`` -- the live route;
* ``GET /historical/fills`` -- everything older than ``GET /historical/cutoff``,
  which the live route does not serve at all.

**Exhausted means the server said there was nothing more.** A walk that stopped
because ``max_fills`` ran out saw part of the history and produces a list that
looks exactly like a complete one. That is why :class:`WalkStats` records *why*
a walk ended: without it, "this is the whole history" is a claim no evidence
could contradict.

Completeness is therefore never inferred from the size of the result, from the
absence of errors, or from the caller's intent. It is asserted only when both
walks report ``exhausted`` and neither reports ``truncated``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .accounting.engine import HistoryCompleteness
from .client import KalshiReadOnlyClient, WalkStats
from .errors import SchemaError
from .models import NormalizedFill, normalize_fill


@dataclass
class HistoryEvidence:
    """Why the history was, or was not, judged complete. Counts only."""

    live_rows: int = 0
    live_pages: int = 0
    live_exhausted: bool = False
    live_truncated: bool = False

    historical_rows: int = 0
    historical_pages: int = 0
    historical_exhausted: bool = False
    historical_truncated: bool = False
    #: The archive route was not consulted at all.
    historical_skipped: bool = False
    #: The archive route failed; its absence is then unproven, not empty.
    historical_failed: bool = False

    fills_rejected: int = 0
    cutoff_retrieved: bool = False

    @property
    def complete(self) -> bool:
        """True only when both walks ran out of data rather than out of budget."""
        return (
            self.live_exhausted
            and not self.live_truncated
            and self.historical_exhausted
            and not self.historical_truncated
            and not self.historical_skipped
            and not self.historical_failed
        )

    @property
    def completeness(self) -> HistoryCompleteness:
        return (
            HistoryCompleteness.COMPLETE
            if self.complete
            else HistoryCompleteness.BOUNDED_WINDOW
        )

    def as_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in vars(self).items()}
        data["complete"] = self.complete
        return data

    def render(self) -> str:
        return "\n".join([
            "history completeness evidence:",
            f"  live fills: {self.live_rows} over {self.live_pages} pages",
            f"    walk exhausted (server had no more): {self.live_exhausted}",
            f"    walk truncated (budget ran out): {self.live_truncated}",
            f"  archived fills: {self.historical_rows} over "
            f"{self.historical_pages} pages",
            f"    walk exhausted: {self.historical_exhausted}",
            f"    walk truncated: {self.historical_truncated}",
            f"    archive skipped: {self.historical_skipped}",
            f"    archive failed: {self.historical_failed}",
            f"  fills rejected (excluded from accounting): {self.fills_rejected}",
            f"  historical cutoff retrieved: {self.cutoff_retrieved}",
            "",
            f"  HISTORY IS COMPLETE: {self.complete}",
            "",
            "  NOTE: complete means both walks ended because the server had",
            "        nothing more, never because a limit was reached. Only a",
            "        complete history can make an episode's opening provable,",
            "        and only a provable opening can be imported.",
        ])


@dataclass
class AssembledHistory:
    """Fills plus the evidence for what may be claimed about them."""

    fills: list[NormalizedFill] = field(default_factory=list)
    evidence: HistoryEvidence = field(default_factory=HistoryEvidence)

    @property
    def completeness(self) -> HistoryCompleteness:
        return self.evidence.completeness


def assemble_history(
    client: KalshiReadOnlyClient,
    max_fills: int | None = None,
    include_archive: bool = True,
) -> AssembledHistory:
    """Walk both fill routes and report what may honestly be claimed.

    ``max_fills`` bounds each walk. Passing it is what makes the result a
    sample, and the evidence will say so: a truncated walk can never yield
    ``COMPLETE``, however many rows it returned.
    """
    history = AssembledHistory()
    evidence = history.evidence

    live_stats = WalkStats()
    _collect(client.iter_fills(max_fills=max_fills, stats=live_stats), history)
    evidence.live_rows = live_stats.rows
    evidence.live_pages = live_stats.pages
    evidence.live_exhausted = live_stats.exhausted
    evidence.live_truncated = live_stats.truncated

    if not include_archive:
        evidence.historical_skipped = True
        return history

    try:
        client.get_historical_cutoff()
        evidence.cutoff_retrieved = True
    except Exception:
        # The cutoff is informational; failing to read it does not by itself
        # invalidate the walks, so it is recorded rather than fatal.
        evidence.cutoff_retrieved = False

    archive_stats = WalkStats()
    try:
        _collect(
            client.iter_historical_fills(max_fills=max_fills, stats=archive_stats),
            history,
        )
    except Exception:
        # An archive that could not be read is UNPROVEN, never empty. Treating a
        # failure as "there was nothing older" is how a partial history gets
        # promoted to a complete one.
        evidence.historical_failed = True

    evidence.historical_rows = archive_stats.rows
    evidence.historical_pages = archive_stats.pages
    evidence.historical_exhausted = archive_stats.exhausted
    evidence.historical_truncated = archive_stats.truncated
    return history


def _collect(raw_fills: Any, history: AssembledHistory) -> None:
    """Normalize, excluding-and-counting what cannot be interpreted."""
    for raw in raw_fills:
        try:
            history.fills.append(normalize_fill(raw))
        except SchemaError:
            history.evidence.fills_rejected += 1
