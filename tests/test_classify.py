"""Sport classification: evidence hierarchy and the fail-closed rule."""

from __future__ import annotations

import pytest

from kalshi_router.classify import (
    EvidenceLevel,
    EvidenceStrength,
    MarketContext,
    UnresolvedReason,
    classify_market,
    derive_series_ticker,
)
from kalshi_router.milestones import MilestoneIndex
from kalshi_router.sports import Sport
from kalshi_router.taxonomy import parse_filters_by_sport

from .synthetic import make_event, make_event_metadata, make_market, make_series, make_taxonomy


def context(
    market_ticker="KXTEST-SYNTH01-AAA",
    event_ticker="KXTEST-SYNTH01",
    series_ticker="KXTEST",
    series_extra=None,
    event_extra=None,
    market_extra=None,
    competition=None,
    competition_scope=None,
    with_event_metadata=False,
    **kwargs,
):
    metadata = None
    if competition is not None or with_event_metadata:
        metadata = make_event_metadata(competition, competition_scope)
    return MarketContext(
        market_ticker=market_ticker,
        market=make_market(market_ticker, event_ticker, **(market_extra or {})),
        event=make_event(event_ticker, series_ticker, **(event_extra or {})),
        series=make_series(series_ticker, **(series_extra or {})),
        event_metadata=metadata,
        **kwargs,
    )


TAXONOMY = parse_filters_by_sport(make_taxonomy({
    "Baseball": ["Pro Baseball", "College Baseball"],
    "Football": ["Pro Football", "College Football", "Semi-Pro Football"],
    "Tennis": ["US Open Men Singles", "ATP Madrid"],
    "Basketball": ["Pro Basketball (M)"],
    "Soccer": ["Premier League"],
}))


# =============================== L1: event metadata competition ==============

def test_pro_baseball_competition_resolves_to_mlb():
    result = classify_market(context(competition="Pro Baseball", competition_scope="Game"))
    assert result.sport is Sport.MLB
    assert result.resolved_by is EvidenceLevel.L1_EVENT_COMPETITION
    assert result.competition == "Pro Baseball"
    assert result.competition_scope == "Game"


def test_pro_football_competition_resolves_to_nfl():
    result = classify_market(context(competition="Pro Football"))
    assert result.sport is Sport.NFL
    assert result.resolved_by is EvidenceLevel.L1_EVENT_COMPETITION


def test_college_football_competition_resolves_to_cfb():
    """The exact NFL/CFB split Phase 0 had to refuse."""
    result = classify_market(context(competition="College Football"))
    assert result.sport is Sport.CFB
    assert result.resolved_by is EvidenceLevel.L1_EVENT_COMPETITION


def test_pro_and_college_football_are_never_confused():
    pro = classify_market(context(competition="Pro Football"))
    college = classify_market(context(competition="College Football"))
    assert pro.sport is Sport.NFL and college.sport is Sport.CFB


def test_competition_matching_is_case_and_space_insensitive():
    assert classify_market(context(competition="  pro   BASEBALL ")).sport is Sport.MLB


def test_tennis_tour_competition_resolves_without_taxonomy():
    result = classify_market(context(competition="ATP Madrid"))
    assert result.sport is Sport.TENNIS


def test_out_of_scope_competition_is_other():
    result = classify_market(context(competition="Pro Basketball (M)"))
    assert result.sport is Sport.OTHER
    assert result.resolved_by is EvidenceLevel.L1_EVENT_COMPETITION


# =============================== L2: sport taxonomy ==========================

def test_tournament_competition_resolves_through_the_taxonomy():
    result = classify_market(context(competition="US Open Men Singles"), taxonomy=TAXONOMY)
    assert result.sport is Sport.TENNIS
    assert result.resolved_by is EvidenceLevel.L2_SPORT_TAXONOMY


def test_taxonomy_places_an_unknown_competition_out_of_scope():
    result = classify_market(context(competition="Premier League"), taxonomy=TAXONOMY)
    assert result.sport is Sport.OTHER


