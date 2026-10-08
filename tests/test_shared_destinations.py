"""NBA, SOCCER and TENNIS as routed destinations (2026-10-02, app-readiness pass). ACCOUNTING ONLY.

Classification for all three (and that every one of the seven sports classifies from real-shaped evidence), the
shared row builder, the three destination profiles, settlement rows, receipt normalisation -- and, when the
sibling checkouts exist, each destination's own importers and validator run end to end on router-built payloads:
import, byte-identical re-import as DUPLICATE_NOOP, settlement import and re-import, orphan refusal, containment.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from kalshi_router.classify import EvidenceLevel, MarketContext, classify_market
from kalshi_router.competitions import possible_sports, sport_from_competition, sport_from_taxonomy_sport
from kalshi_router.destination import ROUTER_IMPORT_BATCH_ID, write_payloads, write_settlement_payloads
from kalshi_router.destinations import PROFILES, describe, profile_for, render_command, routable_sport_names
from kalshi_router.production import (
    ROW_BUILDERS, SHARED_LEDGER_ROW_FIELDS, SHARED_LEDGER_SPORTS, OrderFinality, ProductionWager,
    to_nhl_import_row,
)
from kalshi_router.receipts import normalise
from kalshi_router.series_registry import lookup_series_ticker
from kalshi_router.settlement import ECONOMICS_V2, SETTLED, WagerSettlement
from kalshi_router.sports import REPORT_ORDER, ROUTABLE_SPORTS, Sport
from kalshi_router.taxonomy import parse_filters_by_sport

from .synthetic import make_event, make_event_metadata, make_market, make_series, make_taxonomy

ROOT = Path(__file__).resolve().parents[1]

#: A live-like taxonomy: the headings and competitions each repository's probes have seen.
LIVE_LIKE = parse_filters_by_sport(make_taxonomy({
    "Baseball": ["Japan NPB", "Korea KBO", "Pro Baseball"],
    "Football": ["CFL", "NCAA Football", "Pro Football"],
    "Hockey": ["Czech Extraliga", "KHL", "Pro Hockey", "SHL"],
    "Basketball": ["Pro Basketball (M)", "Pro Basketball (W)", "College Basketball (M)", "EuroLeague"],
    "Soccer": ["Premier League", "La Liga", "MLS", "UEFA Champions League", "Brasileirão Série A",
               "CONCACAF Nations League", "International Friendly"],
    "Tennis": ["US Open Men Singles", "ATP Shanghai", "WTA Beijing"],
    "Golf": ["PGA Tour"],
}))

REPO_DIRS = {
    "NBA": ("NBA_EDGE_FINDER_DIR", "nba-edge-finder"),
    "SOCCER": ("SOCCER_EDGE_FINDER_DIR", "soccer-edge-finder"),
    "TENNIS": ("TENNIS_EDGE_FINDER_DIR", "Tennis-Edge-Finder"),
}


def ctx(competition=None, series="KXNBAGAME", event="KXNBAGAME-26OCT03MIATOR", market=None, series_extra=None):
    market = market or f"{event}-TOR"
    return MarketContext(
        market_ticker=market, market=make_market(market, event), event=make_event(event, series),
        series=make_series(series, **(series_extra or {})),
        event_metadata=make_event_metadata(competition) if competition is not None else None,
    )


# ------------------------------------------------------------------------------------------------ vocabulary
def test_all_seven_sports_are_routable_reported_and_profiled():
    for sport in (Sport.MLB, Sport.CFB, Sport.NFL, Sport.NBA, Sport.NHL, Sport.SOCCER, Sport.TENNIS):
        assert sport in ROUTABLE_SPORTS and sport in REPORT_ORDER and sport in PROFILES
        assert sport.value in routable_sport_names() and sport.value in ROW_BUILDERS
    assert Sport.OTHER not in PROFILES and Sport.UNRESOLVED not in PROFILES


# ------------------------------------------------------------------------------------------- classification
@pytest.mark.parametrize("competition", ["Pro Basketball (M)", "NBA", "National Basketball Association", " nba "])
def test_nba_competitions_resolve_to_nba(competition):
    assert sport_from_competition(competition) is Sport.NBA
    c = classify_market(ctx(competition), taxonomy=LIVE_LIKE)
    assert c.sport is Sport.NBA and c.resolved_by is EvidenceLevel.L1_EVENT_COMPETITION


@pytest.mark.parametrize("competition", ["Pro Basketball (W)", "WNBA", "College Basketball (M)", "NCAAB"])
def test_non_nba_basketball_is_positively_other(competition):
    assert sport_from_competition(competition) is Sport.OTHER
    assert classify_market(ctx(competition), taxonomy=LIVE_LIKE).sport is Sport.OTHER


def test_an_unknown_basketball_competition_is_unresolved_not_nba():
    # EuroLeague is under the Basketball heading; the heading narrows to {NBA} but never resolves to it.
    c = classify_market(ctx("EuroLeague"), taxonomy=LIVE_LIKE)
    assert c.sport is Sport.UNRESOLVED
    assert possible_sports("Basketball") == frozenset({Sport.NBA})
    assert sport_from_taxonomy_sport("Basketball") is None


@pytest.mark.parametrize("competition", ["Premier League", "La Liga", "MLS", "UEFA Champions League",
                                         "Brasileirão Série A", "CONCACAF Nations League", "International Friendly"])
def test_soccer_resolves_at_the_sport_level_from_the_taxonomy(competition):
    c = classify_market(ctx(competition, series="KXEPLGAME", event="KXEPLGAME-26OCT04ARSTOT", market="KXEPLGAME-26OCT04ARSTOT-ARS"),
                        taxonomy=LIVE_LIKE)
    assert c.sport is Sport.SOCCER, c.reason
    assert sport_from_taxonomy_sport("Soccer") is Sport.SOCCER


@pytest.mark.parametrize("competition", ["La Liga", "MLS", "UEFA Champions League", "Brasileirão Série A", "Liga MX"])
def test_soccer_competition_tokens_resolve_without_the_taxonomy(competition):
    assert sport_from_competition(competition) is Sport.SOCCER


def test_ambiguous_soccer_names_stay_unresolved_without_the_taxonomy():
    # "Premier League" alone could be the Indian Premier League (cricket); only the Soccer heading settles it.
    assert sport_from_competition("Premier League") is None
    assert classify_market(ctx("Premier League", series="KXEPLGAME")).sport is Sport.UNRESOLVED


@pytest.mark.parametrize("competition", ["ATP Shanghai", "WTA Beijing", "US Open Men Singles"])
def test_tennis_resolves(competition):
    c = classify_market(ctx(competition, series="KXATPMATCH", event="KXATPMATCH-26OCT02HURGEA", market="KXATPMATCH-26OCT02HURGEA-HUR"),
                        taxonomy=LIVE_LIKE)
    assert c.sport is Sport.TENNIS


def test_american_football_is_untouched_by_soccer():
    """The Football family still means NFL/CFB and still fails closed without a league."""
    assert possible_sports("Football") == frozenset({Sport.NFL, Sport.CFB})
    bare = classify_market(ctx(None, series="KXTEST", event="KXTEST-SYNTH01", series_extra={"tags": ["Football"]}))
    assert bare.sport is Sport.UNRESOLVED
    assert classify_market(ctx("Pro Football", series="KXNFLGAME"), taxonomy=LIVE_LIKE).sport is Sport.NFL
    assert classify_market(ctx("College Football", series="KXNCAAFGAME"), taxonomy=LIVE_LIKE).sport is Sport.CFB


def test_series_tags_resolve_nba_and_soccer_but_a_bare_basketball_tag_does_not():
    # A synthetic series ticker, so the L5 registry stays out of it and only the tags decide.
    def tagged(*tags):
        return classify_market(ctx(None, series="KXTEST", event="KXTEST-SYNTH01", series_extra={"tags": list(tags)}))
    assert tagged("Basketball", "NBA").sport is Sport.NBA
    assert tagged("Soccer").sport is Sport.SOCCER
    assert tagged("Basketball").sport is Sport.UNRESOLVED
    assert tagged("WNBA").sport is Sport.OTHER


def test_registry_knows_the_live_series_and_never_outranks_a_competition():
    assert lookup_series_ticker("KXNBAGAME").sport is Sport.NBA and lookup_series_ticker("KXNBAGAME").verified
    assert lookup_series_ticker("KXEPLGAME").sport is Sport.SOCCER
    assert lookup_series_ticker("KXMLSGAME").sport is Sport.SOCCER
    assert lookup_series_ticker("KXAFCACGAME") is None  # the exchange mis-tags this one; deliberately absent
    assert classify_market(ctx("Pro Hockey", series="KXNBAGAME"), taxonomy=LIVE_LIKE).sport is Sport.NHL


# --------------------------------------------------------------------------------------- every sport, one path
EVERY_SPORT = {
    "MLB": ("Pro Baseball", "KXMLBGAME-26OCT011400PHIATL-ATL"),
    "CFB": ("College Football", "KXNCAAFGAME-26OCT03OSUIOWA-IOWA"),
    "NFL": ("Pro Football", "KXNFLGAME-26OCT05KCLAC-KC"),
    "NBA": ("Pro Basketball (M)", "KXNBAGAME-26OCT03MIATOR-TOR"),
    "NHL": ("Pro Hockey", "KXNHLGAME-26OCT02NYRDET-NYR"),
    "SOCCER": ("Premier League", "KXEPLGAME-26OCT04ARSTOT-ARS"),
    "TENNIS": ("ATP Shanghai", "KXATPMATCH-26OCT02HURGEA-HUR"),
}


@pytest.mark.parametrize("sport", sorted(EVERY_SPORT))
def test_every_sport_classifies_and_has_a_destination(sport):
    competition, market = EVERY_SPORT[sport]
    event = market.rsplit("-", 1)[0]
    series = event.split("-", 1)[0]
    c = classify_market(ctx(competition, series=series, event=event, market=market), taxonomy=LIVE_LIKE)
    assert c.sport.value == sport, c.reason
    prof = profile_for(sport)
    assert prof.repo.startswith("chmoses98/")
    assert sport in ROW_BUILDERS


# ---------------------------------------------------------------------------------------------- row builder
def production_wager(sport="NBA", key="kalshi:v1:" + "ab" * 32, ticker="KXNBAGAME-26OCT03MIATOR-TOR"):
    return ProductionWager(
        source_key=key, market_ticker=ticker, sport=sport, game_date="2026-10-03", side="YES",
        contracts=Decimal("25"), vwap_price=Decimal("0.57"), total_fees=Decimal("0.36"), stake=Decimal("14.61"),
        first_execution_time=Decimal("1791000000"), last_execution_time=Decimal("1791000001"), fill_count=1,
        finality=OrderFinality.FINAL_MARKET_CLOSED, execution_action="BUY",
    )


@pytest.mark.parametrize("sport", SHARED_LEDGER_SPORTS)
def test_shared_ledger_rows_have_exactly_the_nhl_shape(sport):
    row = ROW_BUILDERS[sport](production_wager(sport=sport), ROUTER_IMPORT_BATCH_ID)
    assert tuple(row) == SHARED_LEDGER_ROW_FIELDS
    nhl = to_nhl_import_row(production_wager(sport="NHL"), ROUTER_IMPORT_BATCH_ID)
    assert {k: v for k, v in row.items()} == {k: v for k, v in nhl.items()}
    assert row["stake"] == pytest.approx(25 * 0.57 + 0.36)
    assert row["fees_are_estimated"] is False and row["venue"] == "kalshi"
    for forbidden in ("recommendation_id", "model_probability", "fair_probability", "wager_id", "season", "week"):
        assert forbidden not in row
    with pytest.raises(ValueError):
        ROW_BUILDERS[sport](production_wager(sport="NHL"), ROUTER_IMPORT_BATCH_ID)
    with pytest.raises(ValueError):
        ROW_BUILDERS[sport](production_wager(sport=sport), "")


# -------------------------------------------------------------------------------------------------- profiles
@pytest.mark.parametrize("sport", SHARED_LEDGER_SPORTS)
def test_profiles_describe_the_shared_ledger_contract(sport):
    prof = profile_for(sport)
    assert prof.ledger_branch == "accounting-data" and prof.code_branch == "main"
    assert prof.mergeable_paths == frozenset({"data/accounting/wagers.jsonl", "data/accounting/settlements.jsonl"})
    assert prof.committable_prefixes == ("data/accounting/",)
    assert prof.ledger_branch_runs_ci is False and prof.ledger_validator is not None
    assert prof.settlement_importer is not None and prof.settlement_economics == ECONOMICS_V2
    assert prof.requires_season is False
    # Observation closes per destination, on its own evidence: SOCCER on 2026-10-08 (docs/DESTINATIONS.md).
    assert prof.auto_merge is (sport == "SOCCER")
    d = describe(sport)
    assert d["needs_separate_code_checkout"] and d["has_ledger_validator"]
    cmd = render_command(prof.wager_importer, payload="/p.json", work="/w", code="/c", receipts="/r.json")
    assert cmd == ["python", "/c/scripts/accounting/import_routed_wagers.py", "--payload", "/p.json",
                   "--base-dir", "/w", "--receipts-out", "/r.json"]


def test_payloads_are_written_per_sport_and_only_for_routable_sports(tmp_path):
    counts = write_payloads([production_wager(sport=s, key=f"kalshi:v1:{i:064x}", ticker=t)
                             for i, (s, t) in enumerate((s, m) for s, (_, m) in EVERY_SPORT.items())], str(tmp_path))
    assert counts == {s: 1 for s in EVERY_SPORT}
    nba = json.loads((tmp_path / "NBA.json").read_text())
    assert nba["importBatchId"] == ROUTER_IMPORT_BATCH_ID and nba["rows"][0]["import_batch_id"] == ROUTER_IMPORT_BATCH_ID


# ------------------------------------------------------------------------------------------------ settlement
def settlement(sport="NBA", source_bet_key="kalshi:v1:" + "ab" * 32, ticker="KXNBAGAME-26OCT03MIATOR-TOR"):
    return WagerSettlement(source_bet_key=source_bet_key, market_ticker=ticker, side="YES", settlement_status=SETTLED,
                           settled_at="2026-10-04T03:00:00Z", result="WON", gross_return=Decimal("25"),
                           net_profit_loss=Decimal("10.39"), refusals=(), economics_version=ECONOMICS_V2)


def test_settlement_payloads_carry_v2_economics(tmp_path):
    counts = write_settlement_payloads([settlement()], str(tmp_path), {settlement().source_bet_key: "NBA"})
    assert counts == {"NBA": 1}
    row = json.loads((tmp_path / "NBA-settlements.json").read_text())["settlements"][0]
    assert row["economics_version"] == ECONOMICS_V2 and row["net_profit_loss"] == 10.39


# ------------------------------------------------------------------ end to end with the destinations' own code
def _checkout(sport: str) -> Path | None:
    env, dirname = REPO_DIRS[sport]
    for cand in (os.environ.get(env), ROOT.parent / dirname):
        if cand and (Path(cand) / "scripts" / "accounting" / "import_routed_wagers.py").exists():
            return Path(cand)
    return None


@pytest.mark.parametrize("sport", SHARED_LEDGER_SPORTS)
def test_end_to_end_with_the_destination_importers(tmp_path, sport):
    code = _checkout(sport)
    if code is None:
        pytest.skip(f"no {REPO_DIRS[sport][1]} checkout (set {REPO_DIRS[sport][0]}); CI proves the router side only")
    prof = profile_for(sport)
    ticker = EVERY_SPORT[sport][1]
    work = tmp_path / "accounting-data"
    work.mkdir()
    payload_dir = tmp_path / "out"
    wager = production_wager(sport=sport, ticker=ticker)
    stl = settlement(sport=sport, ticker=ticker)
    write_payloads([wager], str(payload_dir))
    write_settlement_payloads([stl], str(payload_dir), {stl.source_bet_key: sport})

    def run(template, payload, receipts):
        cmd = render_command(template, payload=str(payload), work=str(work), code=str(code), receipts=str(receipts))
        cmd[0] = sys.executable
        return subprocess.run(cmd, capture_output=True, text=True)

    def ledger_bytes():
        return tuple((work / p).read_bytes() for p in sorted(prof.mergeable_paths) if (work / p).exists())

    r1 = run(prof.wager_importer, payload_dir / f"{sport}.json", tmp_path / "w1.json")
    assert r1.returncode == 0, r1.stdout + r1.stderr
    after_first = ledger_bytes()
    r2 = run(prof.wager_importer, payload_dir / f"{sport}.json", tmp_path / "w2.json")
    assert r2.returncode == 0 and ledger_bytes() == after_first
    w1, w2 = (normalise(json.loads((tmp_path / f).read_text())) for f in ("w1.json", "w2.json"))
    assert [x.verdict for x in w1] == ["NEW"] and [x.verdict for x in w2] == ["DUPLICATE_NOOP"]
    assert w1[0].identity == w2[0].identity and w1[0].identity.startswith(f"{sport[:3].lower()}w-")

    s1 = run(prof.settlement_importer, payload_dir / f"{sport}-settlements.json", tmp_path / "s1.json")
    assert s1.returncode == 0, s1.stdout + s1.stderr
    after_settle = ledger_bytes()
    s2 = run(prof.settlement_importer, payload_dir / f"{sport}-settlements.json", tmp_path / "s2.json")
    assert s2.returncode == 0 and ledger_bytes() == after_settle
    assert [x.verdict for x in normalise(json.loads((tmp_path / "s2.json").read_text()))] == ["DUPLICATE_NOOP"]

    orphan_dir = tmp_path / "orphan"
    orphan = settlement(sport=sport, source_bet_key="kalshi:v1:" + "cd" * 32, ticker=ticker)
    write_settlement_payloads([orphan], str(orphan_dir), {orphan.source_bet_key: sport})
    o = run(prof.settlement_importer, orphan_dir / f"{sport}-settlements.json", tmp_path / "o.json")
    assert o.returncode == 1 and ledger_bytes() == after_settle
    assert [x.verdict for x in normalise(json.loads((tmp_path / "o.json").read_text()))] == ["REFUSED"]

    v = run(prof.ledger_validator, "", tmp_path / "v.json")
    assert v.returncode == 0, v.stdout
    written = {str(p.relative_to(work)) for p in work.rglob("*") if p.is_file()}
    assert written == set(prof.mergeable_paths)
    for text in (r1.stdout, r2.stdout, s1.stdout, o.stdout, v.stdout):
        for secret in (ticker, "14.61", "0.57", "10.39", "ab" * 32):
            assert secret not in text
