"""The public milestone backstop and its privacy property."""

from __future__ import annotations

import pytest

from kalshi_router.client import KalshiReadOnlyClient
from kalshi_router.config import AuditConfig
from kalshi_router.errors import SchemaError
from kalshi_router.milestones import (
    MilestoneIndex,
    TARGET_COMPETITIONS,
    build_milestone_index,
    parse_milestones_payload,
)

from .synthetic import FakeTransport, make_milestone


def build(handler, signer):
    transport = FakeTransport(handler)
    client = KalshiReadOnlyClient(
        signer=signer, config=AuditConfig(max_retries=0), transport=transport, sleep=lambda _: None
    )
    return client, transport


def milestone_handler(by_competition: dict[str, list[dict]], pages: int = 1):
    def handler(method, path, query):
        if not path.endswith("/milestones"):
            return 404, {"error": "unknown"}
        competition = query.get("competition", [""])[0]
        cursor = query.get("cursor", [None])[0]
        milestones = by_competition.get(competition, [])
        if pages > 1 and not cursor:
            return 200, {"milestones": milestones, "cursor": "next"}
        return 200, {"milestones": milestones if cursor or pages == 1 else [], "cursor": ""}

    return handler


def test_index_maps_event_tickers_to_competitions(signer):
    client, _ = build(milestone_handler({
        "Pro Football": [make_milestone("m1", ["KXNFLGAME-A", "KXNFLGAME-B"])],
        "College Football": [make_milestone("m2", ["KXNCAAFGAME-C"])],
    }), signer)
    index = build_milestone_index(client)
    assert index.competition_for_event("KXNFLGAME-A") == "Pro Football"
    assert index.competition_for_event("KXNCAAFGAME-C") == "College Football"
    assert index.indexed_events == 3


def test_lookup_is_case_insensitive_on_the_ticker(signer):
    client, _ = build(milestone_handler({"Pro Baseball": [make_milestone("m", ["kxmlb-a"])]}), signer)
    index = build_milestone_index(client)
    assert index.competition_for_event("KXMLB-A") == "Pro Baseball"


def test_related_event_tickers_are_indexed_too(signer):
    milestone = make_milestone("m", [], related_event_tickers=["KXNFLGAME-R"])
    client, _ = build(milestone_handler({"Pro Football": [milestone]}), signer)
    assert build_milestone_index(client).competition_for_event("KXNFLGAME-R") == "Pro Football"


def test_unknown_event_is_not_indexed(signer):
    client, _ = build(milestone_handler({"Pro Football": [make_milestone("m", ["KXA"])]}), signer)
    assert build_milestone_index(client).competition_for_event("KXB") is None
    assert build_milestone_index(client).competition_for_event(None) is None


def test_account_tickers_are_never_sent_to_the_milestone_endpoint(signer):
    """The index is built from public competition queries and looked up locally."""
    client, transport = build(
        milestone_handler({"Pro Football": [make_milestone("m", ["KXNFLGAME-A"])]}), signer
    )
    build_milestone_index(client)
    joined = " ".join(transport.paths)
    assert "KXNFLGAME-A" not in joined
    for competition in TARGET_COMPETITIONS:
        assert competition.replace(" ", "+") in joined or competition.replace(" ", "%20") in joined


def test_only_target_competitions_are_swept(signer):
    client, transport = build(milestone_handler({}), signer)
    index = build_milestone_index(client)
    assert index.competitions_swept == len(TARGET_COMPETITIONS)
    assert len(transport.paths) == len(TARGET_COMPETITIONS)


def test_request_budget_is_enforced(signer):
    client, transport = build(milestone_handler({}), signer)
    index = build_milestone_index(client, request_budget=2)
    assert index.requests_issued <= 2
    assert index.budget_exhausted is True


def test_pagination_is_followed(signer):
    client, transport = build(
        milestone_handler({"Pro Football": [make_milestone("m", ["KXA"])]}, pages=2), signer
    )
    index = build_milestone_index(client, competitions=["Pro Football"])
    assert index.requests_issued == 2


def test_milestone_outage_degrades_instead_of_raising(signer):
    def handler(method, path, query):
        return 500, {"error": "boom"}

    client, _ = build(handler, signer)
    index = build_milestone_index(client)
    assert index.fetch_failed is True
    assert index.indexed_events == 0


def test_malformed_milestone_list_degrades_instead_of_raising(signer):
    client, _ = build(lambda m, p, q: (200, {"milestones": "not-a-list"}), signer)
    index = build_milestone_index(client)
    assert index.fetch_failed is True
    assert index.indexed_events == 0


def test_non_object_milestone_entries_are_skipped(signer):
    client, _ = build(
        lambda m, p, q: (200, {"milestones": ["oops", make_milestone("m", ["KXA"])], "cursor": ""}),
        signer,
    )
    index = build_milestone_index(client, competitions=["Pro Football"])
    assert index.competition_for_event("KXA") == "Pro Football"


@pytest.mark.parametrize("payload", [{}, {"milestones": "x"}, {"milestones": 3}])
def test_parse_milestones_payload_fails_closed(payload):
    with pytest.raises(SchemaError):
        parse_milestones_payload(payload)


# ================================ event conflicts ============================

def test_duplicate_link_under_the_same_competition_is_harmless(signer):
    client, _ = build(milestone_handler({
        "Pro Football": [make_milestone("m1", ["KXA"]), make_milestone("m2", ["KXA"])],
    }), signer)
    index = build_milestone_index(client, competitions=["Pro Football"])
    assert index.competition_for_event("KXA") == "Pro Football"
    assert index.conflict_count == 0
    assert index.indexed_events == 1


def test_event_under_two_competitions_becomes_conflicted(signer):
    client, _ = build(milestone_handler({
        "Pro Football": [make_milestone("m1", ["KXA", "KXONLYPRO"])],
        "College Football": [make_milestone("m2", ["KXA", "KXONLYCFB"])],
    }), signer)
    index = build_milestone_index(client)
    assert index.is_conflicted("KXA") is True
    assert index.competition_for_event("KXA") is None
    assert index.conflict_count == 1
    # Unaffected events still resolve.
    assert index.competition_for_event("KXONLYPRO") == "Pro Football"
    assert index.competition_for_event("KXONLYCFB") == "College Football"


def test_sweep_order_cannot_change_the_result(signer):
    both = {
        "Pro Football": [make_milestone("m1", ["KXA"])],
        "College Football": [make_milestone("m2", ["KXA"])],
    }
    client_a, _ = build(milestone_handler(both), signer)
    client_b, _ = build(milestone_handler(both), signer)
    forward = build_milestone_index(client_a, competitions=["Pro Football", "College Football"])
    reverse = build_milestone_index(client_b, competitions=["College Football", "Pro Football"])
    assert forward.competition_for_event("KXA") is None
    assert reverse.competition_for_event("KXA") is None
    assert forward.conflict_count == reverse.conflict_count == 1


def test_a_conflicted_event_stays_conflicted_after_a_repeat_link():
    index = MilestoneIndex()
    index.record("KXA", "Pro Football")
    index.record("KXA", "College Football")
    index.record("KXA", "Pro Football")
    index.record("KXA", "Pro Football")
    assert index.competition_for_event("KXA") is None
    assert index.is_conflicted("KXA") is True
    assert index.indexed_events == 0


def test_record_is_case_insensitive_when_detecting_conflicts():
    index = MilestoneIndex()
    index.record("kxa", "Pro Football")
    index.record("KXA", "College Football")
    assert index.competition_for_event("KXA") is None
