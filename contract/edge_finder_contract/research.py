"""The research graph (contract 1.1.0, additive to ``edge_finder.app.v1``).

A sport's research adapter turns its committed data into an ``explorer/`` tree beside ``app/latest``:

    explorer/index.json             what is here (file table with digests, entity directory)
    explorer/capabilities.json      what this sport can honestly show (VERIFIED / PARTIAL / RESEARCH / UNAVAILABLE)
    explorer/metrics.json           what every metric means (the metric registry)
    explorer/search_index.json      what a static client can search
    explorer/teams/<prt_>.json      entity profiles
    explorer/players/<prt_>.json
    explorer/events/<evt_>.json     event research (matchup, players, projections, markets)
    explorer/rankings/<rnk_>.json   comparison universes (every entity, not a bare percentile)
    explorer/series/<ser_>.json     time series (points link to the game and opponent that produced them)
    explorer/market_history/<evt_>.json

Three rules make the tree navigable without a server:
1. Every number is an :func:`observation` with a :func:`window`, a quality status and, where a comparison
   universe exists, a ``context`` (rank, universe size, league average/median, best, worst).
2. Every document carries ``links`` and every link's ``path`` must exist in the index
   (:func:`verify_explorer` enforces it: no dead ends unless the sport genuinely lacks the data).
3. The capability manifest is the gate: :func:`check_capabilities` refuses VERIFIED/PARTIAL claims
   without evidence files and UNAVAILABLE claims that contradict the published files.

Nothing here computes a model, an adjustment or a projection. Rankings and percentiles are
arithmetic over values the source repository already stores.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import statistics
import tempfile
from pathlib import Path
from typing import Any, Iterable

from . import CAPABILITIES, QUALITY_STATUSES, SCHEMA_VERSION, ids
from .build import _num, _str, _strs
from .publish import dumps, sha256_text
from .timeutil import parse_ts, to_date, to_iso, to_iso_or_none
from .validate import SchemaError, validate

EXPLORER_DIR = "explorer"
INDEX_NAME = "index.json"
CAPABILITIES_NAME = "capabilities.json"
METRICS_NAME = "metrics.json"
SEARCH_NAME = "search_index.json"
SUBDIRS = {"entity_profile:TEAM": "teams", "entity_profile:PLAYER": "players", "event_research": "events",
           "ranking": "rankings", "time_series": "series", "market_history": "market_history"}

VERIFIED, PARTIAL, RESEARCH, UNAVAILABLE, UNKNOWN = QUALITY_STATUSES

#: Which published kinds prove which capability. A capability with no entry is proven by the
#: capability manifest's own ``evidence`` paths alone.
CAPABILITY_EVIDENCE_KINDS = {
    "team_profiles": ("entity_profile:TEAM",), "player_profiles": ("entity_profile:PLAYER",),
    "event_research": ("event_research",), "rankings": ("ranking",), "time_series": ("time_series",),
    "market_price_history": ("market_history",), "search": ("search_index",),
}


# ------------------------------------------------------------------ small constructors

def quality(*, status: str, source: str, generated_at: object, production: bool, data_as_of: object = None,
            source_version: object = None, methodology_version: object = None, coverage: object = None,
            sample_size: object = None, missingness: object = None, limitations: Iterable[str] | None = None) -> dict:
    if status not in QUALITY_STATUSES:
        raise ValueError(f"quality status must be one of {QUALITY_STATUSES}, not {status!r}")
    lims = _strs(limitations)
    if status in (PARTIAL, RESEARCH) and not lims:
        raise ValueError(f"a {status} quality object must state its limitations")
    out = {
        "status": status, "source": str(source), "source_version": _str(source_version),
        "methodology_version": _str(methodology_version), "production": bool(production),
        "generated_at": to_iso(generated_at), "data_as_of": to_iso_or_none(data_as_of),
        "coverage": _str(coverage), "sample_size": None if sample_size is None else int(sample_size),
        "missingness": None if missingness is None else round(float(missingness), 6), "limitations": lims,
    }
    validate(out, "quality")
    return out


def link(*, rel: str, target_kind: str, label: str, target_id: object = None, path: object = None) -> dict:
    out = {"rel": rel, "target_kind": target_kind, "target_id": _str(target_id), "label": str(label), "path": _str(path)}
    validate(out, "link")
    return out


def window(kind: str = "SEASON", *, label: str | None = None, n: object = None, start: object = None,
           end: object = None) -> dict:
    if kind == "LAST_N" and n is None:
        raise ValueError("a LAST_N window needs n")
    if label is None:
        label = {"SEASON": "SEASON", "LAST_N": f"L{n}", "GAME": "GAME", "RUN": "RUN"}.get(kind)
        if label is None:
            label = f"{to_date(start)}..{to_date(end)}" if start or end else kind
    return {"kind": kind, "n": None if n is None else int(n), "start": to_iso_or_none(start),
            "end": to_iso_or_none(end), "label": str(label)}


def split(dimension: str, value: object) -> dict:
    return {"dimension": str(dimension), "value": str(value)}


def _split_label(sp: dict | None) -> str | None:
    return None if not sp else f"{sp['dimension']}={sp['value']}"


def observation(*, sport: str, metric_id: str, entity_id: str, entity_type: str, value: object, window: dict,
                as_of: object, source: str, quality_status: str, unit: object = None, adjusted_value: object = None,
                display_value: object = None, split: dict | None = None, sample_size: object = None,
                season: object = None, event_id: object = None, opponent_id: object = None,
                context: dict | None = None, extensions: dict | None = None) -> dict:
    sport = ids.normalize_sport(sport)
    as_of_iso = to_iso(as_of)
    out = {
        "observation_id": ids.observation_id(sport, metric_id, entity_id, window["label"], _split_label(split), as_of_iso),
        "metric_id": metric_id, "sport": sport, "entity_id": entity_id, "entity_type": entity_type,
        "value": _num(value), "adjusted_value": _num(adjusted_value), "display_value": _str(display_value),
        "unit": _str(unit), "window": window, "split": split, "sample_size": None if sample_size is None else int(sample_size),
        "as_of": as_of_iso, "season": _str(season), "event_id": _str(event_id), "opponent_id": _str(opponent_id),
        "context": context, "source": str(source), "quality_status": quality_status, "extensions": dict(extensions or {}),
    }
    validate(out, "observation")
    return out


SUPPORT_KEYS = ("rank", "percentile", "time_series", "windows", "splits", "opponent_adjustment",
                "schedule_adjustment", "home_away", "game_state")


def supports(**flags: bool) -> dict:
    unknown = set(flags) - set(SUPPORT_KEYS)
    if unknown:
        raise ValueError(f"unknown supports flags {sorted(unknown)}")
    return {k: bool(flags.get(k, False)) for k in SUPPORT_KEYS}


def metric(*, sport: str, slug: str, name: str, short_name: str, description: str, entity_type: str, category: str,
           stat_type: str, source: str, quality: dict, freshness: str, higher_is_better: object = None,
           subcategory: object = None, unit: object = None, comparison_universe: object = None,
           supports: dict | None = None, windows: Iterable[str] | None = None, splits: Iterable[str] | None = None,
           source_version: object = None, methodology_version: object = None, historical_start: object = None,
           update_frequency: object = None, known_limitations: Iterable[str] | None = None,
           related_metrics: Iterable[str] | None = None, extensions: dict | None = None) -> dict:
    out = {
        "metric_id": ids.metric_id(sport, slug), "sport": ids.normalize_sport(sport), "name": str(name),
        "short_name": str(short_name), "description": str(description), "entity_type": entity_type,
        "category": str(category), "subcategory": _str(subcategory), "unit": _str(unit), "stat_type": stat_type,
        "higher_is_better": None if higher_is_better is None else bool(higher_is_better),
        "comparison_universe": _str(comparison_universe), "supports": supports or globals()["supports"](),
        "windows": _strs(windows), "splits": _strs(splits), "source": str(source), "source_version": _str(source_version),
        "methodology_version": _str(methodology_version), "quality": quality,
        "historical_start": to_date(historical_start) if historical_start else None,
        "update_frequency": _str(update_frequency), "freshness": freshness,
        "known_limitations": _strs(known_limitations), "related_metrics": _strs(related_metrics),
        "extensions": dict(extensions or {}),
    }
    validate(out, "metric")
    return out


def metric_registry(*, sport: str, run_id: str, generated_at: object, metrics: list[dict]) -> dict:
    ordered = sorted(metrics, key=lambda m: m["metric_id"])
    seen = set()
    for m in ordered:
        if m["metric_id"] in seen:
            raise ValueError(f"duplicate metric {m['metric_id']}")
        seen.add(m["metric_id"])
    out = _envelope("metric_registry", sport, run_id, generated_at)
    out.update({"count": len(ordered), "items": ordered})
    validate(out, "metric_registry")
    return out


# ------------------------------------------------------------------ rankings (arithmetic only)

def _rank_entries(values: list[tuple[str, float]], higher_is_better: bool | None) -> list[tuple[int, str, float, float]]:
    """(rank, entity_id, value, percentile). Ties share the best rank (competition ranking); the
    percentile is the share of the universe this entity beats or ties, 100 = best."""
    if not values:
        return []
    reverse = higher_is_better is not False  # None ranks like "higher is better" but says so in the ranking
    ordered = sorted(values, key=lambda kv: ((-kv[1]) if reverse else kv[1], kv[0]))
    out = []
    n = len(ordered)
    rank = 0
    prev = None
    for i, (eid, v) in enumerate(ordered):
        if prev is None or v != prev:
            rank = i + 1
            prev = v
        better = sum(1 for _, other in ordered if (other > v if reverse else other < v))
        out.append((rank, eid, v, round(100.0 * (n - better) / n, 2)))
    return out


def ranking(*, sport: str, metric_id: str, universe_label: str, entity_type: str, window: dict, as_of: object,
            higher_is_better: bool | None, values: list[dict], run_id: str, generated_at: object, quality: dict,
            season: object = None, universe_filter: object = None, split: dict | None = None,
            path_for: Any = None, links: Iterable[dict] | None = None) -> dict:
    """``values`` rows: ``{entity_id, display_name, short_name?, value, adjusted_value?, sample_size?, path?}``.
    Entities whose value is None are excluded: the UI sees exactly the universe that was ranked."""
    sport = ids.normalize_sport(sport)
    usable = [(r["entity_id"], float(r["value"])) for r in values if r.get("value") is not None]
    by_id = {r["entity_id"]: r for r in values}
    ranked = _rank_entries(usable, higher_is_better)
    vals = [v for _, _, v, _ in ranked]
    entries = []
    for rank, eid, v, pct in ranked:
        row = by_id[eid]
        entries.append({
            "rank": rank, "entity_id": eid, "display_name": str(row["display_name"]), "short_name": _str(row.get("short_name")),
            "value": v, "adjusted_value": _num(row.get("adjusted_value")),
            "sample_size": None if row.get("sample_size") is None else int(row["sample_size"]), "percentile": pct,
            "path": _str(path_for(eid) if path_for else row.get("path")),
        })
    samples = [e["sample_size"] for e in entries if e["sample_size"] is not None]
    summary = {
        "mean": round(statistics.fmean(vals), 6) if vals else None,
        "median": round(statistics.median(vals), 6) if vals else None,
        "min": min(vals) if vals else None, "max": max(vals) if vals else None,
        "stdev": round(statistics.pstdev(vals), 6) if len(vals) > 1 else None,
        "best_entity_id": entries[0]["entity_id"] if entries else None,
        "worst_entity_id": entries[-1]["entity_id"] if entries else None,
        "sample_size_min": min(samples) if samples else None, "sample_size_max": max(samples) if samples else None,
    }
    out = _envelope("ranking", sport, run_id, generated_at)
    out.update({
        "ranking_id": ids.ranking_id(sport, metric_id, universe_label, window["label"], _split_label(split)),
        "metric_id": metric_id,
        "universe": {"label": str(universe_label), "entity_type": entity_type, "season": _str(season), "size": len(entries),
                     "filter": _str(universe_filter)},
        "window": window, "split": split, "as_of": to_iso(as_of),
        "higher_is_better": None if higher_is_better is None else bool(higher_is_better),
        "summary": summary, "entries": entries, "quality": quality, "links": list(links or []),
    })
    validate(out, "ranking")
    return out


def context_from_ranking(rk: dict, entity_id: str) -> dict | None:
    """The comparison context an observation carries, read off a ranking document."""
    entry = next((e for e in rk["entries"] if e["entity_id"] == entity_id), None)
    if entry is None:
        return None
    s = rk["summary"]
    hib = rk["higher_is_better"] is not False
    return {
        "rank": entry["rank"], "universe_size": rk["universe"]["size"], "percentile": entry["percentile"],
        "ranking_id": rk["ranking_id"], "universe_label": rk["universe"]["label"],
        "league_average": s["mean"], "league_median": s["median"],
        "best_value": s["max"] if hib else s["min"], "worst_value": s["min"] if hib else s["max"],
        "best_entity_id": s["best_entity_id"], "worst_entity_id": s["worst_entity_id"],
        "higher_is_better": rk["higher_is_better"],
    }


# ------------------------------------------------------------------ time series

def point(*, x: str, t: object, value: object, quality_status: str, event_id: object = None, opponent_id: object = None,
          adjusted_value: object = None, rolling_value: object = None, sample_size: object = None, run_id: object = None,
          source: object = None, path: object = None) -> dict:
    return {"x": str(x), "t": to_iso(t), "event_id": _str(event_id), "opponent_id": _str(opponent_id), "value": _num(value),
            "adjusted_value": _num(adjusted_value), "rolling_value": _num(rolling_value),
            "sample_size": None if sample_size is None else int(sample_size), "run_id": _str(run_id), "source": _str(source),
            "quality_status": quality_status, "path": _str(path)}


def rolling(points: list[dict], n: int, key: str = "value") -> list[dict]:
    """Attach ``rolling_value`` = trailing mean of the last ``n`` non-null values (inclusive). Pure arithmetic."""
    out = []
    buf: list[float] = []
    for p in sorted(points, key=lambda p: (parse_ts(p["t"]), p["x"])):
        q = dict(p)
        if q.get(key) is not None:
            buf.append(float(q[key]))
        tail = buf[-n:]
        q["rolling_value"] = round(statistics.fmean(tail), 6) if tail else None
        out.append(q)
    return out


def time_series(*, sport: str, metric_id: str, entity_id: str, entity_type: str, x_axis: str, points: list[dict],
                as_of: object, run_id: str, generated_at: object, quality: dict, unit: object = None,
                split: dict | None = None, rolling_window: object = None, links: Iterable[dict] | None = None) -> dict:
    sport = ids.normalize_sport(sport)
    ordered = sorted(points, key=lambda p: (parse_ts(p["t"]), p["x"]))
    out = _envelope("time_series", sport, run_id, generated_at)
    out.update({
        "series_id": ids.series_id(sport, metric_id, entity_id, x_axis, _split_label(split)),
        "metric_id": metric_id, "entity_id": entity_id, "entity_type": entity_type, "x_axis": x_axis, "split": split,
        "unit": _str(unit), "as_of": to_iso(as_of), "rolling_window": None if rolling_window is None else int(rolling_window),
        "points": ordered, "quality": quality, "links": list(links or []),
    })
    validate(out, "time_series")
    return out


# ------------------------------------------------------------------ profiles, event research, market history

def _envelope(kind: str, sport: str, run_id: str, generated_at: object) -> dict:
    return {"schema_version": SCHEMA_VERSION, "kind": kind, "sport": ids.normalize_sport(sport), "run_id": run_id,
            "generated_at": to_iso(generated_at)}


def entity_profile(*, sport: str, run_id: str, generated_at: object, entity: dict, entity_type: str, quality: dict,
                   season: object = None, league: object = None, team: dict | None = None, metrics: Iterable[dict] | None = None,
                   splits: dict | None = None, series: Iterable[dict] | None = None, rankings: Iterable[dict] | None = None,
                   games: Iterable[dict] | None = None, players: Iterable[dict] | None = None,
                   opponents: Iterable[dict] | None = None, markets: Iterable[dict] | None = None,
                   projections: Iterable[dict] | None = None, availability: Iterable[dict] | None = None,
                   links: Iterable[dict] | None = None, extensions: dict | None = None) -> dict:
    out = _envelope("entity_profile", sport, run_id, generated_at)
    out.update({
        "entity": entity, "entity_type": entity_type, "season": _str(season), "league": _str(league), "team": team,
        "metrics": list(metrics or []), "splits": {k: list(v) for k, v in (splits or {}).items()},
        "series": list(series or []), "rankings": list(rankings or []),
        "games": sorted((games or []), key=lambda g: (g["start_time_utc"], g["event_id"])), "players": list(players or []),
        "opponents": list(opponents or []), "markets": list(markets or []), "projections": list(projections or []),
        "availability": list(availability or []), "links": list(links or []), "quality": quality,
        "extensions": dict(extensions or {}),
    })
    validate(out, "entity_profile")
    return out


def event_research(*, sport: str, run_id: str, generated_at: object, event: dict, quality: dict,
                   participants: Iterable[dict] | None = None, matchup: Iterable[dict] | None = None,
                   players: Iterable[dict] | None = None, projections: Iterable[dict] | None = None,
                   distributions: Iterable[dict] | None = None, markets: Iterable[dict] | None = None,
                   market_history_path: object = None, context: dict | None = None, wagers: Iterable[str] | None = None,
                   links: Iterable[dict] | None = None, extensions: dict | None = None) -> dict:
    ctx = {"injuries": [], "lineups": [], "weather": None, "venue": None, "notes": []}
    ctx.update(context or {})
    out = _envelope("event_research", sport, run_id, generated_at)
    out.update({
        "event": event, "participants": list(participants or []), "matchup": list(matchup or []),
        "players": list(players or []), "projections": list(projections or []), "distributions": list(distributions or []),
        "markets": list(markets or []), "market_history_path": _str(market_history_path), "context": ctx,
        "wagers": _strs(wagers), "links": list(links or []), "quality": quality, "extensions": dict(extensions or {}),
    })
    validate(out, "event_research")
    return out


def market_history(*, sport: str, run_id: str, generated_at: object, event_id: str, as_of: object, series: list[dict],
                   quality: dict, links: Iterable[dict] | None = None) -> dict:
    cleaned = []
    for s_ in sorted(series, key=lambda r: r["market_id"]):
        pts = sorted(s_["points"], key=lambda p: parse_ts(p["captured_at"]))
        cleaned.append({"market_id": s_["market_id"], "kalshi_ticker": s_["kalshi_ticker"], "points": pts})
    out = _envelope("market_history", sport, run_id, generated_at)
    out.update({"event_id": event_id, "as_of": to_iso(as_of), "series": cleaned, "quality": quality, "links": list(links or [])})
    validate(out, "market_history")
    return out


def price_point(*, captured_at: object, yes_bid: object = None, yes_ask: object = None, last_price: object = None,
                volume: object = None, open_interest: object = None, source: object = None) -> dict:
    return {"captured_at": to_iso(captured_at), "yes_bid": _num(yes_bid), "yes_ask": _num(yes_ask), "last_price": _num(last_price),
            "volume": _num(volume), "open_interest": _num(open_interest), "source": _str(source)}


def market_ref(m: dict) -> dict:
    """A compact market reference from a v1 market object."""
    return {"market_id": m["market_id"], "kalshi_ticker": m["kalshi_ticker"], "event_id": m.get("event_id"),
            "market_family": m["market_family"], "yes_description": m["yes_description"],
            "market_probability": m.get("market_probability"), "yes_bid": m.get("yes_bid"), "yes_ask": m.get("yes_ask"),
            "captured_at": m.get("captured_at"), "participant_id": m.get("participant_id"), "player_id": m.get("player_id"),
            "period": m.get("period"), "line": m.get("line"), "threshold": m.get("threshold")}


def projection_ref(mp: dict, *, research_only: bool, authority: str, quality_status: str, metric_id: object = None) -> dict:
    """A compact projection reference from a v1 model price."""
    return {"model_price_id": mp.get("model_price_id"), "market_id": mp.get("market_id"), "event_id": mp.get("event_id"),
            "metric_id": _str(metric_id), "fair_probability": mp.get("fair_probability"),
            "market_probability": mp.get("market_probability"), "edge": mp.get("edge"),
            "projection_value": mp.get("projection_value"), "projection_unit": mp.get("projection_unit"),
            "lower_bound": mp.get("lower_bound"), "upper_bound": mp.get("upper_bound"), "generated_at": mp["generated_at"],
            "run_id": mp.get("run_id"), "model_version": mp.get("model_version"), "research_only": bool(research_only),
            "authority": authority, "quality_status": quality_status}


def game_ref(*, event_id: str, start_time_utc: object, status: str, opponent_id: object = None, opponent_name: object = None,
             home_away: object = None, result: dict | None = None, competition: object = None, path: object = None) -> dict:
    return {"event_id": event_id, "start_time_utc": to_iso(start_time_utc), "opponent_id": _str(opponent_id),
            "opponent_name": _str(opponent_name), "home_away": home_away, "status": str(status), "result": result,
            "competition": _str(competition), "path": _str(path)}


# ------------------------------------------------------------------ capability manifest

def capability(*, capability: str, status: str, summary: str, entity_types: Iterable[str] | None = None,
               reasons: Iterable[str] | None = None, limitations: Iterable[str] | None = None,
               evidence: Iterable[str] | None = None, coverage: object = None, since: object = None,
               metrics: Iterable[str] | None = None, windows: Iterable[str] | None = None,
               splits: Iterable[str] | None = None) -> dict:
    if capability not in CAPABILITIES:
        raise ValueError(f"unknown capability {capability!r}")
    out = {"capability": capability, "status": status, "entity_types": _strs(entity_types), "summary": str(summary),
           "reasons": _strs(reasons), "limitations": _strs(limitations), "evidence": _strs(evidence),
           "coverage": _str(coverage), "since": to_date(since) if since else None, "metrics": _strs(metrics),
           "windows": _strs(windows), "splits": _strs(splits)}
    validate(out, "capability")
    return out


def capability_manifest(*, sport: str, run_id: str, generated_at: object, capabilities: list[dict], audit_date: object,
                        split_dimensions: Iterable[dict] | None = None, windows: Iterable[dict] | None = None,
                        notes: Iterable[str] | None = None) -> dict:
    """Every capability in the vocabulary must be answered; a missing answer is UNKNOWN, never silence."""
    answered = {c["capability"]: c for c in capabilities}
    items = []
    for name in CAPABILITIES:
        if name in answered:
            items.append(answered[name])
        else:
            items.append(capability(capability=name, status=UNKNOWN, summary="not assessed in this publication"))
    out = _envelope("capability_manifest", sport, run_id, generated_at)
    out.update({"count": len(items), "items": items, "split_dimensions": list(split_dimensions or []),
                "windows": list(windows or []), "audit_date": to_date(audit_date), "notes": _strs(notes)})
    validate(out, "capability_manifest")
    return out


def check_capabilities(manifest: dict, files: dict[str, dict]) -> list[str]:
    """Truthfulness: VERIFIED / PARTIAL need evidence that exists; UNAVAILABLE must not be contradicted by
    published files of the proving kind. ``files`` is the explorer index file table (explorer-relative
    path -> entry, entries of entity profiles carrying ``entity_type``)."""
    problems = []
    kinds_present = set()
    for entry in files.values():
        kinds_present.add(entry["kind"])
        if entry["kind"] == "entity_profile" and entry.get("entity_type"):
            kinds_present.add(f"entity_profile:{entry['entity_type']}")
    for item in manifest["items"]:
        name, status = item["capability"], item["status"]
        proof_kinds = CAPABILITY_EVIDENCE_KINDS.get(name, ())
        if status in (VERIFIED, PARTIAL):
            if not item["evidence"]:
                problems.append(f"{name}: {status} without evidence paths")
            for path in item["evidence"]:
                rel = path[len(EXPLORER_DIR) + 1:] if path.startswith(EXPLORER_DIR + "/") else path
                if rel not in files:
                    problems.append(f"{name}: evidence {path} is not a published explorer file")
            if status == PARTIAL and not item["limitations"]:
                problems.append(f"{name}: PARTIAL without limitations")
            for k in proof_kinds:
                if k not in kinds_present:
                    problems.append(f"{name}: {status} but no {k} file is published")
        elif status == UNAVAILABLE:
            for k in proof_kinds:
                if k in kinds_present:
                    problems.append(f"{name}: UNAVAILABLE but {k} files are published")
            if item["evidence"]:
                problems.append(f"{name}: UNAVAILABLE with evidence paths")
    return problems


# ------------------------------------------------------------------ search index

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokens(*texts: object) -> list[str]:
    """Lowercase alphanumeric tokens across the inputs, deduplicated and sorted (stable output)."""
    found: set[str] = set()
    for t in texts:
        if t is None:
            continue
        for tok in _TOKEN_RE.findall(str(t).lower()):
            if len(tok) >= 2 or tok.isdigit():
                found.add(tok)
    return sorted(found)


def search_entry(*, id: str, kind: str, label: str, path: str, sport: str, secondary: object = None,
                 aliases: Iterable[str] | None = None, team: object = None, position: object = None,
                 league: object = None, season: object = None) -> dict:
    al = _strs(aliases)
    out = {"id": str(id), "kind": kind, "label": str(label), "secondary": _str(secondary), "aliases": al,
           "tokens": tokens(label, secondary, *al, team, position), "path": str(path), "sport": ids.normalize_sport(sport),
           "context": {"team": _str(team), "position": _str(position), "league": _str(league), "season": _str(season)}}
    validate(out, "search_entry")
    return out


def search_index(*, sport: str, run_id: str, generated_at: object, entries: list[dict]) -> dict:
    ordered = sorted(entries, key=lambda e: (e["kind"], e["label"].lower(), e["id"]))
    out = _envelope("search_index", sport, run_id, generated_at)
    out.update({"count": len(ordered), "items": ordered})
    validate(out, "search_index")
    return out


# ------------------------------------------------------------------ explorer publication

class ExplorerError(RuntimeError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems[:10]) + (" ..." if len(problems) > 10 else ""))
        self.problems = problems


def path_for(doc: dict) -> str:
    """The explorer-relative path a document is published at."""
    kind = doc["kind"]
    if kind == "entity_profile":
        return f"{SUBDIRS['entity_profile:' + doc['entity_type']]}/{doc['entity']['participant_id']}.json"
    if kind == "event_research":
        return f"{SUBDIRS[kind]}/{doc['event']['event_id']}.json"
    if kind == "ranking":
        return f"{SUBDIRS[kind]}/{doc['ranking_id']}.json"
    if kind == "time_series":
        return f"{SUBDIRS[kind]}/{doc['series_id']}.json"
    if kind == "market_history":
        return f"{SUBDIRS[kind]}/{doc['event_id']}.json"
    if kind == "capability_manifest":
        return CAPABILITIES_NAME
    if kind == "metric_registry":
        return METRICS_NAME
    if kind == "search_index":
        return SEARCH_NAME
    raise ValueError(f"not an explorer document kind: {kind}")


def app_path(explorer_relative: str) -> str:
    """App-root-relative path (what ``links[].path`` and v1 ``detail_path`` use)."""
    return f"{EXPLORER_DIR}/{explorer_relative}"


def team_path(participant_id: str) -> str:
    return app_path(f"{SUBDIRS['entity_profile:TEAM']}/{participant_id}.json")


def player_path(participant_id: str) -> str:
    return app_path(f"{SUBDIRS['entity_profile:PLAYER']}/{participant_id}.json")


def event_path(event_id: str) -> str:
    return app_path(f"{SUBDIRS['event_research']}/{event_id}.json")


def ranking_path(ranking_id: str) -> str:
    return app_path(f"{SUBDIRS['ranking']}/{ranking_id}.json")


def series_path(series_id: str) -> str:
    return app_path(f"{SUBDIRS['time_series']}/{series_id}.json")


def market_history_path(event_id: str) -> str:
    return app_path(f"{SUBDIRS['market_history']}/{event_id}.json")


def _entity_id_of(doc: dict) -> str | None:
    kind = doc["kind"]
    if kind == "entity_profile":
        return doc["entity"]["participant_id"]
    if kind == "event_research":
        return doc["event"]["event_id"]
    if kind == "market_history":
        return doc["event_id"]
    if kind == "ranking":
        return doc["ranking_id"]
    if kind == "time_series":
        return doc["series_id"]
    return None


def build_index(*, sport: str, run_id: str, generated_at: object, documents: dict[str, dict], texts: dict[str, str],
                quality: dict, as_of: object = None, commit_sha: object = None, base_manifest_run_id: object = None,
                windows: Iterable[dict] | None = None, warnings: Iterable[str] | None = None) -> dict:
    files = {}
    for rel, doc in documents.items():
        files[rel] = {"kind": doc["kind"], "sha256": sha256_text(texts[rel]), "bytes": len(texts[rel].encode("utf-8")),
                      "entity_id": _entity_id_of(doc)}
    counts = {"teams": 0, "players": 0, "events": 0, "rankings": 0, "series": 0, "market_history": 0, "metrics": 0}
    teams, players_by_team, events = [], {}, []
    for rel, doc in documents.items():
        k = doc["kind"]
        if k == "entity_profile" and doc["entity_type"] == "TEAM":
            counts["teams"] += 1
            e = doc["entity"]
            teams.append({"participant_id": e["participant_id"], "display_name": e["display_name"],
                          "short_name": e.get("short_name"), "path": app_path(rel)})
        elif k == "entity_profile":
            counts["players"] += 1
            tid = (doc.get("team") or {}).get("participant_id") or "_none"
            players_by_team.setdefault(tid, []).append(doc["entity"]["participant_id"])
        elif k == "event_research":
            counts["events"] += 1
            ev = doc["event"]
            events.append({"event_id": ev["event_id"], "start_time_utc": ev["start_time_utc"],
                           "home_participant": ev.get("home_participant"), "away_participant": ev.get("away_participant"),
                           "participants": [p["participant_id"] for p in ev["participants"]], "status": ev["status"],
                           "path": app_path(rel)})
        elif k == "ranking":
            counts["rankings"] += 1
        elif k == "time_series":
            counts["series"] += 1
        elif k == "market_history":
            counts["market_history"] += 1
        elif k == "metric_registry":
            counts["metrics"] = doc["count"]
    out = _envelope("explorer_index", sport, run_id, generated_at)
    out.update({
        "commit_sha": _str(commit_sha), "as_of": to_iso_or_none(as_of), "base_manifest_run_id": _str(base_manifest_run_id),
        "counts": counts, "files": dict(sorted(files.items())),
        "capabilities_path": app_path(CAPABILITIES_NAME), "metrics_path": app_path(METRICS_NAME),
        "search_index_path": app_path(SEARCH_NAME),
        "teams": sorted(teams, key=lambda t: (t["display_name"], t["participant_id"])),
        "players_by_team": {k: sorted(v) for k, v in sorted(players_by_team.items())},
        "events": sorted(events, key=lambda e: (e["start_time_utc"], e["event_id"])),
        "windows": list(windows or []), "quality": quality, "warnings": _strs(warnings),
    })
    validate(out, "explorer_index")
    return out


def _referenced_paths(doc: dict) -> Iterable[str]:
    for ln in doc.get("links", []) or []:
        if ln.get("path"):
            yield ln["path"]
    for key in ("series", "rankings", "games", "players", "opponents", "participants", "entries", "points", "teams", "events"):
        for row in doc.get(key, []) or []:
            if isinstance(row, dict) and row.get("path"):
                yield row["path"]
    if doc.get("team") and doc["team"].get("path"):
        yield doc["team"]["path"]
    if doc.get("market_history_path"):
        yield doc["market_history_path"]


def check_graph(documents: dict[str, dict], index_files: dict[str, dict]) -> list[str]:
    """No dead ends: every path referenced by any document exists in the file table; every observation's
    metric exists in the registry; every ranking_id in an observation context is published; series
    points are ordered; entries are ordered by rank; search entries point at published files."""
    problems = []
    published = {app_path(rel) for rel in index_files}
    registry = next((d for d in documents.values() if d["kind"] == "metric_registry"), None)
    metric_ids = {m["metric_id"] for m in registry["items"]} if registry else set()
    rankings = {d["ranking_id"] for d in documents.values() if d["kind"] == "ranking"}
    for rel, doc in documents.items():
        for path in _referenced_paths(doc):
            if path not in published:
                problems.append(f"{rel}: dead link {path}")
        if doc["kind"] == "search_index":
            for e in doc["items"]:
                if e["path"] not in published:
                    problems.append(f"{rel}: search entry {e['id']} points at unpublished {e['path']}")
        obs_lists = [doc.get("metrics", [])] + list((doc.get("splits") or {}).values())
        for row in doc.get("matchup", []) or []:
            obs_lists.append([o for o in (row.get("home"), row.get("away")) if o])
        for obs_list in obs_lists:
            for o in obs_list:
                if o["metric_id"] not in metric_ids:
                    problems.append(f"{rel}: observation metric {o['metric_id']} is not in the registry")
                ctx = o.get("context")
                if ctx and ctx.get("ranking_id") and ctx["ranking_id"] not in rankings:
                    problems.append(f"{rel}: context ranking {ctx['ranking_id']} is not published")
        for key in ("series", "rankings"):
            for row in doc.get(key, []) or []:
                if row.get("metric_id") and row["metric_id"] not in metric_ids:
                    problems.append(f"{rel}: {key} metric {row['metric_id']} is not in the registry")
        if doc["kind"] in ("ranking", "time_series") and doc["metric_id"] not in metric_ids:
            problems.append(f"{rel}: metric {doc['metric_id']} is not in the registry")
        if doc["kind"] == "time_series":
            ts_ = [parse_ts(p["t"]) for p in doc["points"]]
            if ts_ != sorted(ts_):
                problems.append(f"{rel}: points are not in time order")
        if doc["kind"] == "ranking":
            ranks = [e["rank"] for e in doc["entries"]]
            if ranks != sorted(ranks):
                problems.append(f"{rel}: entries are not in rank order")
        if doc["kind"] == "market_history":
            for s_ in doc["series"]:
                ts_ = [parse_ts(p["captured_at"]) for p in s_["points"]]
                if ts_ != sorted(ts_):
                    problems.append(f"{rel}: {s_['market_id']} points are not in time order")
    return problems


def _files_with_types(index: dict, docs: dict[str, dict]) -> dict[str, dict]:
    files = {rel: dict(e) for rel, e in index["files"].items()}
    for rel, entry in files.items():
        if entry["kind"] == "entity_profile":
            entry["entity_type"] = docs[rel]["entity_type"]
    return files


def publish_explorer(*, app_root: Path, sport: str, run_id: str, generated_at: object, documents: Iterable[dict],
                     quality: dict, as_of: object = None, commit_sha: object = None, base_manifest_run_id: object = None,
                     windows: Iterable[dict] | None = None, warnings: Iterable[str] | None = None,
                     compact: bool = True) -> dict:
    """Validate every document, check the graph and the capability manifest, write everything to a
    staging directory, then swap it into ``<app_root>/explorer`` with ``index.json`` last. On any
    problem the previous explorer tree is untouched. Returns the index."""
    app_root = Path(app_root)
    docs: dict[str, dict] = {}
    problems: list[str] = []
    for doc in documents:
        try:
            validate(doc, doc["kind"])
        except SchemaError as exc:
            problems.extend(f"{doc.get('kind')}: {e}" for e in exc.errors[:10])
            continue
        if doc.get("run_id") != run_id:
            problems.append(f"{doc['kind']}: run_id {doc.get('run_id')} != {run_id}")
        rel = path_for(doc)
        if rel in docs:
            problems.append(f"duplicate explorer path {rel}")
        docs[rel] = doc
    required = {CAPABILITIES_NAME: "capability_manifest", METRICS_NAME: "metric_registry", SEARCH_NAME: "search_index"}
    for name, kind in required.items():
        if name not in docs:
            problems.append(f"missing {kind} ({name})")
    if problems:
        raise ExplorerError(problems)
    texts = {rel: dumps(doc, compact=compact) for rel, doc in docs.items()}
    index = build_index(sport=sport, run_id=run_id, generated_at=generated_at, documents=docs, texts=texts, quality=quality,
                        as_of=as_of, commit_sha=commit_sha, base_manifest_run_id=base_manifest_run_id, windows=windows,
                        warnings=warnings)
    problems = check_graph(docs, index["files"]) + check_capabilities(docs[CAPABILITIES_NAME], _files_with_types(index, docs))
    if problems:
        raise ExplorerError(problems)
    texts[INDEX_NAME] = dumps(index, compact=False)

    target = app_root / EXPLORER_DIR
    app_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".explorer-staging-", dir=str(app_root)))
    try:
        for rel, text in texts.items():
            dst = staging / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(text, encoding="utf-8")
        previous = {str(p.relative_to(target)).replace("\\", "/") for p in target.rglob("*.json")} if target.exists() else set()
        ordered = [rel for rel in texts if rel != INDEX_NAME] + [INDEX_NAME]
        for rel in ordered:
            dst = target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging / rel, dst)
        for stale in sorted(previous - set(texts)):
            (target / stale).unlink(missing_ok=True)
        for sub in sorted((p for p in target.rglob("*") if p.is_dir()), reverse=True):
            if not any(sub.iterdir()):
                sub.rmdir()
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return index


def read_index(app_root: Path) -> dict | None:
    path = Path(app_root) / EXPLORER_DIR / INDEX_NAME
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_explorer(app_root: Path) -> tuple[dict, dict[str, dict]]:
    """The index and every document it names (explorer-relative path -> document)."""
    index = read_index(app_root)
    if index is None:
        raise ExplorerError(["no explorer/index.json"])
    docs = {}
    for rel in index["files"]:
        docs[rel] = json.loads((Path(app_root) / EXPLORER_DIR / rel).read_text(encoding="utf-8"))
    return index, docs


def verify_explorer(app_root: Path) -> list[str]:
    """Re-validate a published explorer tree: index, digests, every document, the graph, the manifest."""
    try:
        index, docs = load_explorer(app_root)
    except ExplorerError as exc:
        return exc.problems
    except FileNotFoundError as exc:
        return [f"index names a missing file: {exc.filename}"]
    problems = []
    try:
        validate(index, "explorer_index")
    except SchemaError as exc:
        problems.extend(f"index: {e}" for e in exc.errors[:10])
    for rel, entry in index["files"].items():
        text = (Path(app_root) / EXPLORER_DIR / rel).read_text(encoding="utf-8")
        if sha256_text(text) != entry["sha256"]:
            problems.append(f"{rel}: digest mismatch")
        try:
            validate(docs[rel], docs[rel]["kind"])
        except SchemaError as exc:
            problems.extend(f"{rel}: {e}" for e in exc.errors[:5])
        if docs[rel]["kind"] != entry["kind"]:
            problems.append(f"{rel}: kind {docs[rel]['kind']} != index {entry['kind']}")
    problems.extend(check_graph(docs, index["files"]))
    if CAPABILITIES_NAME in docs:
        problems.extend(check_capabilities(docs[CAPABILITIES_NAME], _files_with_types(index, docs)))
    else:
        problems.append("no capabilities.json")
    return problems


_SECRET_PATTERNS = [re.compile(p) for p in (
    r"ghp_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{20,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"(?i)\b(api[_-]?key|secret|token|password)\b\s*[:=]\s*[\"']?[A-Za-z0-9+/=_-]{16,}",
)]


def no_secret_shaped_strings(app_root: Path) -> list[str]:
    """Paths of explorer files containing token/key shaped strings (the same scan the app export runs)."""
    hits = []
    for path in sorted((Path(app_root) / EXPLORER_DIR).rglob("*.json")):
        text = path.read_text(encoding="utf-8")
        if any(p.search(text) for p in _SECRET_PATTERNS):
            hits.append(str(path))
    return hits


def digest_tree(app_root: Path) -> str:
    """One digest over the explorer tree (determinism tests)."""
    h = hashlib.sha256()
    for path in sorted((Path(app_root) / EXPLORER_DIR).rglob("*.json")):
        h.update(str(path.relative_to(app_root)).encode("utf-8"))
        h.update(path.read_bytes())
    return h.hexdigest()


def tree_bytes(app_root: Path) -> dict[str, int]:
    """Bytes per explorer subdirectory (payload budgeting)."""
    out: dict[str, int] = {}
    for path in (Path(app_root) / EXPLORER_DIR).rglob("*.json"):
        key = path.relative_to(Path(app_root) / EXPLORER_DIR).parts[0]
        out[key] = out.get(key, 0) + path.stat().st_size
    return dict(sorted(out.items()))