def test_unrecognized_competition_in_an_ambiguous_sport_fails_closed():
    """A new Football competition must never be guessed into NFL or CFB."""
    result = classify_market(context(competition="Semi-Pro Football"), taxonomy=TAXONOMY)
    assert result.sport is Sport.UNRESOLVED
    assert result.unresolved_reason is UnresolvedReason.COMPETITION_UNKNOWN


def test_unknown_competition_without_taxonomy_fails_closed():
    result = classify_market(context(competition="Totally Unknown Cup"))
    assert result.sport is Sport.UNRESOLVED
    assert result.unresolved_reason is UnresolvedReason.COMPETITION_UNKNOWN


def test_unknown_competition_does_not_fall_back_to_the_registry():
    """Fail-closed beats a guessed registry: the competition is the stronger claim."""
    result = classify_market(
        context(
            market_ticker="KXNFLGAME-SYNTH01-KC",
            event_ticker="KXNFLGAME-SYNTH01",
            series_ticker="KXNFLGAME",
            competition="Semi-Pro Football",
        ),
        taxonomy=TAXONOMY,
    )
    assert result.sport is Sport.UNRESOLVED
    assert result.unresolved_reason is UnresolvedReason.COMPETITION_UNKNOWN


# =============================== null competition ============================

def test_null_competition_falls_through_to_series_metadata():
    result = classify_market(
        context(with_event_metadata=True, series_extra={"category": "Sports", "tags": ["MLB"]})
    )
    assert result.sport is Sport.MLB
    assert result.resolved_by is EvidenceLevel.L4_SERIES_METADATA


def test_null_competition_with_nothing_else_is_competition_absent():
    result = classify_market(context(with_event_metadata=True))
    assert result.sport is Sport.UNRESOLVED
    assert result.unresolved_reason is UnresolvedReason.COMPETITION_ABSENT


# =============================== L3: milestones ==============================

def test_milestone_competition_resolves_an_event_without_competition():
    index = MilestoneIndex(event_to_competition={"KXTEST-SYNTH01": "College Football"})
    result = classify_market(context(with_event_metadata=True), milestone_index=index)
    assert result.sport is Sport.CFB
    assert result.resolved_by is EvidenceLevel.L3_MILESTONE


def test_milestone_is_ignored_when_the_event_is_not_indexed():
    index = MilestoneIndex(event_to_competition={"SOMETHING-ELSE": "Pro Football"})
    result = classify_market(context(with_event_metadata=True), milestone_index=index)
    assert result.sport is Sport.UNRESOLVED


# =============================== precedence ==================================

def test_competition_overrides_a_contradictory_registry_entry():
    """The registry is our table, not Kalshi's: it yields, and we count it."""
    result = classify_market(
        context(
            market_ticker="KXNFLGAME-SYNTH01-KC",
            event_ticker="KXNFLGAME-SYNTH01",
            series_ticker="KXNFLGAME",
            competition="College Football",
        )
    )
    assert result.sport is Sport.CFB
    assert result.resolved_by is EvidenceLevel.L1_EVENT_COMPETITION
    assert result.lower_level_conflict is True


def test_registry_never_overrides_a_contradictory_competition():
    result = classify_market(
        context(
            market_ticker="KXMLBGAME-SYNTH01-NYY",
            event_ticker="KXMLBGAME-SYNTH01",
            series_ticker="KXMLBGAME",
            competition="Pro Football",
        )
    )
    assert result.sport is Sport.NFL
    assert result.resolved_by is not EvidenceLevel.L5_SERIES_REGISTRY


def test_competition_conflicting_with_series_category_is_unresolved():
    """Two pieces of real Kalshi metadata disagreeing means we do not understand it."""
    result = classify_market(
        context(competition="Pro Baseball", series_extra={"category": "Sports", "tags": ["NFL"]})
    )
    assert result.sport is Sport.UNRESOLVED
    assert result.unresolved_reason is UnresolvedReason.EVIDENCE_CONFLICT


def test_competition_agreeing_with_series_metadata_resolves():
    result = classify_market(
        context(competition="Pro Baseball", series_extra={"category": "Sports", "tags": ["MLB"]})
    )
    assert result.sport is Sport.MLB
    assert result.resolved_by is EvidenceLevel.L1_EVENT_COMPETITION


