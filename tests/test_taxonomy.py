"""Parsing Kalshi's public sport/competition/scope taxonomy."""

from __future__ import annotations

import pytest

from kalshi_router.errors import SchemaError
from kalshi_router.taxonomy import parse_filters_by_sport

from .synthetic import make_taxonomy


def test_parses_sports_and_competitions():
    taxonomy = parse_filters_by_sport(make_taxonomy({
        "Football": ["Pro Football", "College Football"],
        "Tennis": ["ATP Madrid"],
    }))
    assert taxonomy.sport_count == 2
    assert taxonomy.competition_count == 3
    assert taxonomy.sport_for_competition("Pro Football") == "football"
    assert taxonomy.sport_for_competition("ATP Madrid") == "tennis"


def test_lookup_is_normalized():
    taxonomy = parse_filters_by_sport(make_taxonomy({"Tennis": ["US  Open Men Singles"]}))
    assert taxonomy.sport_for_competition("  us open   MEN singles ") == "tennis"


def test_unknown_competition_returns_none():
    taxonomy = parse_filters_by_sport(make_taxonomy({"Tennis": ["ATP Madrid"]}))
    assert taxonomy.sport_for_competition("Pro Football") is None


def test_scopes_are_captured_for_diagnostics():
    taxonomy = parse_filters_by_sport(
        make_taxonomy({"Football": ["Pro Football"]}, scopes=["Games", "Futures"])
    )
    assert taxonomy.scopes == {"games", "futures"}


def test_competition_objects_with_a_name_field_are_read():
    payload = {
        "filters_by_sports": {
            "Football": {"competitions": [{"name": "Pro Football"}, {"title": "College Football"}]}
        },
        "sport_ordering": ["Football"],
    }
    taxonomy = parse_filters_by_sport(payload)
    assert taxonomy.sport_for_competition("College Football") == "football"


def test_sport_with_unreadable_filters_is_still_registered_and_counted():
    payload = {"filters_by_sports": {"Football": "not-an-object"}, "sport_ordering": []}
    taxonomy = parse_filters_by_sport(payload)
    assert "football" in taxonomy.sports
    assert taxonomy.skipped_sports == 1
    assert taxonomy.competition_count == 0


def test_empty_taxonomy_is_valid():
    taxonomy = parse_filters_by_sport({"filters_by_sports": {}, "sport_ordering": []})
    assert taxonomy.sport_count == 0 and taxonomy.competition_count == 0


@pytest.mark.parametrize(
    "payload",
    [{}, {"sport_ordering": []}, {"filters_by_sports": []}, {"filters_by_sports": "x"}],
)
def test_malformed_taxonomy_envelope_fails_closed(payload):
    with pytest.raises(SchemaError):
        parse_filters_by_sport(payload)


# ============================ competition ownership collisions ===============

def test_same_competition_repeated_under_one_sport_is_harmless():
    payload = {
        "filters_by_sports": {
            "Football": {
                "competitions": ["Pro Football", "Pro Football", "pro  FOOTBALL"],
                "leagues": ["Pro Football"],
            }
        },
        "sport_ordering": ["Football"],
    }
    taxonomy = parse_filters_by_sport(payload)
    assert taxonomy.sport_for_competition("Pro Football") == "football"
    assert taxonomy.is_ambiguous_competition("Pro Football") is False
    assert taxonomy.collision_count == 0


def test_same_competition_under_two_sports_becomes_ambiguous():
    taxonomy = parse_filters_by_sport(make_taxonomy({
        "Football": ["Shared Competition", "Pro Football"],
        "Tennis": ["Shared Competition", "ATP Madrid"],
    }))
    assert taxonomy.is_ambiguous_competition("Shared Competition") is True
    assert taxonomy.sport_for_competition("Shared Competition") is None
    assert taxonomy.collision_count == 1
    # Unaffected competitions still resolve.
    assert taxonomy.sport_for_competition("Pro Football") == "football"
    assert taxonomy.sport_for_competition("ATP Madrid") == "tennis"


def test_collision_is_detected_across_normalization():
    taxonomy = parse_filters_by_sport(make_taxonomy({
        "Football": ["Shared  Competition"],
        "Tennis": ["shared competition"],
    }))
    assert taxonomy.sport_for_competition("SHARED COMPETITION") is None
    assert taxonomy.collision_count == 1


def test_sport_ordering_cannot_change_the_outcome():
    forward = parse_filters_by_sport(make_taxonomy({
        "Football": ["Shared Competition"],
        "Tennis": ["Shared Competition"],
    }))
    reverse = parse_filters_by_sport(make_taxonomy({
        "Tennis": ["Shared Competition"],
        "Football": ["Shared Competition"],
    }))
    assert forward.sport_for_competition("Shared Competition") is None
    assert reverse.sport_for_competition("Shared Competition") is None
    assert forward.collision_count == reverse.collision_count == 1


def test_three_way_collision_is_still_one_ambiguous_competition():
    taxonomy = parse_filters_by_sport(make_taxonomy({
        "Football": ["Shared"], "Tennis": ["Shared"], "Baseball": ["Shared"],
    }))
    assert taxonomy.sport_for_competition("Shared") is None
    assert taxonomy.collision_count == 1
    assert taxonomy.competition_count == 0
