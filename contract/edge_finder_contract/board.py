"""The compact current board (home screen) and per-event detail, built from a validated bundle."""

from __future__ import annotations

from . import SCHEMA_VERSION, ids
from .freshness import DEFAULT_THRESHOLDS, UNKNOWN, Thresholds, status_for, worst
from .timeutil import now_utc, parse_ts, to_iso
from .validate import validate

TOP_N = 3
_REC_ORDER = {"RECOMMENDED": 0, "RESEARCH_CANDIDATE": 1, "WATCH": 2, "PASS": 3, "NOT_PLAYABLE": 4, "EXPIRED": 5}


def _compact_participant(p: dict) -> dict:
    return {k: p[k] for k in ("participant_id", "display_name", "short_name", "participant_type")}


def _compact_rec(r: dict) -> dict:
    return {k: r[k] for k in ("recommendation_id", "market_id", "selection", "market_description",
                              "fair_probability", "current_price", "edge", "status", "authority", "research_only")}


def _latest(stamps: list[str | None]) -> str | None:
    real = [s for s in stamps if s]
    return max(real, key=parse_ts) if real else None


def build_board(*, sport: str, run_id: str, generated_at: object, events: list[dict], markets: list[dict],
                model_prices: list[dict], recommendations: list[dict], wagers: list[dict], health: dict,
                thresholds: dict[str, Thresholds] | None = None, now: object | None = None) -> dict:
    now = now or now_utc()
    th = dict(DEFAULT_THRESHOLDS)
    th.update(thresholds or {})
    by_event_markets: dict[str, list[dict]] = {}
    for m in markets:
        if m.get("event_id"):
            by_event_markets.setdefault(m["event_id"], []).append(m)
    priced_markets: dict[str, set[str]] = {}
    model_stamps: dict[str, list[str]] = {}
    market_to_event = {m["market_id"]: m.get("event_id") for m in markets}
    for mp in model_prices:
        ev = mp.get("event_id") or market_to_event.get(mp["market_id"])
        if ev:
            priced_markets.setdefault(ev, set()).add(mp["market_id"])
            model_stamps.setdefault(ev, []).append(mp["generated_at"])
    recs_by_event: dict[str, list[dict]] = {}
    for r in recommendations:
        recs_by_event.setdefault(r["event_id"], []).append(r)
    wagers_by_event: dict[str, int] = {}
    for w in wagers:
        if w.get("event_id"):
            wagers_by_event[w["event_id"]] = wagers_by_event.get(w["event_id"], 0) + 1

    rows = []
    for ev in sorted(events, key=lambda e: (e["start_time_utc"], e["event_id"])):
        eid = ev["event_id"]
        ev_markets = by_event_markets.get(eid, [])
        captured = _latest([m.get("captured_at") for m in ev_markets])
        modelled = _latest(model_stamps.get(eid, []))
        states = [status_for(captured, component="market_data", now=now, thresholds=th["market_data"]) if ev_markets else UNKNOWN]
        if modelled:
            states.append(status_for(modelled, component="model", now=now, thresholds=th["model"]))
        freshness = worst(*states)
        flags = []
        if not ev_markets:
            flags.append("NO_MARKETS")
        if ev_markets and not modelled:
            flags.append("NO_MODEL_PRICES")
        if freshness == "STALE":
            flags.append("STALE_DATA")
        if ev.get("start_time_confidence") == "PLACEHOLDER":
            flags.append("START_TIME_PLACEHOLDER")
        recs = sorted(recs_by_event.get(eid, []), key=lambda r: (_REC_ORDER.get(r["status"], 9), -(r.get("edge") or 0)))
        rows.append({
            "event_id": eid, "league": ev.get("league"), "competition": ev.get("competition"),
            "start_time_utc": ev["start_time_utc"], "status": ev["status"],
            "home_participant": ev.get("home_participant"), "away_participant": ev.get("away_participant"),
            "participants": [_compact_participant(p) for p in ev.get("participants", [])],
            "data_freshness": freshness, "market_captured_at": captured, "model_generated_at": modelled,
            "markets_available": len(ev_markets), "markets_priced": len(priced_markets.get(eid, ())),
            "recommendations_count": len(recs),
            "top_recommendations": [_compact_rec(r) for r in recs[:TOP_N]],
            "wagers_count": wagers_by_event.get(eid, 0),
            "health_flags": flags,
            "detail_path": f"event_detail/{eid}.json",
        })
    out = {
        "schema_version": SCHEMA_VERSION, "kind": "board", "sport": ids.normalize_sport(sport), "run_id": run_id,
        "generated_at": to_iso(generated_at), "count": len(rows), "items": rows,
        "overall_status": health["overall_status"], "bet_authority": health["bet_authority"],
    }
    validate(out, "board")
    return out


def build_event_detail(*, sport: str, run_id: str, generated_at: object, event: dict, markets: list[dict],
                       model_prices: list[dict], recommendations: list[dict], theses: list[dict],
                       wagers: list[dict], settlements: list[dict], context: dict | None = None,
                       price_history: list[dict] | None = None, data_freshness: str = "UNKNOWN") -> dict:
    eid = event["event_id"]
    ev_markets = [m for m in markets if m.get("event_id") == eid]
    market_ids = {m["market_id"] for m in ev_markets}
    ev_wagers = [w for w in wagers if w.get("event_id") == eid or w["market_id"] in market_ids]
    wager_ids = {w["wager_id"] for w in ev_wagers}
    out = {
        "schema_version": SCHEMA_VERSION, "kind": "event_detail", "sport": ids.normalize_sport(sport),
        "run_id": run_id, "generated_at": to_iso(generated_at),
        "event": event,
        "markets": ev_markets,
        "model_prices": [p for p in model_prices if p["market_id"] in market_ids],
        "recommendations": [r for r in recommendations if r["event_id"] == eid],
        "theses": [t for t in theses if t["event_id"] == eid],
        "wagers": ev_wagers,
        "settlements": [s for s in settlements if s["wager_id"] in wager_ids],
        "context": dict(context or {}),
        "price_history": list(price_history or []),
        "data_freshness": data_freshness,
    }
    validate(out, "event_detail")
    return out
