"""The AI-ready handicap packet (contract 1.1.0) and the research-tray resolver.

The Edge Finder app is free and hosts no model. A user presses COPY FOR CHATGPT and receives ONE
compact, self-contained, deterministic package: the handicap protocol, the relevant evidence, every
current market in scope, the model evidence, data quality and freshness, the user's own research
focus, and the warning that projections are evidence, not recommendations.

Everything is built from a published app root (``app/latest`` with its ``explorer/`` tree); nothing
is fetched, nothing is inferred, nothing missing is invented. The same request against the same
publication yields byte-identical output (:func:`build` sorts, deduplicates and digests).

Scopes
    GAME    one event: its research document, both participants, the relevant players, every market
    SLATE   every event whose start falls inside a UTC window
    CUSTOM  a research tray: the user's references, resolved to documents, plus the events they imply
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from . import SCHEMA_VERSION, ids
from .freshness import UNKNOWN as F_UNKNOWN, status_for, worst
from .research import EXPLORER_DIR, load_explorer
from .timeutil import parse_ts, to_iso, to_iso_or_none
from .validate import validate

PACKET_VERSION = "1.0.0"
PROTOCOL_DIR = Path(__file__).resolve().parent / "protocols"
WARNING = ("Everything in this packet is EVIDENCE. Model prices, projections and the repository's own "
           "recommendations are research outputs, not bets. Form your own view, price it, name the counter-case, "
           "and PASS when nothing compelling exists. Items marked RESEARCH are lower-confidence; missing data is "
           "listed, never filled in.")
DEFAULT_MAX_CHARS = 60_000
RECENT_POINTS = 8


# ------------------------------------------------------------------ protocols

def protocol_ids() -> list[str]:
    return sorted(p.name[:-5] for p in PROTOCOL_DIR.glob("*.json"))


def load_protocol(protocol_id: str) -> dict:
    path = PROTOCOL_DIR / f"{protocol_id}.json"
    if not path.exists():
        raise KeyError(f"no protocol {protocol_id!r}; available: {protocol_ids()}")
    doc = json.loads(path.read_text(encoding="utf-8"))
    validate(doc, "handicap_protocol")
    return doc


def protocol_for_sport(sport: str) -> dict:
    sport = ids.normalize_sport(sport)
    pid = f"edge_finder.handicap.{sport.lower()}.v1"
    try:
        return load_protocol(pid)
    except KeyError:
        return load_protocol("edge_finder.handicap.core.v1")


# ------------------------------------------------------------------ the published app root

class AppRoot:
    """Lazy reader over a published ``app/latest`` (v1 documents + explorer)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._cache: dict[str, Any] = {}

    def _read(self, name: str) -> dict | None:
        if name not in self._cache:
            path = self.root / name
            self._cache[name] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        return self._cache[name]

    def items(self, kind: str) -> list[dict]:
        doc = self._read(f"{kind}.json")
        return list(doc["items"]) if doc else []

    @property
    def manifest(self) -> dict | None:
        return self._read("manifest.json")

    @property
    def health(self) -> dict | None:
        return self._read("health.json")

    @property
    def explorer(self) -> tuple[dict, dict[str, dict]] | None:
        if "explorer" not in self._cache:
            try:
                self._cache["explorer"] = load_explorer(self.root)
            except Exception:  # noqa: BLE001 - absence is a state, not an error
                self._cache["explorer"] = None
        return self._cache["explorer"]

    def explorer_doc(self, app_path: str) -> dict | None:
        ex = self.explorer
        if not ex:
            return None
        rel = app_path[len(EXPLORER_DIR) + 1:] if app_path.startswith(EXPLORER_DIR + "/") else app_path
        return ex[1].get(rel)

    def explorer_docs(self, kind: str) -> list[dict]:
        ex = self.explorer
        return [d for d in ex[1].values() if d["kind"] == kind] if ex else []


# ------------------------------------------------------------------ research tray

def tray_item(*, ref_kind: str, sport: str, id: str, added_at: object, extra: dict | None = None, note: object = None) -> dict:
    ex = None
    if extra:
        ex = {"series_id": extra.get("series_id"), "x": extra.get("x"), "metric_id": extra.get("metric_id"),
              "market_id": extra.get("market_id"), "event_id": extra.get("event_id")}
    out = {"item_id": ids.tray_item_id(ref_kind, id, json.dumps(ex, sort_keys=True) if ex else None), "ref_kind": ref_kind,
           "sport": ids.normalize_sport(sport), "id": str(id), "extra": ex, "note": None if note is None else str(note),
           "added_at": to_iso(added_at)}
    validate(out, "tray_item")
    return out


