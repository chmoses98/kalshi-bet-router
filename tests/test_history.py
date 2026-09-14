"""History completeness: the gate that keeps `provable` at zero.

An episode is importable only if its OPENING is provable, and that needs the
whole history behind it. The failure this guards against is subtle: a truncated
walk and a complete one produce lists that look identical afterwards, so unless
the reason a walk ENDED is recorded, "this is the whole history" is a claim no
evidence could contradict.
"""

from __future__ import annotations

import pytest

from kalshi_router.accounting import HistoryCompleteness
from kalshi_router.client import KalshiReadOnlyClient, WalkStats
from kalshi_router.config import AuditConfig
from kalshi_router.history import HistoryEvidence, assemble_history

from .synthetic import FakeTransport, make_fill


def client_for(live_pages, archive_pages=None, signer=None, fail_archive=False):
    archive_pages = archive_pages if archive_pages is not None else [[]]

    def handler(method, path, query):
        if path.endswith("/historical/cutoff"):
            return 200, {"cutoff_ts": 1788000000}
        pages = archive_pages if "/historical/fills" in path else live_pages
        if "/historical/fills" in path and fail_archive:
            return 500, {"error": "archive unavailable"}
        cursor = query.get("cursor", [None])[0]
        index = int(cursor) if cursor else 0
        body = {"fills": pages[index]}
        body["cursor"] = str(index + 1) if index + 1 < len(pages) else ""
        return 200, body

    return KalshiReadOnlyClient(
        signer=signer, config=AuditConfig(max_retries=0),
        transport=FakeTransport(handler), sleep=lambda _: None,
    )


# ------------------------------------------------- what "exhausted" means

def test_a_walk_that_runs_out_of_data_is_exhausted(signer):
    stats = WalkStats()
    c = client_for([[make_fill(1)], [make_fill(2)]], signer=signer)
    list(c.iter_fills(max_fills=100, stats=stats))
    assert stats.exhausted and not stats.truncated
    assert stats.rows == 2 and stats.pages == 2


def test_a_walk_that_runs_out_of_budget_is_truncated(signer):
    stats = WalkStats()
    c = client_for([[make_fill(1), make_fill(2)], [make_fill(3)]], signer=signer)
    list(c.iter_fills(max_fills=2, stats=stats))
    assert stats.truncated and not stats.exhausted
    assert stats.rows == 2


def test_the_two_walks_return_identical_looking_lists(signer):
    """Which is exactly why the REASON a walk ended has to be recorded."""
    full = WalkStats()
    cut = WalkStats()
    a = list(client_for([[make_fill(1), make_fill(2)]], signer=signer)
             .iter_fills(max_fills=100, stats=full))
    b = list(client_for([[make_fill(1), make_fill(2)], [make_fill(3)]], signer=signer)
             .iter_fills(max_fills=2, stats=cut))
    assert a == b                      # indistinguishable from the rows alone
    assert full.exhausted and cut.truncated   # distinguishable only from stats


# --------------------------------------------------- the completeness rule

def test_history_is_complete_only_when_both_walks_exhaust(signer):
    history = assemble_history(
        client_for([[make_fill(1)]], [[make_fill(2)]], signer=signer)
    )
    assert history.evidence.complete
    assert history.completeness is HistoryCompleteness.COMPLETE
    assert len(history.fills) == 2


def test_a_truncated_live_walk_can_never_be_complete(signer):
    history = assemble_history(
        client_for([[make_fill(1), make_fill(2)], [make_fill(3)]],
                   [[make_fill(4)]], signer=signer),
        max_fills=2,
    )
    assert not history.evidence.complete
    assert history.completeness is HistoryCompleteness.BOUNDED_WINDOW


def test_skipping_the_archive_can_never_be_complete(signer):
    """The archive holds everything before the cutoff; not asking is not proof."""
    history = assemble_history(
        client_for([[make_fill(1)]], signer=signer), include_archive=False
    )
    assert history.evidence.historical_skipped
    assert not history.evidence.complete


def test_an_archive_that_fails_is_unproven_never_empty(signer):
    """Treating a failure as 'nothing older' is how a partial history gets
    promoted to a complete one."""
    history = assemble_history(
        client_for([[make_fill(1)]], signer=signer, fail_archive=True)
    )
    assert history.evidence.historical_failed
    assert history.evidence.historical_rows == 0
    assert not history.evidence.complete


def test_an_empty_archive_that_answered_is_still_complete(signer):
    """An account younger than the cutoff legitimately has no archive."""
    history = assemble_history(client_for([[make_fill(1)]], [[]], signer=signer))
    assert history.evidence.historical_exhausted
    assert history.evidence.historical_rows == 0
    assert history.evidence.complete


def test_a_rejected_fill_is_counted_and_excluded(signer):
    history = assemble_history(
        client_for([[make_fill(1), {"fill_id": "BAD"}]], [[]], signer=signer)
    )
    assert history.evidence.fills_rejected == 1
    assert len(history.fills) == 1


def test_completeness_is_not_inferred_from_row_count(signer):
    """A large truncated walk is still not the whole history."""
    big = [[make_fill(i) for i in range(50)], [make_fill(99)]]
    history = assemble_history(client_for(big, [[]], signer=signer), max_fills=50)
    assert history.evidence.live_rows == 50
    assert not history.evidence.complete


# ------------------------------------------------------------- the report

def test_the_evidence_renders_its_verdict_and_its_reasons():
    rendered = HistoryEvidence(live_exhausted=True, historical_exhausted=True).render()
    assert "HISTORY IS COMPLETE: True" in rendered
    assert "never because a limit was reached" in rendered


def test_the_evidence_is_counts_and_booleans_only():
    for name, value in HistoryEvidence().as_dict().items():
        assert isinstance(value, (int, bool)), f"{name} is not a count or a flag"
