"""Temporal linkage: which model price / recommendation existed BEFORE a wager was placed.

Never retroactive. A model record generated after the wager cannot have motivated it, so it is
never linked, and when no qualifying record exists the link is null rather than the nearest one.
"""

from __future__ import annotations

from .timeutil import parse_ts


def _before(candidate_ts: object, placed_at: object) -> bool:
    try:
        return parse_ts(candidate_ts) <= parse_ts(placed_at)
    except ValueError:
        return False


def link_model_price(wager: dict, model_prices: list[dict]) -> dict | None:
    """The newest model price for the wager's market whose inputs_as_of (or generated_at) is at or
    before placed_at. None when there is none."""
    best = None
    for mp in model_prices:
        if mp.get("market_id") != wager.get("market_id"):
            continue
        stamp = mp.get("inputs_as_of") or mp.get("generated_at")
        if not stamp or not _before(stamp, wager["placed_at"]):
            continue
        if best is None or parse_ts(stamp) > parse_ts(best.get("inputs_as_of") or best.get("generated_at")):
            best = mp
    return best


def link_recommendation(wager: dict, recommendations: list[dict]) -> dict | None:
    best = None
    for rec in recommendations:
        if rec.get("market_id") != wager.get("market_id") or rec.get("selection") != wager.get("selection"):
            continue
        if not _before(rec["created_at"], wager["placed_at"]):
            continue
        if best is None or parse_ts(rec["created_at"]) > parse_ts(best["created_at"]):
            best = rec
    return best


def apply_links(wager: dict, model_prices: list[dict], recommendations: list[dict],
                markets: list[dict] | None = None) -> dict:
    """Return a copy of ``wager`` with model_run_id / model_price_id / recommendation_id / linkage
    filled from pre-wager records only."""
    out = dict(wager)
    link = dict(out.get("linkage") or {})
    link["wager_placed_at"] = out["placed_at"]
    mp = link_model_price(out, model_prices)
    if mp is not None:
        out["model_price_id"] = mp["model_price_id"]
        out["model_run_id"] = mp["run_id"]
        link["model_inputs_as_of"] = mp.get("inputs_as_of") or mp.get("generated_at")
    else:
        out["model_price_id"] = None
        out["model_run_id"] = None
        link["model_inputs_as_of"] = None
    rec = link_recommendation(out, recommendations)
    if rec is not None:
        out["recommendation_id"] = rec["recommendation_id"]
        link["recommendation_generated_at"] = rec["created_at"]
    else:
        out["recommendation_id"] = None
        link["recommendation_generated_at"] = None
    market_capture = None
    for m in markets or []:
        if m.get("market_id") == out["market_id"] and m.get("captured_at") and _before(m["captured_at"], out["placed_at"]):
            market_capture = m["captured_at"]
    link["market_captured_at"] = market_capture
    out["linkage"] = link
    return out
