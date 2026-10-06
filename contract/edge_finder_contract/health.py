"""The per-sport health object and the one rule that decides ``overall_status``.

    UNAVAILABLE   no usable payload (export failed and nothing good exists), or required data missing
    STALE         required market or model data is past its stale threshold
    DEGRADED      a component is degraded, or the last export attempt failed but a good payload stands
    RESEARCH_ONLY everything is fresh and the sport's models have no betting authority
    HEALTHY       everything is fresh and something may act on it

It never reports HEALTHY while required data is stale or missing.
"""

from __future__ import annotations

from . import SCHEMA_VERSION, ids
from .freshness import DEFAULT_THRESHOLDS, STALE, UNKNOWN, AGING, FRESH, Thresholds, classify, worst
from .timeutil import age_seconds, now_utc, to_iso, to_iso_or_none
from .validate import validate

OK, DEGRADED, STALE_C, UNAVAILABLE, NOT_APPLICABLE, UNKNOWN_C = (
    "OK", "DEGRADED", "STALE", "UNAVAILABLE", "NOT_APPLICABLE", "UNKNOWN")


def component(as_of: object, *, thresholds: Thresholds, now: object, required: bool = True,
              detail: object = None, degraded: bool = False, applicable: bool = True) -> dict:
    if not applicable:
        return {"status": NOT_APPLICABLE, "as_of": None, "age_seconds": None, "detail": _s(detail)}
    age = age_seconds(as_of, now) if as_of else None
    fresh = classify(age, thresholds)
    if as_of is None:
        status = UNAVAILABLE if required else UNKNOWN_C
    elif fresh == STALE:
        status = STALE_C
    elif degraded:
        status = DEGRADED
    else:
        status = OK
    return {"status": status, "as_of": to_iso_or_none(as_of), "age_seconds": None if age is None else round(age, 1),
            "detail": _s(detail)}


def _s(v: object) -> str | None:
    return None if v is None else str(v)


def overall_status(*, model_status: str, market_data_status: str, bet_authority: str, errors: list[str],
                   payload_available: bool, export_failed: bool, model_required: bool = True,
                   market_required: bool = True) -> str:
    if not payload_available:
        return "UNAVAILABLE"
    required = ([market_data_status] if market_required else []) + ([model_status] if model_required else [])
    if any(s == UNAVAILABLE for s in required):
        return "UNAVAILABLE"
    if any(s == STALE_C for s in required):
        return "STALE"
    if export_failed or errors or any(s in (DEGRADED, UNKNOWN_C) for s in required):
        return "DEGRADED"
    if bet_authority == "RESEARCH_ONLY":
        return "RESEARCH_ONLY"
    return "HEALTHY"


def build_health(*, sport: str, run_id: str, bet_authority: str, last_market_capture: object,
                 last_model_generated: object, last_successful_run: object, payload_run_id: object,
                 payload_available: bool, export_failed: bool = False, commit_sha: object = None,
                 next_scheduled_run: object = None, router_as_of: object = None,
                 settlement_as_of: object = None, model_required: bool = True,
                 router_applicable: bool = True, settlement_applicable: bool = True,
                 thresholds: dict[str, Thresholds] | None = None, warnings=None, errors=None,
                 extra_components: dict[str, dict] | None = None, now: object = None,
                 generated_at: object = None, market_required: bool = True,
                 extensions: dict | None = None) -> dict:
    """``market_required=False`` (contract 1.2.0) is for a sport that publishes no executable markets by
    design (CBB today): the market component is NOT_APPLICABLE and does not decide the overall status.
    ``extensions`` (1.2.0, optional) carries sport-specific operational state; omitted when None."""
    now = now or now_utc()
    th = dict(DEFAULT_THRESHOLDS)
    th.update(thresholds or {})
    warnings = [str(w) for w in (warnings or [])]
    errors = [str(e) for e in (errors or [])]
    comps = {
        "market_data": component(last_market_capture, thresholds=th["market_data"], now=now,
                                 required=market_required, applicable=market_required or last_market_capture is not None),
        "model": component(last_model_generated, thresholds=th["model"], now=now, required=model_required,
                           applicable=model_required),
        "export": component(last_successful_run, thresholds=th["export"], now=now, degraded=export_failed,
                            detail="last export attempt failed; payload is last-known-good" if export_failed else None),
        "router": component(router_as_of, thresholds=th["router"], now=now, required=False, applicable=router_applicable),
        "settlement": component(settlement_as_of, thresholds=th["settlement"], now=now, required=False,
                                applicable=settlement_applicable),
    }
    comps.update(extra_components or {})
    market_age = comps["market_data"]["age_seconds"]
    model_age = comps["model"]["age_seconds"]
    fresh_states = [classify(market_age, th["market_data"])] if market_required or market_age is not None else []
    if model_required:
        fresh_states.append(classify(model_age, th["model"]))
    freshness = worst(*fresh_states) if fresh_states else UNKNOWN
    if freshness == UNKNOWN and market_required and not any(s is None for s in (market_age,)):
        freshness = STALE
    overall = overall_status(model_status=comps["model"]["status"], market_data_status=comps["market_data"]["status"],
                             bet_authority=bet_authority, errors=errors, payload_available=payload_available,
                             export_failed=export_failed, model_required=model_required,
                             market_required=market_required)
    ages = [a for a in (market_age, model_age) if a is not None]
    out = {
        "schema_version": SCHEMA_VERSION, "kind": "health", "sport": ids.normalize_sport(sport),
        "run_id": run_id, "generated_at": to_iso(generated_at or now),
        "overall_status": overall,
        "model_status": comps["model"]["status"],
        "market_data_status": comps["market_data"]["status"],
        "router_status": comps["router"]["status"],
        "settlement_status": comps["settlement"]["status"],
        "freshness_status": freshness,
        "bet_authority": bet_authority,
        "last_successful_run": to_iso_or_none(last_successful_run),
        "last_export_attempt": to_iso(now),
        "payload_run_id": _s(payload_run_id),
        "last_market_capture": to_iso_or_none(last_market_capture),
        "last_model_generated": to_iso_or_none(last_model_generated),
        "next_scheduled_run": to_iso_or_none(next_scheduled_run),
        "data_age_seconds": max(ages) if ages else None,
        "thresholds": {k: v.as_dict() for k, v in th.items()},
        "components": comps,
        "warnings": warnings, "errors": errors,
        "commit_sha": _s(commit_sha),
    }
    if extensions is not None:
        out["extensions"] = dict(extensions)
    validate(out, "health")
    return out