def tray(items: Iterable[dict], updated_at: object, tray_version: str = "1.0.0") -> dict:
    out = {"schema_version": SCHEMA_VERSION, "kind": "research_tray", "tray_version": tray_version,
           "items": list(items), "updated_at": to_iso(updated_at)}
    validate(out, "research_tray")
    return out


def resolve_tray(app: AppRoot, tray_doc: dict) -> list[dict]:
    """Each item -> ``{item, resolved: bool, label, document, event_ids, entity_ids, market_ids, note}``.
    Unresolved items are reported, never invented."""
    out = []
    ex = app.explorer
    docs = ex[1] if ex else {}
    by_entity = {}
    for d in docs.values():
        if d["kind"] == "entity_profile":
            by_entity[d["entity"]["participant_id"]] = d
        elif d["kind"] == "event_research":
            by_entity[d["event"]["event_id"]] = d
        elif d["kind"] == "ranking":
            by_entity[d["ranking_id"]] = d
        elif d["kind"] == "time_series":
            by_entity[d["series_id"]] = d
    markets = {m["market_id"]: m for m in app.items("markets")}
    prices = {mp["model_price_id"]: mp for mp in app.items("model_prices")}
    registry = next((d for d in docs.values() if d["kind"] == "metric_registry"), None)
    metrics = {m["metric_id"]: m for m in registry["items"]} if registry else {}
    for item in tray_doc["items"]:
        kind, ref = item["ref_kind"], item["id"]
        res = {"item": item, "resolved": False, "label": None, "document": None, "event_ids": [], "entity_ids": [],
               "market_ids": [], "note": item.get("note")}
        if kind in ("TEAM", "PLAYER") and ref in by_entity:
            d = by_entity[ref]
            res.update(resolved=True, label=d["entity"]["display_name"], document=d, entity_ids=[ref],
                       event_ids=[g["event_id"] for g in d.get("games", [])])
        elif kind == "EVENT" and ref in by_entity:
            d = by_entity[ref]
            res.update(resolved=True, label=_event_label(d["event"]), document=d, event_ids=[ref])
        elif kind == "METRIC" and ref in metrics:
            res.update(resolved=True, label=metrics[ref]["name"], document=metrics[ref])
        elif kind in ("RANKING", "SERIES") and ref in by_entity:
            d = by_entity[ref]
            res.update(resolved=True, label=f"{kind.lower()} {d['metric_id']}", document=d,
                       entity_ids=[d["entity_id"]] if "entity_id" in d else [])
        elif kind == "CHART_POINT":
            sid = (item.get("extra") or {}).get("series_id")
            x = (item.get("extra") or {}).get("x")
            d = by_entity.get(sid) if sid else None
            pt = next((p for p in d["points"] if p["x"] == x), None) if d else None
            if d and pt:
                res.update(resolved=True, label=f"{d['metric_id']} @ {x}", document={"series": d, "point": pt},
                           entity_ids=[d["entity_id"]], event_ids=[pt["event_id"]] if pt.get("event_id") else [])
        elif kind == "MARKET" and ref in markets:
            m = markets[ref]
            res.update(resolved=True, label=m["yes_description"], document=m, market_ids=[ref],
                       event_ids=[m["event_id"]] if m.get("event_id") else [])
        elif kind == "PROJECTION" and ref in prices:
            mp = prices[ref]
            res.update(resolved=True, label=f"model price {mp['market_id']}", document=mp, market_ids=[mp["market_id"]],
                       event_ids=[mp["event_id"]] if mp.get("event_id") else [])
        out.append(res)
    return out


# ------------------------------------------------------------------ packet construction

def _event_label(ev: dict) -> str:
    names = {p["participant_id"]: p.get("short_name") or p["display_name"] for p in ev["participants"]}
    if ev.get("home_participant") and ev.get("away_participant"):
        return f"{names.get(ev['away_participant'], '?')} @ {names.get(ev['home_participant'], '?')}"
    return " vs ".join(names.values())


def _mid(m: dict) -> float | None:
    if m.get("yes_bid") is not None and m.get("yes_ask") is not None:
        return round((m["yes_bid"] + m["yes_ask"]) / 2, 6)
    return m.get("market_probability")


