"""The series probe corroborates candidates; it never promotes one.

The registry is EXACT MATCH and the classifier treats it as authoritative, so
an entry is only as good as the evidence behind it. `KXMLBF5` is obviously MLB
to a human reading it -- and that is exactly the reasoning this project does
not accept, because a prefix that looks right is a naming convention, not
evidence.
"""

from __future__ import annotations

import pytest

from kalshi_router.errors import HttpStatusError, SchemaError
from kalshi_router.series_probe import (
    MLB_LEDGER_CANDIDATES,
    probe_series,
)
from kalshi_router.sports import Sport


class FakeClient:
    def __init__(self, responses):
        self._responses = responses

    def get_series(self, ticker):
        value = self._responses.get(ticker)
        if value is None:
            raise HttpStatusError("get_series", 404)
        if isinstance(value, Exception):
            raise value
        return value


def test_a_series_whose_metadata_names_the_expected_sport_is_confirmed():
    client = FakeClient({"KXMLBF5": {"title": "MLB First 5 Innings", "category": "Sports"}})

    report = probe_series(client, {"KXMLBF5": Sport.MLB})

    assert report.confirmed == 1
    assert report.results[0].promotable
    assert report.results[0].evidence_field == "series.title"


def test_a_series_kalshi_does_not_have_is_not_promoted():
    client = FakeClient({})

    report = probe_series(client, {"KXMLBNOPE": Sport.MLB})

    assert report.not_found == 1
    assert report.confirmed == 0
    assert not report.results[0].promotable


def test_a_series_with_no_corroborating_metadata_is_not_promoted():
    """Existing is not the same as being about the sport we think."""
    client = FakeClient({"KXMLBF5": {"title": "First 5 Innings", "category": "Sports"}})

    report = probe_series(client, {"KXMLBF5": Sport.MLB})

    assert report.no_corroborating_metadata == 1
    assert report.confirmed == 0
    assert not report.results[0].promotable


def test_metadata_naming_a_DIFFERENT_sport_is_a_contradiction_not_a_miss():
    """The dangerous case: a KXMLB-prefixed series that is not MLB.

    Reported as contradicted rather than merely uncorroborated, because the two
    call for different responses -- one is a gap, the other is a trap.
    """
    client = FakeClient({"KXMLBF5": {"title": "NFL Week 3", "tags": ["NFL"]}})

    report = probe_series(client, {"KXMLBF5": Sport.MLB})

    assert report.contradicted == 1
    assert report.confirmed == 0
    assert not report.results[0].promotable
    assert "NFL" in report.results[0].note


def test_metadata_naming_the_expected_sport_AND_another_is_still_refused():
    client = FakeClient({"KXMLBF5": {"title": "MLB", "tags": ["NCAAF"]}})

    report = probe_series(client, {"KXMLBF5": Sport.MLB})

    assert report.contradicted == 1
    assert not report.results[0].promotable


def test_a_lookup_failure_is_distinct_from_a_missing_series():
    """A schema error means we could not ask, not that the answer was no."""
    client = FakeClient({"KXMLBF5": SchemaError("bad envelope")})

    report = probe_series(client, {"KXMLBF5": Sport.MLB})

    assert report.lookup_failed == 1
    assert report.not_found == 0
    assert not report.results[0].promotable


def test_every_candidate_lands_in_exactly_one_bucket():
    client = FakeClient({
        "A": {"title": "MLB game"},
        "B": {"title": "nothing useful"},
        "C": {"title": "NFL"},
    })

    report = probe_series(client, {k: Sport.MLB for k in ("A", "B", "C", "D")})

    assert report.probed == 4
    assert (
        report.confirmed
        + report.not_found
        + report.no_corroborating_metadata
        + report.contradicted
        + report.lookup_failed
    ) == report.probed


def test_the_candidates_are_the_gap_measured_from_the_owners_ledger():
    """Each candidate carries the row count that evidences it, so the list
    cannot quietly grow to include a series nobody has ever traded."""
    assert MLB_LEDGER_CANDIDATES
    assert all(count > 0 for count in MLB_LEDGER_CANDIDATES.values())
    assert sum(MLB_LEDGER_CANDIDATES.values()) == 357


def test_the_probe_promotes_nothing_by_itself():
    """It reports. A table the classifier treats as authoritative is not
    written by the same run that decided it wanted more entries."""
    from kalshi_router import series_registry

    before = dict(series_registry.SERIES_TICKER_REGISTRY)
    probe_series(FakeClient({"KXMLBF5": {"title": "MLB"}}), {"KXMLBF5": Sport.MLB})

    assert series_registry.SERIES_TICKER_REGISTRY == before
