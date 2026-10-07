"""One realistic record of every kind for every sport. Tickers follow the real series each
repository captures; identities come from the strongest provider id each repository holds."""

from __future__ import annotations

from edge_finder_contract import build, ids

NOW = "2026-10-02T15:00:00Z"
CAPTURED = "2026-10-02T14:54:00Z"
MODELLED = "2026-10-02T14:40:00Z"

SPORT_CASES = {
    "MLB": dict(source="mlb_game_pk", source_id="849844", league="MLB", season="2026", competition="Regular Season",
                home=("TEAM", "mlb_team_abbr", "ATL", "Atlanta Braves", "ATL"),
                away=("TEAM", "mlb_team_abbr", "PHI", "Philadelphia Phillies", "PHI"),
                start="2026-10-02T00:00:00Z", ticker="KXMLBGAME-26OCT011400PHIATL-ATL", family="game_result",
                yes="Atlanta wins", repo="chmoses98/edge-finder-api", source_bet_key="kalshi:v1:" + "a" * 64),
    "CFB": dict(source="kalshi_milestone_id", source_id="1d99030a-0000-4000-8000-000000000001", league="NCAAF",
                season="2026", competition="Week 5",
                home=("TEAM", "kalshi_team_code", "IOWA", "Iowa Hawkeyes", "IOWA"),
                away=("TEAM", "kalshi_team_code", "OSU", "Ohio State Buckeyes", "OSU"),
                start="2026-10-03T19:30:00Z", ticker="KXNCAAFSPREAD-26OCT03OSUIOWA-IOWA10", family="game_spread",
                yes="Iowa wins by over 9.5 points", repo="chmoses98/cfb-edge-finder", source_bet_key="kalshi:v1:" + "b" * 64),
    "NFL": dict(source="nflverse_game_id", source_id="2026_04_PIT_CLE", league="NFL", season="2026", competition="Week 4",
                home=("TEAM", "nflverse_team", "CLE", "Cleveland Browns", "CLE"),
                away=("TEAM", "nflverse_team", "PIT", "Pittsburgh Steelers", "PIT"),
                start="2026-10-02T00:15:00Z", ticker="KXNFLSPREAD-26OCT01PITCLE-CLE10", family="spread",
                yes="YES iff CLE margin (FULL) > 9.5", repo="chmoses98/nfl-edge-finder", source_bet_key="kalshi:v1:" + "c" * 64),
    "NBA": dict(source="espn_event_id", source_id="401902644", league="NBA", season="2026-27", competition="preseason",
                home=("TEAM", "nba_team_id", 1610612761, "Toronto Raptors", "TOR"),
                away=("TEAM", "nba_team_id", 1610612748, "Miami Heat", "MIA"),
                start="2026-10-03T23:00:00Z", ticker="KXNBAGAME-26OCT03MIATOR-TOR", family="game_winner",
                yes="Toronto wins", repo="chmoses98/nba-edge-finder", source_bet_key="kalshi:v1:" + "d" * 64),
    "NHL": dict(source="nhl_game_id", source_id="2026020017", league="NHL", season="2026-27", competition="regular",
                home=("TEAM", "nhl_team_id", 17, "Detroit Red Wings", "DET"),
                away=("TEAM", "nhl_team_id", 3, "New York Rangers", "NYR"),
                start="2026-10-02T22:30:00Z", ticker="KXNHLGAME-26OCT02NYRDET-NYR", family="game_winner",
                yes="New York Rangers win", repo="chmoses98/NHL-edge-finder", source_bet_key="kalshi:v1:" + "e" * 64),
    "SOCCER": dict(source="fixture_id", source_id="fx:bra.serie_a:2026:bra.atletico_mineiro:bra.bragantino",
                   league="Brasileirão Série A", season="2026", competition="bra.serie_a",
                   home=("TEAM", "team_id", "bra.atletico_mineiro", "Atlético Mineiro", "CAM"),
                   away=("TEAM", "team_id", "bra.bragantino", "Red Bull Bragantino", "RBB"),
                   start="2026-10-03T22:00:00Z", ticker="KXBRASILEIROBTTS-26OCT03ATLRBB-BTTS", family="btts",
                   yes="Both teams score", repo="chmoses98/soccer-edge-finder", source_bet_key="kalshi:v1:" + "f" * 64),
    "TENNIS": dict(source="kalshi_event_ticker", source_id="KXATPMATCH-26OCT02HURGEA", league="ATP",
                   season="2026", competition="ATP Shanghai",
                   home=None, away=None,
                   players=[("PLAYER", "sackmann_id", "208169", "Hubert Hurkacz", "Hurkacz"),
                            ("PLAYER", "sackmann_id", "200282", "Marcos Giron", "Giron")],
                   start="2026-10-02T06:00:00Z", ticker="KXATPMATCH-26OCT02HURGEA-HUR", family="match_winner",
                   yes="Hurkacz wins the match", repo="chmoses98/Tennis-Edge-Finder", source_bet_key="kalshi:v1:" + "0" * 64),
    # contract 1.2.0: NCAA D-I men's basketball (canonical game id = "G" + ESPN event id)
    "CBB": dict(source="cbb_game_id", source_id="G401920982", league="NCAA D-I", season="2026-27",
                competition="Eternal City Tip-Off",
                home=("TEAM", "cbb_team_id", "T0352", "Villanova Wildcats", "NOVA"),
                away=("TEAM", "cbb_team_id", "T0230", "Notre Dame Fighting Irish", "ND"),
                start="2026-10-03T14:30:00Z", ticker="KXNCAAMBGAME-26OCT03NDNOVA-NOVA", family="game_winner",
                yes="Villanova wins", repo="chmoses98/cbb-edge-finder", source_bet_key="kalshi:v1:" + "1" * 64),
}