def _packet_market(m: dict, now: object) -> dict:
    return {"market_id": m["market_id"], "kalshi_ticker": m["kalshi_ticker"], "event_id": m.get("event_id"),
            "market_family": m["market_family"], "yes_description": m["yes_description"], "yes_bid": m.get("yes_bid"),
            "yes_ask": m.get("yes_ask"), "mid": _mid(m), "last_price": m.get("last_price"), "captured_at": m.get("captured_at"),
            "freshness": status_for(m.get("captured_at"), component="market_data", now=now) if m.get("captured_at") else F_UNKNOWN,
            "market_status": m.get("market_status", "UNKNOWN"), "participant_id": m.get("participant_id"), "player_id": m.get("player_id"),
            "period": m.get("period"), "side": m.get("side"), "line": m.get("line"), "threshold": m.get("threshold")}


def _packet_model(mp: dict, now: object, rec_authority: dict) -> dict:
    research_only, authority = rec_authority.get(mp["market_id"], (True, "RESEARCH_ONLY"))
    return {"market_id": mp["market_id"], "fair_probability": mp.get("fair_probability"),
            "market_probability": mp.get("market_probability"), "edge": mp.get("edge"),
            "projection_value": mp.get("projection_value"), "projection_unit": mp.get("projection_unit"),
            "model_version": mp.get("model_version"), "generated_at": mp["generated_at"], "research_only": research_only,
            "authority": authority, "data_quality_status": mp.get("data_quality_status", "UNKNOWN"),
            "freshness": status_for(mp["generated_at"], component="model", now=now)}


def _packet_obs(o: dict, name: str) -> dict:
    ctx = o.get("context") or {}
    return {"metric_id": o["metric_id"], "name": name, "value": o.get("value"), "adjusted_value": o.get("adjusted_value"),
            "display_value": o.get("display_value"), "unit": o.get("unit"), "window": o["window"]["label"],
            "split": f"{o['split']['dimension']}={o['split']['value']}" if o.get("split") else None,
            "rank": ctx.get("rank"), "universe_size": ctx.get("universe_size"), "league_average": ctx.get("league_average"),
            "as_of": o["as_of"], "quality_status": o["quality_status"], "source": o["source"]}


def _evidence_for_profile(app: AppRoot, prof: dict, metric_names: dict, focus_metric_ids: set[str]) -> dict:
    obs = [_packet_obs(o, metric_names.get(o["metric_id"], o["metric_id"])) for o in prof.get("metrics", [])]
    for dim, rows in (prof.get("splits") or {}).items():
        obs.extend(_packet_obs(o, metric_names.get(o["metric_id"], o["metric_id"])) for o in rows)
    # Keep every observation of a focused metric; otherwise one observation per metric per window to stay compact.
    seen = set()
    kept = []
    for o in sorted(obs, key=lambda r: (r["metric_id"], r["window"], r["split"] or "")):
        key = (o["metric_id"], o["window"], o["split"])
        if key in seen:
            continue
        seen.add(key)
        kept.append(o)
    recent = []
    for ref in prof.get("series", [])[:12]:
        d = app.explorer_doc(ref["path"])
        if not d:
            continue
        pts = d["points"][-RECENT_POINTS:] if d["metric_id"] in focus_metric_ids or len(recent) < 3 * RECENT_POINTS else d["points"][-3:]
        for p in pts:
            recent.append({"x": p["x"], "t": p["t"], "metric_id": d["metric_id"], "value": p.get("value"),
                           "opponent": p.get("opponent_id"), "event_id": p.get("event_id")})
    team = (prof.get("team") or {}).get("display_name")
    return {"entity_id": prof["entity"]["participant_id"], "entity_type": prof["entity_type"],
            "label": prof["entity"]["display_name"], "team": team, "role": (prof["entity"].get("metadata") or {}).get("position"),
            "observations": kept, "availability": prof.get("availability", []), "recent": recent}


def _events_in_window(app: AppRoot, start: object, end: object) -> list[dict]:
    s, e = parse_ts(start), parse_ts(end)
    return [ev for ev in app.items("events") if s <= parse_ts(ev["start_time_utc"]) <= e]