def test_series_metadata_outranks_the_registry():
    result = classify_market(
        context(
            market_ticker="KXNFLGAME-SYNTH01-KC",
            event_ticker="KXNFLGAME-SYNTH01",
            series_ticker="KXNFLGAME",
            series_extra={"category": "Sports", "tags": ["College Football"]},
        )
    )
    assert result.sport is Sport.CFB
    assert result.resolved_by is EvidenceLevel.L4_SERIES_METADATA
    assert result.lower_level_conflict is True


# ------------------------------------------------------- the four sports

def test_mlb_from_series_tags():
    result = classify_market(
        context(series_extra={"category": "Sports", "tags": ["Baseball", "MLB"]})
    )
    assert result.sport is Sport.MLB
    assert "series.tags" in result.reason


def test_nfl_from_series_tags():
    result = classify_market(
        context(series_extra={"category": "Sports", "tags": ["Football", "NFL"]})
    )
    assert result.sport is Sport.NFL


def test_cfb_from_series_tags():
    result = classify_market(
        context(series_extra={"category": "Sports", "tags": ["Football", "College Football"]})
    )
    assert result.sport is Sport.CFB


def test_tennis_from_series_tags():
    result = classify_market(context(series_extra={"category": "Sports", "tags": ["Tennis"]}))
    assert result.sport is Sport.TENNIS


def test_league_from_series_title_when_tags_absent():
    result = classify_market(context(series_extra={"title": "MLB Game Winner"}))
    assert result.sport is Sport.MLB


def test_tag_objects_with_a_name_field_are_read():
    result = classify_market(
        context(series_extra={"tags": [{"name": "Tennis"}, {"name": "ATP"}]})
    )
    assert result.sport is Sport.TENNIS


def test_categories_list_is_read_alongside_category():
    result = classify_market(
        context(series_extra={"category": "Sports", "categories": ["Sports", "NFL"]})
    )
    assert result.sport is Sport.NFL


# ------------------------------------------------------------ series registry

def test_exact_series_ticker_registry_match_classifies():
    result = classify_market(
        context(
            market_ticker="KXNFLGAME-SYNTH01-KC",
            event_ticker="KXNFLGAME-SYNTH01",
            series_ticker="KXNFLGAME",
        )
    )
    assert result.sport is Sport.NFL
    assert result.used_unverified_series_ticker is True


def test_series_prefix_of_a_market_ticker_is_used_when_metadata_is_thin():
    result = classify_market(
        MarketContext(
            market_ticker="KXMLBGAME-SYNTH01-NYY",
            market=make_market("KXMLBGAME-SYNTH01-NYY", "KXMLBGAME-SYNTH01"),
        )
    )
    assert result.sport is Sport.MLB


def test_unknown_series_ticker_does_not_guess():
    result = classify_market(
        context(
            market_ticker="KXNFLISH-SYNTH01-AAA",
            event_ticker="KXNFLISH-SYNTH01",
            series_ticker="KXNFLISH",
        )
    )
    assert result.sport is Sport.UNRESOLVED


def test_series_prefix_requires_an_exact_match_not_a_prefix_resemblance():
    result = classify_market(
        MarketContext(
            market_ticker="KXNFLGAMEXTRA-SYNTH01-AAA",
            market=make_market("KXNFLGAMEXTRA-SYNTH01-AAA", "KXNFLGAMEXTRA-SYNTH01"),
        )
    )
    assert result.sport is Sport.UNRESOLVED


def test_derive_series_ticker_prefers_metadata_over_the_ticker_prefix():
    ticker, source = derive_series_ticker(
        context(market_ticker="KXOTHER-SYNTH01-AAA", series_ticker="KXMLBGAME")
    )
    assert ticker == "KXMLBGAME" and source == "series.ticker"


# ------------------------------------------------------------------- OTHER

def test_non_target_sport_is_confidently_other():
    result = classify_market(
        context(series_extra={"category": "Sports", "tags": ["Basketball", "NBA"]})
    )
    assert result.sport is Sport.OTHER


def test_non_sports_category_is_confidently_other():
    result = classify_market(context(series_extra={"category": "Economics", "tags": ["Inflation"]}))
    assert result.sport is Sport.OTHER


