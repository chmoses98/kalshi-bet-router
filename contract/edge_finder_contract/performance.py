"""wager -> settlement -> P&L, aggregated for the app. Nothing here is estimated: a settlement
without established money counts in ``unknown`` and in ``data_completeness``, never as zero."""

from __future__ import annotations

from collections import defaultdict

from . import SCHEMA_VERSION, ids
from .timeutil import now_utc, parse_ts, to_iso
from .validate import validate

RECENT_N = 50


def _totals() -> dict:
    return {"wagers": 0, "settled": 0, "pending": 0, "won": 0, "lost": 0, "push": 0, "void": 0, "scalar": 0,
            "unknown": 0, "stake": 0.0, "gross_payout": None, "fees": None, "net_pnl": None, "roi": None,
            "open_exposure": 0.0, "settled_with_economics": 0}


def _add(t: dict, wager: dict, settlement: dict | None) -> None:
    t["wagers"] += 1
    t["stake"] = round(t["stake"] + float(wager["stake"]), 4)
    if settlement is None or wager.get("settlement_status") == "PENDING":
        t["pending"] += 1
        t["open_exposure"] = round(t["open_exposure"] + float(wager["stake"]), 4)
        return
    t["settled"] += 1
    result = settlement.get("result", "UNKNOWN")
    key = {"WON": "won", "LOST": "lost", "PUSH": "push", "VOID": "void", "SCALAR": "scalar"}.get(result, "unknown")
    t[key] += 1
    if settlement.get("net_pnl") is not None:
        t["settled_with_economics"] += 1
        t["net_pnl"] = round((t["net_pnl"] or 0.0) + float(settlement["net_pnl"]), 4)
        if settlement.get("gross_payout") is not None:
            t["gross_payout"] = round((t["gross_payout"] or 0.0) + float(settlement["gross_payout"]), 4)
        if settlement.get("fees") is not None:
            t["fees"] = round((t["fees"] or 0.0) + float(settlement["fees"]), 4)


def _finish(t: dict, settled_stake: float) -> dict:
    if t["net_pnl"] is not None and settled_stake > 0:
        t["roi"] = round(t["net_pnl"] / settled_stake, 6)
    return t


def build_performance(*, sport: str, run_id: str, generated_at: object, wagers: list[dict],
                      settlements: list[dict], markets: list[dict], recommendations: list[dict] | None = None,
                      bankroll_history: list[dict] | None = None, bankroll_basis: str | None = None,
                      clv_values: dict[str, float] | None = None, notes: list[str] | None = None) -> dict:
    by_wager = {s["wager_id"]: s for s in settlements}
    family_of = {m["market_id"]: m["market_family"] for m in markets}
    totals = _totals()
    by_family: dict[str, dict] = defaultdict(_totals)
    by_month: dict[str, dict] = defaultdict(_totals)
    by_source: dict[str, dict] = defaultdict(_totals)
    settled_stake = defaultdict(float)
    for w in wagers:
        s = by_wager.get(w["wager_id"]) if w.get("settlement_id") else None
        fam = family_of.get(w["market_id"], "unknown")
        month = w["placed_at"][:7]
        for key, table in (("total", totals), (fam, by_family[fam]), (month, by_month[month]), (w["source"], by_source[w["source"]])):
            _add(table, w, s)
            if s is not None and s.get("net_pnl") is not None:
                settled_stake[key] += float(w["stake"])
    _finish(totals, settled_stake["total"])
    for name, table in by_family.items():
        _finish(table, settled_stake[name])
    for name, table in by_month.items():
        _finish(table, settled_stake[name])
    for name, table in by_source.items():
        _finish(table, settled_stake[name])
    wagers_by_id = {w["wager_id"]: w for w in wagers}
    recent = sorted((s for s in settlements if s["wager_id"] in wagers_by_id), key=lambda s: s["settled_at"], reverse=True)[:RECENT_N]
    pending = sorted((w for w in wagers if w.get("settlement_status") == "PENDING"), key=lambda w: w["placed_at"], reverse=True)
    linked = sum(1 for w in wagers if w.get("recommendation_id"))
    clv = clv_values or {}
    out = {
        "schema_version": SCHEMA_VERSION, "kind": "performance", "sport": ids.normalize_sport(sport), "run_id": run_id,
        "generated_at": to_iso(generated_at), "as_of": to_iso(generated_at),
        "totals": totals,
        "by_market_family": dict(sorted(by_family.items())),
        "by_month": dict(sorted(by_month.items())),
        "by_source": dict(sorted(by_source.items())),
        "recent_results": [{"wager_id": s["wager_id"], "settlement_id": s["settlement_id"], "market_id": s["market_id"],
                            "kalshi_ticker": wagers_by_id[s["wager_id"]]["kalshi_ticker"],
                            "selection": wagers_by_id[s["wager_id"]]["selection"], "result": s["result"],
                            "net_pnl": s.get("net_pnl"), "settled_at": s["settled_at"]} for s in recent],
        "pending_wagers": [{"wager_id": w["wager_id"], "market_id": w["market_id"], "kalshi_ticker": w["kalshi_ticker"],
                            "selection": w["selection"], "stake": float(w["stake"]), "placed_at": w["placed_at"]} for w in pending],
        "recommended_vs_wagered": {"recommendations": len(recommendations or []), "wagers_linked_to_recommendation": linked,
                                   "wagers_unlinked": len(wagers) - linked},
        "clv": {"available": bool(clv), "wagers_with_clv": len(clv),
                "mean_clv": round(sum(clv.values()) / len(clv), 6) if clv else None},
        "bankroll": {"available": bool(bankroll_history), "basis": bankroll_basis,
                     "history": [{"as_of": to_iso(h["as_of"]), "balance": float(h["balance"])} for h in (bankroll_history or [])]},
        "data_completeness": {
            "wagers_with_fees": sum(1 for w in wagers if w.get("fees") is not None),
            "wagers_without_fees": sum(1 for w in wagers if w.get("fees") is None),
            "settlements_with_economics": sum(1 for s in settlements if s.get("net_pnl") is not None),
            "settlements_without_economics": sum(1 for s in settlements if s.get("net_pnl") is None),
            "wagers_with_event": sum(1 for w in wagers if w.get("event_id")),
            "clv_available": bool(clv),
        },
        "notes": [str(n) for n in (notes or [])],
    }
    validate(out, "performance")
    return out
