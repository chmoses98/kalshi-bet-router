"""Sport classification: evidence hierarchy and the fail-closed rule."""

from __future__ import annotations

import pytest

from kalshi_router.classify import (
    EvidenceStrength,
    MarketContext,
    classify_market,
    derive_series_ticker,
)
from kalshi_router.sports import Sport

from .synthetic import make_event, make_market, make_series


def context(
    market_ticker="KXTEST-SYNTH01-AAA",
    event_ticker="KXTEST-SYNTH01",
    series_ticker="KXTEST",
    series_extra=None,
    event_extra=None,
    market_extra=None,
    **kwargs,
):
    return MarketContext(
        market_ticker=market_ticker,
        market=make_market(market_ticker, event_ticker, **(market_extra or {})),
        event=make_event(event_ticker, series_ticker, **(event_extra or {})),
        series=make_series(series_ticker, **(series_extra or {})),
        **kwargs,
    )


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


def test_conflicting_authoritative_evidence_is_unresolved():
    result = classify_market(
        context(
            market_ticker="KXNFLGAME-SYNTH01-KC",
            event_ticker="KXNFLGAME-SYNTH01",
            series_ticker="KXNFLGAME",
            series_extra={"category": "Sports", "tags": ["Tennis"]},
        )
    )
    assert result.sport is Sport.UNRESOLVED
    assert "conflicting" in result.reason


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