def test_a_named_rival_sport_outranks_a_generic_family_word():
    # "NCAA" alone is ambiguous, but "Basketball" positively identifies the sport.
    result = classify_market(
        context(series_extra={"category": "Sports", "tags": ["NCAA", "Basketball"]})
    )
    assert result.sport is Sport.OTHER


# ------------------------------------------------------- the fail-closed rule

def test_generic_football_is_never_routed_to_nfl_or_cfb():
    result = classify_market(
        context(series_extra={"category": "Sports", "tags": ["Football"]})
    )
    assert result.sport is Sport.UNRESOLVED
    assert "ambiguous_sport_family" in result.reason


def test_generic_baseball_is_never_routed_to_mlb():
    result = classify_market(
        context(series_extra={"category": "Sports", "tags": ["Baseball"]})
    )
    assert result.sport is Sport.UNRESOLVED


def test_series_metadata_beats_a_contradictory_registry_entry():
    result = classify_market(
        context(
            market_ticker="KXNFLGAME-SYNTH01-KC",
            event_ticker="KXNFLGAME-SYNTH01",
            series_ticker="KXNFLGAME",
            series_extra={"category": "Sports", "tags": ["Tennis"]},
        )
    )
    assert result.sport is Sport.TENNIS
    assert result.lower_level_conflict is True


def test_two_series_tags_naming_different_leagues_is_unresolved():
    result = classify_market(
        context(series_extra={"category": "Sports", "tags": ["MLB"], "title": "NFL Game Winner"})
    )
    assert result.sport is Sport.UNRESOLVED
    assert result.unresolved_reason is UnresolvedReason.EVIDENCE_CONFLICT


def test_supporting_evidence_alone_never_resolves():
    # An event title mentioning a league is not enough on its own.
    result = classify_market(
        context(event_extra={"title": "NFL: Synthetic Team A vs Synthetic Team B"})
    )
    assert result.sport is Sport.UNRESOLVED
    assert result.reason == "insufficient_authoritative_metadata"
    assert any(e.strength is EvidenceStrength.SUPPORTING for e in result.evidence)


def test_metadata_lookup_failure_is_unresolved_not_a_guess():
    result = classify_market(
        MarketContext(market_ticker="KXMLBGAME-SYNTH01-NYY", lookup_error="HttpStatusError(status=500)")
    )
    assert result.sport is Sport.UNRESOLVED
    assert "metadata_lookup_failed" in result.reason


def test_no_metadata_at_all_is_unresolved():
    result = classify_market(MarketContext(market_ticker="KXMLBGAME-SYNTH01-NYY"))
    assert result.sport is Sport.UNRESOLVED
    assert result.reason == "no_metadata_resolved"


@pytest.mark.parametrize("sport", [Sport.MLB, Sport.NFL, Sport.CFB, Sport.TENNIS])
def test_routable_flag_matches_the_four_supported_sports(sport):
    result = classify_market(context(series_extra={"tags": [sport.value]}))
    assert result.sport is sport
    assert result.is_routable is True


def test_other_and_unresolved_are_not_routable():
    other = classify_market(context(series_extra={"category": "Politics"}))
    unresolved = classify_market(MarketContext(market_ticker="X"))
    assert other.is_routable is False and unresolved.is_routable is False


# ------------------------------------------------------------ explainability

def test_classification_carries_auditable_evidence():
    result = classify_market(
        context(
            market_ticker="KXMLBGAME-SYNTH01-NYY",
            event_ticker="KXMLBGAME-SYNTH01",
            series_ticker="KXMLBGAME",
            series_extra={"category": "Sports", "tags": ["MLB"]},
        )
    )
    assert result.sport is Sport.MLB
    assert result.market_ticker == "KXMLBGAME-SYNTH01-NYY"
    assert result.series_ticker == "KXMLBGAME"
    assert result.event_ticker == "KXMLBGAME-SYNTH01"
    sources = {e.source for e in result.evidence}
    assert "series.tags" in sources
    assert any(s.startswith("series_registry") for s in sources)


