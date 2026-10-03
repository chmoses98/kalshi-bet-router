"""A small but complete research graph for every sport, built on the v1 fixture bundle: a metric
registry, a ranking, observations with context, time series, profiles, event research, market history,
a capability manifest and a search index. Published into a temp app root beside the v1 documents so
the packet builder and the verifiers can be exercised end to end."""

from __future__ import annotations

from pathlib import Path

from edge_finder_contract import build, health, publish, research as R
from tests.contract.fixtures import CAPTURED, NOW, SPORT_CASES, bundle_for, documents_for

AUDIT_DATE = "2026-10-03"


def _third_participant(sport: str) -> dict:
    case = SPORT_CASES[sport]
    if case.get("players"):
        return build.participant(sport=sport, participant_type="PLAYER", source="sackmann_id", source_id="999001",
                                 display_name="Casper Ruud", short_name="Ruud")
    ptype, src, _, _, _ = case["home"]
    return build.participant(sport=sport, participant_type=ptype, source=src, source_id="THIRD",
                             display_name="Third Team", short_name="THD")


def research_documents(sport: str, *, mark_research: bool = False) -> tuple[str, list[dict], dict]:
    """(run_id, explorer documents, index quality). ``mark_research`` flips one metric to RESEARCH so
    tests can check that the status survives into profiles and packets."""
    items = bundle_for(sport)
    run_id = items["runs"][0]["run_id"]
    ev, mk, mp = items["events"][0], items["markets"][0], items["model_prices"][0]
    parts = list(ev["participants"]) + [_third_participant(sport)]
    etype = parts[0]["participant_type"]
    q_ok = R.quality(status="VERIFIED", source="fixture dataset", generated_at=NOW, production=True, data_as_of=CAPTURED,
                     coverage="2026 fixture", sample_size=3)
    q_part = R.quality(status="PARTIAL", source="fixture captures", generated_at=NOW, production=True, data_as_of=CAPTURED,
                       limitations=["one event only"])
    q_res = R.quality(status="RESEARCH", source="fixture research", generated_at=NOW, production=False,
                      limitations=["research notebook output"])
    m_eff = R.metric(sport=sport, slug="efficiency_index", name="Efficiency index", short_name="EFF",
                     description="a fixture efficiency rate", entity_type=etype, category="overall", stat_type="RATE",
                     source="fixture dataset", quality=q_ok, freshness="FRESH", higher_is_better=True,
                     comparison_universe=f"{sport} fixture universe", supports=R.supports(rank=True, percentile=True, time_series=True, windows=True),
                     windows=["SEASON", "L3"], update_frequency="per run", historical_start="2026-09-01")
    m_res = R.metric(sport=sport, slug="experimental_rating", name="Experimental rating", short_name="EXP",
                     description="a research-only rating", entity_type=etype, category="research", stat_type="RATING",
                     source="fixture research", quality=q_res, freshness="STALE", higher_is_better=True,
                     supports=R.supports(rank=False), known_limitations=["not production"], update_frequency="ad hoc")
    metrics = [m_eff, m_res]
    registry = R.metric_registry(sport=sport, run_id=run_id, generated_at=NOW, metrics=metrics)
    w_season = R.window("SEASON")
    values = [{"entity_id": p["participant_id"], "display_name": p["display_name"], "short_name": p["short_name"],
               "value": v, "sample_size": 3} for p, v in zip(parts, [0.31, 0.25, 0.25])]
    pfor = R.player_path if etype == "PLAYER" else R.team_path
    rk = R.ranking(sport=sport, metric_id=m_eff["metric_id"], universe_label=f"{sport} fixture universe", entity_type=etype,
                   window=w_season, as_of=CAPTURED, higher_is_better=True, values=values, run_id=run_id, generated_at=NOW,
                   quality=q_ok, season=ev["season"], path_for=pfor)
    docs: list[dict] = [registry, rk]
    profiles = {}
    series_docs = []
    for p, v in zip(parts, [0.31, 0.25, 0.25]):
        pid = p["participant_id"]
        ctx = R.context_from_ranking(rk, pid)
        obs = R.observation(sport=sport, metric_id=m_eff["metric_id"], entity_id=pid, entity_type=etype, value=v, window=w_season,
                            as_of=CAPTURED, source="fixture dataset", quality_status="VERIFIED", context=ctx, sample_size=3,
                            season=ev["season"])
        obs_l3 = R.observation(sport=sport, metric_id=m_eff["metric_id"], entity_id=pid, entity_type=etype, value=v + 0.02,
                               window=R.window("LAST_N", n=3), as_of=CAPTURED, source="fixture dataset", quality_status="VERIFIED",
                               sample_size=3)
        obs_res = R.observation(sport=sport, metric_id=m_res["metric_id"], entity_id=pid, entity_type=etype, value=v * 100,
                                window=w_season, as_of=CAPTURED, source="fixture research", quality_status="RESEARCH")
        pts = [R.point(x="g1", t="2026-09-14T17:00:00Z", value=v - 0.05, quality_status="VERIFIED", source="fixture dataset"),
               R.point(x="g2", t="2026-09-21T17:00:00Z", value=v, quality_status="VERIFIED", source="fixture dataset"),
               R.point(x="g3", t="2026-09-28T17:00:00Z", value=v + 0.05, quality_status="VERIFIED", source="fixture dataset",
                       event_id=ev["event_id"], path=R.event_path(ev["event_id"]))]
        ser = R.time_series(sport=sport, metric_id=m_eff["metric_id"], entity_id=pid, entity_type=etype, x_axis="GAME",
                            points=R.rolling(pts, 3), as_of=CAPTURED, run_id=run_id, generated_at=NOW, quality=q_ok, rolling_window=3,
                            links=[R.link(rel="TEAM" if etype == "TEAM" else "PLAYER", target_kind="entity_profile", label=p["display_name"],
                                          target_id=pid, path=pfor(pid))])
        series_docs.append(ser)
        in_event = pid in {x["participant_id"] for x in ev["participants"]}
        opp = next((x for x in ev["participants"] if x["participant_id"] != pid), None) if in_event else None
        games = [R.game_ref(event_id=ev["event_id"], start_time_utc=ev["start_time_utc"], status=ev["status"],
                            opponent_id=opp["participant_id"] if opp else None, opponent_name=opp["display_name"] if opp else None,
                            home_away=("HOME" if ev.get("home_participant") == pid else "AWAY") if ev.get("home_participant") else None,
                            path=R.event_path(ev["event_id"]))] if in_event else []
        links = [R.link(rel="RANKING", target_kind="ranking", label=m_eff["name"], target_id=rk["ranking_id"], path=R.ranking_path(rk["ranking_id"])),
                 R.link(rel="SERIES", target_kind="time_series", label=m_eff["name"], target_id=ser["series_id"], path=R.series_path(ser["series_id"]))]
        if in_event:
            links.append(R.link(rel="EVENT", target_kind="event_research", label="next event", target_id=ev["event_id"], path=R.event_path(ev["event_id"])))
        if opp:
            links.append(R.link(rel="OPPONENT", target_kind="entity_profile", label=opp["display_name"], target_id=opp["participant_id"], path=pfor(opp["participant_id"])))
        prof = R.entity_profile(
            sport=sport, run_id=run_id, generated_at=NOW, entity=p, entity_type=etype, quality=q_ok, season=ev["season"], league=ev["league"],
            metrics=[obs, obs_l3, obs_res], series=[{"series_id": ser["series_id"], "metric_id": m_eff["metric_id"], "x_axis": "GAME", "split": None, "path": R.series_path(ser["series_id"])}],
            rankings=[{"ranking_id": rk["ranking_id"], "metric_id": m_eff["metric_id"], "window_label": "SEASON", "split": None, "path": R.ranking_path(rk["ranking_id"])}],
            games=games, opponents=[{"participant_id": opp["participant_id"], "display_name": opp["display_name"], "event_ids": [ev["event_id"]], "path": pfor(opp["participant_id"])}] if opp else [],
            markets=[R.market_ref(mk)] if in_event else [],
            projections=[R.projection_ref(mp, research_only=True, authority="RESEARCH_ONLY", quality_status="VERIFIED")] if in_event else [],
            availability=[{"status": "ACTIVE", "detail": None, "as_of": CAPTURED, "source": "fixture", "event_id": ev["event_id"]}] if in_event else [],
            links=links, extensions={"fixture": True})
        profiles[pid] = prof
        docs.append(prof)
    docs.extend(series_docs)
    home_id, away_id = ev.get("home_participant") or parts[0]["participant_id"], ev.get("away_participant") or parts[1]["participant_id"]
    er = R.event_research(
        sport=sport, run_id=run_id, generated_at=NOW, event=ev, quality=q_ok,
        participants=[{"participant_id": pid, "display_name": profiles[pid]["entity"]["display_name"], "home_away": ha, "path": pfor(pid)}
                      for pid, ha in ((home_id, "HOME" if ev.get("home_participant") else None), (away_id, "AWAY" if ev.get("away_participant") else None))],
        matchup=[{"metric_id": m_eff["metric_id"], "name": m_eff["name"], "home": profiles[home_id]["metrics"][0], "away": profiles[away_id]["metrics"][0], "note": None}],
        projections=[R.projection_ref(mp, research_only=True, authority="RESEARCH_ONLY", quality_status="VERIFIED")],
        distributions=[{"market_id": mk["market_id"], "metric_id": None, "entity_id": None, "label": "fixture margin", "quantiles": {"p05": -10.0, "p50": 1.5, "p95": 12.0},
                        "mean": 1.4, "stdev": 6.5, "samples": 1000, "run_id": run_id, "generated_at": NOW, "source": "fixture simulation", "quality_status": "RESEARCH"}],
        markets=[R.market_ref(mk)], market_history_path=R.market_history_path(ev["event_id"]),
        context={"notes": ["fixture context note"]}, wagers=[items["wagers"][0]["wager_id"]],
        links=[R.link(rel="MARKET_HISTORY", target_kind="market_history", label="price history", target_id=ev["event_id"], path=R.market_history_path(ev["event_id"]))])
    docs.append(er)
    mh = R.market_history(sport=sport, run_id=run_id, generated_at=NOW, event_id=ev["event_id"], as_of=CAPTURED, quality=q_part,
                          series=[{"market_id": mk["market_id"], "kalshi_ticker": mk["kalshi_ticker"], "points": [
                              R.price_point(captured_at=CAPTURED, yes_bid=0.44, yes_ask=0.46, last_price=0.45, source="fixture"),
                              R.price_point(captured_at="2026-10-02T13:54:00Z", yes_bid=0.43, yes_ask=0.45, last_price=0.44, source="fixture")]}],
                          links=[R.link(rel="EVENT_RESEARCH", target_kind="event_research", label="event", target_id=ev["event_id"], path=R.event_path(ev["event_id"]))])
    docs.append(mh)
    profile_cap = "player_profiles" if etype == "PLAYER" else "team_profiles"
    other_cap = "team_profiles" if etype == "PLAYER" else "player_profiles"
    caps = R.capability_manifest(
        sport=sport, run_id=run_id, generated_at=NOW, audit_date=AUDIT_DATE, windows=[w_season, R.window("LAST_N", n=3)],
        capabilities=[
            R.capability(capability=profile_cap, status="VERIFIED", summary="fixture profiles", entity_types=[etype], evidence=[pfor(parts[0]["participant_id"])]),
            R.capability(capability=other_cap, status="UNAVAILABLE", summary="no such entities in the fixture", reasons=["fixture"]),
            R.capability(capability="event_research", status="VERIFIED", summary="fixture event", evidence=[R.event_path(ev["event_id"])]),
            R.capability(capability="rankings", status="VERIFIED", summary="fixture ranking", evidence=[R.ranking_path(rk["ranking_id"])], metrics=[m_eff["metric_id"]]),
            R.capability(capability="time_series", status="VERIFIED", summary="fixture series", evidence=[R.series_path(series_docs[0]["series_id"])], metrics=[m_eff["metric_id"]]),
            R.capability(capability="market_price_history", status="PARTIAL", summary="two captures", limitations=["one event only"], evidence=[R.market_history_path(ev["event_id"])]),
            R.capability(capability="search", status="VERIFIED", summary="fixture search", evidence=["explorer/search_index.json"]),
            R.capability(capability="recent_form_windows", status="VERIFIED", summary="L3", evidence=[pfor(parts[0]["participant_id"])], windows=["L3"]),
            R.capability(capability="advanced_stats", status="RESEARCH", summary="experimental rating", limitations=["not production"], metrics=[m_res["metric_id"]]),
            R.capability(capability="play_by_play", status="UNAVAILABLE", summary="none", reasons=["fixture"]),
        ], notes=["fixture manifest"])
    docs.append(caps)
    entries = [R.search_entry(id=p["participant_id"], kind=etype, label=p["display_name"], path=pfor(p["participant_id"]), sport=sport,
                              aliases=[p["short_name"]], league=ev["league"], season=ev["season"]) for p in parts]
    entries.append(R.search_entry(id=ev["event_id"], kind="EVENT", label=" vs ".join(x["display_name"] for x in ev["participants"]),
                                  path=R.event_path(ev["event_id"]), sport=sport, secondary=ev["competition"], league=ev["league"]))
    entries.append(R.search_entry(id=m_eff["metric_id"], kind="METRIC", label=m_eff["name"], path="explorer/metrics.json", sport=sport, aliases=["EFF"]))
    entries.append(R.search_entry(id=rk["ranking_id"], kind="RANKING", label=f"{m_eff['name']} ranking", path=R.ranking_path(rk["ranking_id"]), sport=sport))
    docs.append(R.search_index(sport=sport, run_id=run_id, generated_at=NOW, entries=entries))
    if mark_research:
        pass
    return run_id, docs, q_ok


def publish_app(sport: str, root: Path) -> tuple[str, dict]:
    """Publish the v1 bundle and the explorer into ``root``; returns (run_id, index)."""
    run_id, documents = documents_for(sport)
    case = SPORT_CASES[sport]
    h = health.build_health(sport=sport, run_id=run_id, now=NOW, last_market_capture=CAPTURED, last_model_generated=NOW,
                            last_successful_run=NOW, bet_authority="RESEARCH_ONLY", payload_run_id=run_id,
                            payload_available=True, commit_sha="deadbeef", router_as_of=NOW, settlement_as_of=NOW)
    publish.publish(root=root, sport=sport, run_id=run_id, generated_at=NOW, documents=documents, source_repo=case["repo"],
                    source_branch="main", commit_sha="deadbeef", model_version="test-model-1.0", health=h)
    _, docs, q = research_documents(sport)
    index = R.publish_explorer(app_root=root, sport=sport, run_id=run_id, generated_at=NOW, documents=docs, quality=q, as_of=CAPTURED,
                               commit_sha="deadbeef", base_manifest_run_id=run_id)
    return run_id, index
