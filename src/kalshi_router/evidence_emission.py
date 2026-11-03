"""Building and gating the execution-evidence payload a destination's ``execution_evidence_ledger.py`` imports.

THE GATE (destinations first, router last)
------------------------------------------
Emission requires BOTH:

* the operator switch ``ROUTER_EMIT_EXECUTION_EVIDENCE=1`` -- default OFF, and
* a destination profile that declares ``evidence_importer`` and ``evidence_ledger_path`` -- which no profile does until
  that destination has merged the vendored ledger module.

With either missing nothing is built and nothing is sent, so the router can ship ahead of the destinations without a
single destination being asked for something it cannot take. Evidence rows live in their OWN ledger file; a wager row
is never touched, so a destination that has not been updated cannot refuse a wager because of this feature.

PRIVACY: rows carry digests, times, and the market's own closing quote. A raw order or fill id never reaches this module's
output (``execution_evidence`` digests them), and the vendored ledger refuses any row shaped like one.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Mapping

from .closes import ResolvedClose
from .destinations import DestinationProfile
from .execution_evidence import ExecutionEvidence

EMIT_ENV = "ROUTER_EMIT_EXECUTION_EVIDENCE"
SCHEMA_VERSION = "edge_finder.execution_evidence.v1"  # must equal the vendored ledger's SCHEMA_VERSION


def emission_allowed(profile: DestinationProfile, env: Mapping[str, str] | None = None) -> tuple[bool, str]:
    """(allowed, reason). Fail closed: anything but an explicit ``1`` plus a declared importer is a no."""
    env = os.environ if env is None else env
    if env.get(EMIT_ENV) != "1":
        return False, "operator switch ROUTER_EMIT_EXECUTION_EVIDENCE is not 1"
    if not profile.evidence_importer or not profile.evidence_ledger_path:
        return False, "destination has not declared an execution-evidence importer"
    if not any(profile.evidence_ledger_path.startswith(p) for p in profile.committable_prefixes):
        return False, "evidence ledger path is outside the destination's committable prefixes"
    return True, "ok"


def _now_iso(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def execution_row(ev: ExecutionEvidence, now: datetime | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION, "kind": "EXECUTION", "producer": "router",
        "source_bet_key": ev.source_bet_key, "recorded_at": _now_iso(now),
        "order_id_digest": ev.order_id_digest, "fill_id_digests": list(ev.fill_id_digests),
        "fill_count": ev.fill_count, "first_fill_ts": ev.first_fill_ts, "last_fill_ts": ev.last_fill_ts,
        "execution_action": ev.execution_action, "all_taker": ev.all_taker, "flags": list(ev.flags),
    }


def close_row(source_bet_key: str, market_ticker: str, side: str, close: ResolvedClose,
              now: datetime | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION, "kind": "CLOSE", "producer": "router",
        "source_bet_key": source_bet_key, "recorded_at": _now_iso(now),
        "market_ticker": market_ticker, "side": side, "close": close.to_close_body(),
    }


def build_payload(profile: DestinationProfile, rows: list[dict[str, Any]],
                  env: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """``{"evidence": rows}`` when emission is allowed for this destination, else None (nothing is sent)."""
    ok, _ = emission_allowed(profile, env)
    return {"evidence": rows} if ok else None