def test_word_boundaries_prevent_substring_false_positives():
    result = classify_market(context(series_extra={"title": "Adaptation index", "tags": []}))
    assert result.sport is Sport.UNRESOLVED


# ================== ambiguous taxonomy competition (fail-closed) =============

AMBIGUOUS_TAXONOMY = parse_filters_by_sport(make_taxonomy({
    "Football": ["Shared Competition", "Pro Football"],
    "Tennis": ["Shared Competition"],
}))


def test_ambiguous_taxonomy_competition_is_unresolved():
    result = classify_market(
        context(competition="Shared Competition"), taxonomy=AMBIGUOUS_TAXONOMY
    )
    assert result.sport is Sport.UNRESOLVED
    assert result.unresolved_reason is UnresolvedReason.COMPETITION_AMBIGUOUS


def test_ambiguous_competition_can_never_route_any_classification():
    """Requirement: never MLB, NFL, CFB, TENNIS or OTHER."""
    taxonomy = parse_filters_by_sport(make_taxonomy({
        # A colliding name that our own direct rules would otherwise resolve.
        "Football": ["Pro Football"],
        "Tennis": ["Pro Football"],
    }))
    result = classify_market(
        context(
            market_ticker="KXNFLGAME-SYNTH01-KC",
            event_ticker="KXNFLGAME-SYNTH01",
            series_ticker="KXNFLGAME",
            competition="Pro Football",
            series_extra={"category": "Sports", "tags": ["NFL"]},
        ),
        taxonomy=taxonomy,
    )
    assert result.sport is Sport.UNRESOLVED
    assert result.is_routable is False
    assert result.resolved_by is None


def test_ambiguous_competition_is_not_rescued_by_series_or_registry():
    result = classify_market(
        context(
            market_ticker="KXMLBGAME-SYNTH01-NYY",
            event_ticker="KXMLBGAME-SYNTH01",
            series_ticker="KXMLBGAME",
            competition="Shared Competition",
            series_extra={"category": "Sports", "tags": ["MLB"]},
        ),
        taxonomy=AMBIGUOUS_TAXONOMY,
    )
    assert result.sport is Sport.UNRESOLVED


def test_taxonomy_sport_ordering_does_not_change_classification():
    forward = parse_filters_by_sport(make_taxonomy({
        "Football": ["Shared Competition"], "Tennis": ["Shared Competition"],
    }))
    reverse = parse_filters_by_sport(make_taxonomy({
        "Tennis": ["Shared Competition"], "Football": ["Shared Competition"],
    }))
    a = classify_market(context(competition="Shared Competition"), taxonomy=forward)
    b = classify_market(context(competition="Shared Competition"), taxonomy=reverse)
    assert a.sport is b.sport is Sport.UNRESOLVED


# ==================== conflicted milestone evidence (fail-closed) ============

def test_conflicted_milestone_event_is_unresolved():
    index = MilestoneIndex()
    index.record("KXTEST-SYNTH01", "Pro Football")
    index.record("KXTEST-SYNTH01", "College Football")
    result = classify_market(context(with_event_metadata=True), milestone_index=index)
    assert result.sport is Sport.UNRESOLVED
    assert result.unresolved_reason is UnresolvedReason.MILESTONE_CONFLICT


def test_conflicted_milestone_evidence_never_routes_a_wager():
    index = MilestoneIndex()
    index.record("KXNFLGAME-SYNTH01", "Pro Football")
    index.record("KXNFLGAME-SYNTH01", "College Football")
    result = classify_market(
        context(
            market_ticker="KXNFLGAME-SYNTH01-KC",
            event_ticker="KXNFLGAME-SYNTH01",
            series_ticker="KXNFLGAME",
            with_event_metadata=True,
        ),
        milestone_index=index,
    )
    assert result.sport is Sport.UNRESOLVED
    assert result.is_routable is False


# ================= malformed event metadata (fail-closed) ====================