def bundle_for(sport: str) -> dict[str, list[dict]]:
    case = SPORT_CASES[sport]
    run_id = ids.run_id(sport, case["repo"], "run-test", generated_at=NOW)
    parts = []
    specs = case.get("players") or [case["home"], case["away"]]
    for ptype, src, sid, name, short in specs:
        parts.append(build.participant(sport=sport, participant_type=ptype, source=src, source_id=sid,
                                       display_name=name, short_name=short))
    home = parts[0]["participant_id"] if case.get("home") else None
    away = parts[1]["participant_id"] if case.get("away") else None
    ev = build.event(sport=sport, source=case["source"], source_id=case["source_id"], start_time_utc=case["start"],
                     participants=parts, home_participant=home, away_participant=away, league=case["league"],
                     season=case["season"], competition=case["competition"], status="SCHEDULED",
                     start_time_source=case["source"], start_time_confidence="SCHEDULED",
                     schedule_updated_at=CAPTURED, last_updated_at=NOW)
    mk = build.market(sport=sport, kalshi_ticker=case["ticker"], market_family=case["family"],
                      yes_description=case["yes"], source="test-capture", event_id=ev["event_id"],
                      participant_id=home or parts[0]["participant_id"], side="HOME" if home else "PARTICIPANT",
                      line=9.5 if "SPREAD" in case["ticker"] else None, yes_bid=0.44, yes_ask=0.46, no_bid=0.54,
                      no_ask=0.56, last_price=0.45, volume=1200, open_interest=800, market_status="OPEN",
                      close_time_utc="2026-10-05T00:00:00Z", captured_at=CAPTURED,
                      raw_market_reference="tests/contract/fixtures.py")
    mp = build.model_price(run_id=run_id, market_id=mk["market_id"], event_id=ev["event_id"], fair_probability=0.52,
                           generated_at=MODELLED, model_version="test-model-1.0", lower_bound=0.47, upper_bound=0.57,
                           market_probability=mk["market_probability"], inputs_as_of=CAPTURED,
                           freshness_status="FRESH", data_quality_status="OK", support_status="OK")
    th = build.thesis(sport=sport, run_id=run_id, event_id=ev["event_id"], generated_at=MODELLED,
                      summary=None, supporting_factors=["model edge 7pts"], evidence={"fair": 0.52})
    rec = build.recommendation(sport=sport, source_repo=case["repo"], event_id=ev["event_id"], market_id=mk["market_id"],
                               run_id=run_id, selection="YES", market_description=case["yes"], created_at=MODELLED,
                               status="RESEARCH_CANDIDATE", authority="RESEARCH_ONLY", research_only=True,
                               current_probability=mk["market_probability"], current_price=0.46, fair_probability=0.52,
                               edge=0.07, bet_up_to_price=0.50, thesis_id=th["thesis_id"], data_freshness="FRESH")
    wg = build.wager(sport=sport, kalshi_ticker=case["ticker"], selection="YES", contracts=10, stake=4.70,
                     average_price=0.46, fees=0.10, placed_at="2026-10-02T14:58:00Z", source="KALSHI_ROUTER",
                     destination_repo=case["repo"], source_bet_key=case["source_bet_key"], event_id=ev["event_id"],
                     side="BUY", router_ingested_at="2026-10-02T15:10:00Z", settlement_status="PENDING")
    st = build.settlement(wager_id=wg["wager_id"], market_id=mk["market_id"], result="WON", winning_side="YES",
                          settlement_value=1.0, settled_at="2026-10-04T03:00:00Z", source="kalshi",
                          verification_status="EXCHANGE_CONFIRMED", gross_payout=10.0, fees=0.10, net_pnl=5.30)
    wg["settlement_status"], wg["settlement_id"], wg["payout"], wg["profit_loss"] = "SETTLED", st["settlement_id"], 10.0, 5.30
    rn = build.run(sport=sport, repo=case["repo"], native_run_id="run-test", completed_at=NOW, scope="test slate",
                   commit_sha="deadbeef", model_version="test-model-1.0", events_processed=1, markets_discovered=1,
                   markets_priced=1, recommendations_created=1, data_sources=["test"], input_freshness={"kalshi": CAPTURED})
    assert rn["run_id"] == run_id
    return {"events": [ev], "markets": [mk], "model_prices": [mp], "theses": [th], "recommendations": [rec],
            "wagers": [wg], "settlements": [st], "runs": [rn]}


def documents_for(sport: str) -> tuple[str, dict[str, dict]]:
    items = bundle_for(sport)
    run_id = items["runs"][0]["run_id"]
    return run_id, {kind: build.collection(kind, sport, run_id, NOW, rows) for kind, rows in items.items()}