def build(*, app_root: Path, scope_kind: str, event_id: object = None, window_start: object = None,
          window_end: object = None, tray_doc: dict | None = None, protocol_id: object = None,
          generated_at: object = None, max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    """Build the packet for one scope from a published app root. Deterministic for a given publication."""
    app = AppRoot(app_root)
    manifest = app.manifest
    if manifest is None:
        raise FileNotFoundError(f"no manifest.json under {app_root}")
    sport = manifest["sport"]
    now = to_iso(generated_at) if generated_at is not None else manifest["generated_at"]
    proto = load_protocol(str(protocol_id)) if protocol_id else protocol_for_sport(sport)
    events_by_id = {ev["event_id"]: ev for ev in app.items("events")}
    markets = app.items("markets")
    model_prices = app.items("model_prices")
    recs = app.items("recommendations")
    theses = {t["event_id"]: t for t in app.items("theses")}
    rec_authority = {r["market_id"]: (bool(r["research_only"]), r["authority"]) for r in recs}

    # 1. the scope -> event ids and focus
    focus: list[dict] = []
    focus_entity_ids: list[str] = []
    focus_market_ids: list[str] = []
    focus_metric_ids: set[str] = set()
    missing: list[str] = []
    if scope_kind == "GAME":
        if not event_id or event_id not in events_by_id:
            raise KeyError(f"event {event_id!r} is not in this publication")
        event_ids = [str(event_id)]
        label = _event_label(events_by_id[event_ids[0]])
    elif scope_kind == "SLATE":
        if not window_start or not window_end:
            raise ValueError("a SLATE scope needs window_start and window_end")
        event_ids = sorted(ev["event_id"] for ev in _events_in_window(app, window_start, window_end))
        label = f"slate {to_iso(window_start)}..{to_iso(window_end)}"
    elif scope_kind == "CUSTOM":
        if tray_doc is None:
            raise ValueError("a CUSTOM scope needs a research tray")
        resolved = resolve_tray(app, tray_doc)
        event_ids = sorted({eid for r in resolved for eid in r["event_ids"] if eid in events_by_id})
        for r in resolved:
            focus.append({"item_id": r["item"]["item_id"], "ref_kind": r["item"]["ref_kind"], "id": r["item"]["id"],
                          "label": r["label"], "resolved": r["resolved"], "note": r["note"]})
            if not r["resolved"]:
                missing.append(f"tray item {r['item']['ref_kind']} {r['item']['id']} is not in this publication")
            focus_entity_ids.extend(r["entity_ids"])
            focus_market_ids.extend(r["market_ids"])
            if r["item"]["ref_kind"] == "METRIC":
                focus_metric_ids.add(r["item"]["id"])
            elif r["item"]["ref_kind"] in ("RANKING", "SERIES", "CHART_POINT") and r["document"]:
                d = r["document"].get("series", r["document"]) if isinstance(r["document"], dict) else None
                if d and d.get("metric_id"):
                    focus_metric_ids.add(d["metric_id"])
        label = f"research tray ({len(tray_doc['items'])} items)"
    else:
        raise ValueError(f"unknown scope kind {scope_kind!r}")

    # 2. events + participants
    packet_events = []
    participant_ids: list[str] = []
    for eid in event_ids:
        ev = events_by_id[eid]
        research = app.explorer_doc(f"explorer/events/{eid}.json")
        notes = list((research or {}).get("context", {}).get("notes", [])) if research else []
        th = theses.get(eid)
        packet_events.append({
            "event_id": eid, "label": _event_label(ev), "start_time_utc": ev["start_time_utc"], "status": ev["status"],
            "home_participant": ev.get("home_participant"), "away_participant": ev.get("away_participant"),
            "venue": ev.get("venue"), "context_notes": notes,
            "theses": [{"summary": th.get("summary"), "supporting_factors": th.get("supporting_factors", []),
                        "opposing_factors": th.get("opposing_factors", []), "research_only": True}] if th else [],
        })
        participant_ids.extend(p["participant_id"] for p in ev["participants"])
        if research:
            participant_ids.extend(p["participant_id"] for p in research.get("players", []))
        else:
            missing.append(f"no event research for {eid}")

    # 3. evidence: focused entities first, then event participants, deduplicated
    registry = next((d for d in app.explorer_docs("metric_registry")), None)
    metric_names = {m["metric_id"]: m["name"] for m in registry["items"]} if registry else {}
    metric_status = {m["metric_id"]: m["quality"]["status"] for m in registry["items"]} if registry else {}
    ordered_entities = list(dict.fromkeys(focus_entity_ids + participant_ids))
    evidence = []
    for pid in ordered_entities:
        prof = app.explorer_doc(f"explorer/teams/{pid}.json") or app.explorer_doc(f"explorer/players/{pid}.json")
        if prof is None:
            missing.append(f"no profile for {pid}")
            continue
        evidence.append(_evidence_for_profile(app, prof, metric_names, focus_metric_ids))

    # 4. markets: every market of the events in scope (+ focused markets), model evidence for those markets
    in_scope = {eid for eid in event_ids}
    packet_markets = []
    seen_m = set()
    for m in sorted(markets, key=lambda m: (m.get("event_id") or "", m["market_family"], m["kalshi_ticker"])):
        if (m.get("event_id") in in_scope or m["market_id"] in focus_market_ids) and m["market_id"] not in seen_m:
            seen_m.add(m["market_id"])
            packet_markets.append(_packet_market(m, now))
    if not packet_markets:
        missing.append("no current markets for the scope")
    packet_models = []
    seen_mp = set()
    for mp in sorted(model_prices, key=lambda r: (r["market_id"], r["generated_at"]), reverse=True):
        if mp["market_id"] in seen_m and mp["market_id"] not in seen_mp:
            seen_mp.add(mp["market_id"])
            packet_models.append(_packet_model(mp, now, rec_authority))
    packet_models.sort(key=lambda r: r["market_id"])
    repo_recs = [{"market_id": r["market_id"], "selection": r["selection"], "status": r["status"],
                  "fair_probability": r.get("fair_probability"), "bet_up_to_price": r.get("bet_up_to_price"),
                  "authority": r["authority"], "research_only": r["research_only"], "created_at": r["created_at"]}
                 for r in sorted(recs, key=lambda r: (r["market_id"], r["created_at"])) if r["market_id"] in seen_m]

    # 5. quality
    research_only_items = sorted({o["metric_id"] for e in evidence for o in e["observations"] if o["quality_status"] == "RESEARCH"}
                                 | {mp["market_id"] for mp in packet_models if mp["research_only"]}
                                 | {mid for mid, st in metric_status.items() if st == "RESEARCH" and mid in
                                    {o["metric_id"] for e in evidence for o in e["observations"]}})
    caps = {}
    cap_doc = next(iter(app.explorer_docs("capability_manifest")), None)
    if cap_doc:
        caps = {c["capability"]: c["status"] for c in cap_doc["items"] if c["status"] != "UNKNOWN"}
    else:
        missing.append("no capability manifest (no explorer published)")
    sources = sorted({o["source"] for e in evidence for o in e["observations"]} | {"kalshi markets"} |
                     ({"model prices"} if packet_models else set()))
    market_fresh = worst(*[m["freshness"] for m in packet_markets]) if packet_markets else F_UNKNOWN
    model_fresh = worst(*[m["freshness"] for m in packet_models]) if packet_models else F_UNKNOWN
    data_as_of = max([m["captured_at"] for m in packet_markets if m["captured_at"]] + [mp["generated_at"] for mp in packet_models]
                     + [manifest["generated_at"]])

    packet = {
        "schema_version": SCHEMA_VERSION, "kind": "handicap_packet",
        "packet_id": ids.packet_id(proto["protocol_id"], scope_kind, *event_ids, *(f["item_id"] for f in focus), data_as_of=data_as_of),
        "packet_version": PACKET_VERSION,
        "protocol": {k: proto[k] for k in ("protocol_id", "version", "extends", "principles", "steps", "outputs_required",
                                           "forbidden", "evidence_weights")},
        "scope": {"kind": scope_kind, "event_ids": event_ids, "window_start": to_iso_or_none(window_start),
                  "window_end": to_iso_or_none(window_end), "label": label},
        "sports": [sport], "generated_at": now, "data_as_of": data_as_of, "warning": WARNING,
        "user_focus": focus, "events": packet_events, "evidence": evidence, "markets": packet_markets,
        "model_evidence": packet_models, "repo_recommendations": repo_recs,
        "quality": {"sources": sources, "market_freshness": market_fresh, "model_freshness": model_fresh,
                    "research_only_items": research_only_items, "missing": sorted(set(missing)), "capabilities": caps},
        "budget": {"max_chars": int(max_chars), "chars": 0, "truncated": []},
    }
    _fit_budget(packet, max_chars)
    validate(packet, "handicap_packet")
    return packet


def _size(packet: dict) -> int:
    """The budget is measured on what the user copies: the text rendering."""
    return len(render_text(packet))


def _fit_budget(packet: dict, max_chars: int) -> None:
    """Trim in a fixed order until the clipboard text fits: recent series points, observations beyond 24
    per entity, repository recommendations, all recent points, then observations beyond 8 per player and
    12 per entity (research-tray items keep theirs). Markets and model evidence are never trimmed (full
    market coverage is the point); the budget records every cut."""
    truncated = packet["budget"]["truncated"]
    if _size(packet) > max_chars:
        for e in packet["evidence"]:
            if e["recent"]:
                e["recent"] = e["recent"][-3:]
        truncated.append("recent series points reduced to 3 per series")
    if _size(packet) > max_chars:
        for e in packet["evidence"]:
            if len(e["observations"]) > 24:
                e["observations"] = e["observations"][:24]
        truncated.append("observations capped at 24 per entity")
    if _size(packet) > max_chars and packet["repo_recommendations"]:
        packet["repo_recommendations"] = []
        truncated.append("repository recommendations omitted")
    if _size(packet) > max_chars:
        for e in packet["evidence"]:
            e["recent"] = []
        truncated.append("recent series points omitted")
    focus = {f["id"] for f in packet["user_focus"]}
    for kind, cap in (("PLAYER", 8), (None, 12)):
        if _size(packet) <= max_chars:
            break
        for e in packet["evidence"]:
            if e["entity_id"] not in focus and (kind is None or e["entity_type"] == kind) and len(e["observations"]) > cap:
                e["observations"] = e["observations"][:cap]
        truncated.append(f"observations capped at {cap} per {'player' if kind else 'entity'} (tray items kept)")
    if _size(packet) > max_chars:
        truncated.append("over budget: every market in scope was kept")
    packet["budget"]["chars"] = _size(packet)


# ------------------------------------------------------------------ text rendering (the clipboard form)

def _fmt_p(v: float | None) -> str:
    return "-" if v is None else f"{v:.3f}"


def _cents(v: float | None) -> str:
    if v is None:
        return "-"
    c = round(float(v) * 100, 1)
    return str(int(c)) if c == int(c) else f"{c:g}"


def _rung_value(m: dict) -> float | None:
    return m["threshold"] if m.get("threshold") is not None else m.get("line")


def _template(rows: list[dict]) -> str | None:
    """The shared description of a ladder with its rung value replaced by X, or None when the
    descriptions do not differ by exactly that one number."""
    out = None
    for m in rows:
        v = float(_rung_value(m))
        desc = m["yes_description"]
        found = None
        for txt in (f"{v:.1f}", f"{v:g}", f"{v:.2f}"):
            if desc.count(txt) == 1:
                found = desc.replace(txt, "X")
                break
        if found is None or (out is not None and found != out):
            return None
        out = found
    return out


def _ladder_groups(markets: list[dict]) -> list[list[dict]]:
    """Group markets that differ only by line/threshold (same event, family, series, period, side,
    team and player), keeping the packet's market order."""
    groups: dict[tuple, list[dict]] = {}
    for m in markets:
        key = (m.get("event_id") or "", m["market_family"], m["kalshi_ticker"].split("-")[0], m.get("period") or "",
               m.get("side") or "", m.get("participant_id") or "", m.get("player_id") or "")
        groups.setdefault(key, []).append(m)
    return list(groups.values())


def _strip_common(texts: list[str]) -> list[str]:
    """Each text with the word-level prefix and suffix shared by all of them removed."""
    words = [t.split(" ") for t in texts]
    pre = 0
    while all(len(w) > pre for w in words) and len({w[pre] for w in words}) == 1:
        pre += 1
    suf = 0
    while all(len(w) - pre > suf for w in words) and len({w[-1 - suf] for w in words}) == 1:
        suf += 1
    return [" ".join(w[pre: len(w) - suf]) for w in words]


def _default_stamp(markets: list[dict]) -> tuple[str | None, str]:
    counts: dict[tuple, int] = {}
    for m in markets:
        k = (m.get("captured_at"), m["freshness"])
        counts[k] = counts.get(k, 0) + 1
    if not counts:
        return None, F_UNKNOWN
    return max(counts.items(), key=lambda kv: (kv[1], str(kv[0][0])))[0]


def _render_markets(packet: dict) -> list[str]:
    models = {mp["market_id"]: mp for mp in packet["model_evidence"]}
    d_cap, d_fresh = _default_stamp(packet["markets"])

    def fair(m: dict) -> str:
        mp = models.get(m["market_id"])
        if not mp:
            return ""
        out = f" fair {_cents(mp['fair_probability'])}" if mp.get("fair_probability") is not None else ""
        if mp.get("projection_value") is not None:
            out += f" proj {mp['projection_value']:g}{(' ' + mp['projection_unit']) if mp.get('projection_unit') else ''}"
        return out

    def stamp(rows: list[dict]) -> str:
        if all(m.get("captured_at") == d_cap and m["freshness"] == d_fresh for m in rows):
            return ""
        caps = sorted({m["captured_at"] for m in rows if m.get("captured_at")})
        fresh = worst(*[m["freshness"] for m in rows]) if rows else F_UNKNOWN
        when = caps[-1] if len(caps) == 1 else (f"{caps[0]}..{caps[-1]}" if caps else "no capture time")
        return f" ({when}, {fresh})"

    def prefix_of(tickers: list[str]) -> str:
        pre = os.path.commonprefix(tickers)
        return pre[: pre.rfind("-") + 1] if "-" in pre else ""

    lines = [f"(unless a line says otherwise, prices were captured at {d_cap or 'an unknown time'}, {d_fresh})"]
    singles: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for rows in _ladder_groups(packet["markets"]):
        first = rows[0]
        per = f" {first['period']}" if first.get("period") else ""
        ladder = len(rows) >= 2 and all(_rung_value(m) is not None for m in rows)
        tmpl = _template(rows) if ladder else None
        if tmpl is not None:
            rows = sorted(rows, key=lambda m: (float(_rung_value(m)), m["kalshi_ticker"]))
            prefix = prefix_of([m["kalshi_ticker"] for m in rows])
            def rung(m: dict) -> str:
                suffix = m["kalshi_ticker"][len(prefix):]
                tag = suffix if suffix == f"{_rung_value(m):g}" else f"{_rung_value(m):g} {suffix}"
                return f"{tag} {_cents(m['yes_bid'])}/{_cents(m['yes_ask'])}{fair(m)}"
            rungs = "; ".join(rung(m) for m in rows)
            lines.append(f"- [{first['market_family']}{per}] {tmpl} | {prefix}*: {rungs}{stamp(rows)}")
            order.append(("line", len(lines) - 1))
            continue
        for m in rows:
            key = (m.get("event_id") or "", m["market_family"], m["kalshi_ticker"].split("-")[0], m.get("period") or "")
            if key not in singles:
                singles[key] = []
                lines.append(None)  # placeholder keeps the packet's market order
                order.append(("board", key, len(lines) - 1))
            singles[key].append(m)
    for entry in order:
        if entry[0] != "board":
            continue
        key, at = entry[1], entry[2]
        rows = singles[key]
        per = f" {key[3]}" if key[3] else ""
        if len(rows) < 3:
            lines[at] = "\n".join(f"- {m['kalshi_ticker']} [{m['market_family']}{per}] {m['yes_description']}: "
                                  f"{_cents(m['yes_bid'])}/{_cents(m['yes_ask'])}{fair(m)}{stamp([m])}" for m in rows)
            continue
        prefix = prefix_of([m["kalshi_ticker"] for m in rows])
        labels = _strip_common([m["yes_description"] for m in rows])
        items = "; ".join(f"{m['kalshi_ticker'][len(prefix):]} {lab or m['yes_description']} {_cents(m['yes_bid'])}/{_cents(m['yes_ask'])}{fair(m)}{stamp([m])}"
                          for m, lab in zip(rows, labels))
        lines[at] = f"- [{key[1]}{per}] {rows[0]['yes_description']} (and like it) | {prefix}*: {items}"
    return [ln for ln in lines if ln is not None]


def _model_summary(packet: dict) -> str | None:
    models = packet["model_evidence"]
    if not models:
        return None
    kinds: dict[tuple, int] = {}
    for mp in models:
        k = (mp.get("model_version") or "unversioned", "RESEARCH" if mp["research_only"] else "PROMOTED", mp["authority"])
        kinds[k] = kinds.get(k, 0) + 1
    gen = max(mp["generated_at"] for mp in models)
    fresh = worst(*[mp["freshness"] for mp in models])
    parts = ", ".join(f"{n} from {v} ({r}/{a})" for (v, r, a), n in sorted(kinds.items()))
    return f"MODEL EVIDENCE: {len(models)} model prices ({parts}); newest {gen}, {fresh}. Shown as 'fair' on each market line."


def render_text(packet: dict) -> str:
    """A compact, deterministic plain-text rendering for the clipboard. Same information, fewer bytes."""
    p = packet["protocol"]
    lines = [f"EDGE FINDER HANDICAP PACKET {packet['packet_id']} (packet {packet['packet_version']}, protocol {p['protocol_id']} {p['version']})",
             f"Scope: {packet['scope']['kind']} — {packet['scope']['label']}; sport {', '.join(packet['sports'])}; data as of {packet['data_as_of']}",
             "", "WARNING: " + packet["warning"], "", "PROTOCOL PRINCIPLES:"]
    lines += [f"- {x}" for x in p["principles"]]
    lines += ["", "STEPS:"] + [f"{i + 1}. [{s['id']}] {s['instruction']}" for i, s in enumerate(p["steps"])]
    lines += ["", "REQUIRED OUTPUTS: " + "; ".join(p["outputs_required"]), "FORBIDDEN: " + "; ".join(p["forbidden"])]
    lines += ["", "EVIDENCE WEIGHTS: " + "; ".join(f"{k}: {v}" for k, v in sorted(p["evidence_weights"].items()))]
    q = packet["quality"]
    lines += ["", f"DATA QUALITY: markets {q['market_freshness']}, model {q['model_freshness']}; sources: {', '.join(q['sources'])}"]
    if q["capabilities"]:
        lines.append("Capabilities: " + ", ".join(f"{k}={v}" for k, v in sorted(q["capabilities"].items())))
    if q["research_only_items"]:
        named = [i for i in q["research_only_items"] if not i.startswith("mkt_")]
        n_mkt = len(q["research_only_items"]) - len(named)
        lines.append("RESEARCH-only items: " + ", ".join(named + ([f"model prices on {n_mkt} markets"] if n_mkt else [])))
    if q["missing"]:
        lines.append("MISSING: " + "; ".join(q["missing"]))
    if packet["user_focus"]:
        lines += ["", "USER FOCUS (the user is specifically investigating these; evaluate them seriously, do not assume they are good bets; "
                      "compare against alternative expressions and opposing evidence):"]
        lines += [f"- {f['ref_kind']} {f['label'] or f['id']}{' [UNRESOLVED]' if not f['resolved'] else ''}{(' — ' + f['note']) if f.get('note') else ''}"
                  for f in packet["user_focus"]]
    lines += ["", "EVENTS:"]
    for ev in packet["events"]:
        lines.append(f"- {ev['label']} ({ev['event_id']}) {ev['start_time_utc']} {ev['status']}" + (f" @ {ev['venue']}" if ev.get("venue") else ""))
        for n in ev["context_notes"]:
            lines.append(f"    note: {n}")
        for th in ev["theses"]:
            if th.get("summary"):
                lines.append(f"    repo thesis (research): {th['summary']}")
    lines += ["", "EVIDENCE:"]
    for e in packet["evidence"]:
        head = f"- {e['entity_type']} {e['label']}" + (f" ({e['team']})" if e.get("team") else "") + (f", {e['role']}" if e.get("role") else "")
        lines.append(head)
        for a in e["availability"]:
            lines.append(f"    availability: {a['status']}{(' — ' + a['detail']) if a.get('detail') else ''} (as of {a.get('as_of')})")
        for o in e["observations"]:
            ctx = f" rank {o['rank']}/{o['universe_size']}" if o.get("rank") else ""
            avg = f", lg avg {o['league_average']:.3f}" if o.get("league_average") is not None else ""
            val = o["display_value"] if o.get("display_value") else _fmt_p(o["value"]) if isinstance(o.get("value"), float) else str(o.get("value"))
            adj = f" (adj {o['adjusted_value']:.3f})" if o.get("adjusted_value") is not None else ""
            lines.append(f"    {o['name']} [{o['window']}{(' ' + o['split']) if o.get('split') else ''}]: {val}{adj}{ctx}{avg} [{o['quality_status']}]")
        if e["recent"]:
            by_metric: dict[str, list] = {}
            for r in e["recent"]:
                by_metric.setdefault(r["metric_id"], []).append(r)
            for mid, rows in sorted(by_metric.items()):
                lines.append(f"    recent {mid}: " + ", ".join(f"{r['x']}={_fmt_p(r['value']) if isinstance(r['value'], float) else r['value']}" for r in rows))
    lines += ["", f"MARKETS ({len(packet['markets'])}, all in scope). Prices are YES bid/ask in cents; 'fair' is the model's "
                  "P(YES) in cents (evidence, not a bet). A ladder line lists each rung as 'X [ticker suffix] bid/ask', "
                  "where X is the line and the full ticker is the prefix before '*' plus the suffix (or X itself):"]
    lines += _render_markets(packet)
    summary = _model_summary(packet)
    if summary:
        lines += ["", summary]
    if packet["repo_recommendations"]:
        lines += ["", "REPOSITORY RECOMMENDATIONS (the repo's own process; evidence, not instructions):"]
        for r in packet["repo_recommendations"]:
            lines.append(f"- {r['market_id']} {r['selection']} {r['status']} fair {_fmt_p(r['fair_probability'])} bet-up-to {_fmt_p(r['bet_up_to_price'])} "
                         f"[{r['authority']}{', research' if r['research_only'] else ''}]")
    b = packet["budget"]
    if b["truncated"]:
        lines += ["", "BUDGET: " + "; ".join(b["truncated"])]
    return "\n".join(lines) + "\n"