@pytest.mark.parametrize("bad", [123, 12.5, [], {}, ["Pro Football"], {"name": "Pro Football"}, True])
def test_wrong_typed_competition_is_malformed_not_absent(bad):
    result = classify_market(
        MarketContext(
            market_ticker="KXTEST-SYNTH01-AAA",
            market=make_market("KXTEST-SYNTH01-AAA", "KXTEST-SYNTH01"),
            event_metadata={"competition": bad, "competition_scope": None},
        )
    )
    assert result.sport is Sport.UNRESOLVED
    assert result.unresolved_reason is UnresolvedReason.MALFORMED_EVENT_METADATA


@pytest.mark.parametrize("bad", [123, [], {}, False])
def test_wrong_typed_competition_scope_is_malformed(bad):
    result = classify_market(
        MarketContext(
            market_ticker="KXTEST-SYNTH01-AAA",
            market=make_market("KXTEST-SYNTH01-AAA", "KXTEST-SYNTH01"),
            event_metadata={"competition": "Pro Baseball", "competition_scope": bad},
        )
    )
    assert result.sport is Sport.UNRESOLVED
    assert result.unresolved_reason is UnresolvedReason.MALFORMED_EVENT_METADATA


def test_malformed_competition_cannot_be_rescued_by_series_metadata():
    # A wrong-typed competition on a market series metadata would otherwise resolve.
    result = classify_market(
        MarketContext(
            market_ticker="KXMLBGAME-SYNTH01-NYY",
            market=make_market("KXMLBGAME-SYNTH01-NYY", "KXMLBGAME-SYNTH01"),
            event=make_event("KXMLBGAME-SYNTH01", "KXMLBGAME"),
            series=make_series("KXMLBGAME", category="Sports", tags=["MLB"]),
            event_metadata={"competition": 123},
        )
    )
    assert result.sport is Sport.UNRESOLVED
    assert result.unresolved_reason is UnresolvedReason.MALFORMED_EVENT_METADATA
    assert result.evidence == ()


def test_malformed_competition_cannot_be_rescued_by_the_registry():
    result = classify_market(
        MarketContext(
            market_ticker="KXNFLGAME-SYNTH01-KC",
            market=make_market("KXNFLGAME-SYNTH01-KC", "KXNFLGAME-SYNTH01"),
            event=make_event("KXNFLGAME-SYNTH01", "KXNFLGAME"),
            event_metadata={"competition": []},
        )
    )
    assert result.sport is Sport.UNRESOLVED
    assert result.unresolved_reason is UnresolvedReason.MALFORMED_EVENT_METADATA


def test_null_competition_is_valid_and_falls_through():
    result = classify_market(
        MarketContext(
            market_ticker="KXMLBGAME-SYNTH01-NYY",
            market=make_market("KXMLBGAME-SYNTH01-NYY", "KXMLBGAME-SYNTH01"),
            series=make_series("KXMLBGAME", category="Sports", tags=["MLB"]),
            event_metadata={"competition": None, "competition_scope": None},
        )
    )
    assert result.sport is Sport.MLB


def test_valid_string_competition_is_accepted():
    result = classify_market(
        MarketContext(
            market_ticker="KXTEST-SYNTH01-AAA",
            market=make_market("KXTEST-SYNTH01-AAA", "KXTEST-SYNTH01"),
            event_metadata={"competition": "Pro Baseball", "competition_scope": "Game"},
        )
    )
    assert result.sport is Sport.MLB


def test_empty_string_competition_is_treated_as_an_absence_not_corruption():
    """An empty string is still the documented type and asserts no competition."""
    result = classify_market(
        MarketContext(
            market_ticker="KXMLBGAME-SYNTH01-NYY",
            market=make_market("KXMLBGAME-SYNTH01-NYY", "KXMLBGAME-SYNTH01"),
            series=make_series("KXMLBGAME", category="Sports", tags=["MLB"]),
            event_metadata={"competition": "   ", "competition_scope": None},
        )
    )
    assert result.sport is Sport.MLB
    assert result.unresolved_reason is None


def test_malformed_metadata_error_never_echoes_the_value():
    result = classify_market(
        MarketContext(
            market_ticker="KXTEST-SYNTH01-AAA",
            market=make_market("KXTEST-SYNTH01-AAA", "KXTEST-SYNTH01"),
            event_metadata={"competition": ["Secret Competition"]},
        )
    )
    assert "Secret Competition" not in result.reason
