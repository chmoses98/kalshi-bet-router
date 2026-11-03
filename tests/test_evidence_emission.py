"""Destinations first, router last: nothing is emitted unless BOTH the switch and the destination say so."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone


from kalshi_router import evidence_emission as em
from kalshi_router.closes import ResolvedClose, TRUE_CLOSE
from kalshi_router.destinations import PROFILES
from kalshi_router.execution_evidence import ExecutionEvidence

NOW = datetime(2026, 11, 2, 23, 0, tzinfo=timezone.utc)


def test_no_destination_profile_accepts_evidence_yet():
    # Flip a profile only in the change that follows that destination merging the vendored ledger module.
    for sport, profile in PROFILES.items():
        assert profile.evidence_importer is None and profile.evidence_ledger_path is None, sport
        ok, why = em.emission_allowed(profile, {"ROUTER_EMIT_EXECUTION_EVIDENCE": "1"})
        assert not ok and "not declared" in why


def test_the_switch_is_off_by_default_and_only_the_literal_1_turns_it_on():
    profile = next(iter(PROFILES.values()))
    ready = dataclasses.replace(profile, evidence_importer=("python", "x"),
                                evidence_ledger_path=profile.committable_prefixes[0] + "execution_evidence.jsonl")
    assert not em.emission_allowed(ready, {})[0]
    for v in ("0", "true", "yes", "", "1 "):
        assert not em.emission_allowed(ready, {em.EMIT_ENV: v})[0], v
    assert em.emission_allowed(ready, {em.EMIT_ENV: "1"})[0]


def test_a_ledger_path_outside_the_committable_prefixes_is_refused():
    profile = next(iter(PROFILES.values()))
    bad = dataclasses.replace(profile, evidence_importer=("python", "x"), evidence_ledger_path="elsewhere/e.jsonl")
    ok, why = em.emission_allowed(bad, {em.EMIT_ENV: "1"})
    assert not ok and "committable" in why


def test_payload_is_none_when_not_allowed_and_wrapped_when_allowed():
    profile = next(iter(PROFILES.values()))
    rows = [{"kind": "EXECUTION"}]
    assert em.build_payload(profile, rows, {em.EMIT_ENV: "1"}) is None
    ready = dataclasses.replace(profile, evidence_importer=("python", "x"),
                                evidence_ledger_path=profile.committable_prefixes[0] + "e.jsonl")
    assert em.build_payload(ready, rows, {em.EMIT_ENV: "1"}) == {"evidence": rows}
    assert em.build_payload(ready, rows, {}) is None


def test_rows_built_from_real_objects_validate_against_the_vendored_ledger():
    import importlib.util, sys
    from pathlib import Path
    mod = Path(__file__).resolve().parents[1] / "contract" / "standalone" / "execution_evidence_ledger.py"
    spec = importlib.util.spec_from_file_location("eel2", mod)
    L = importlib.util.module_from_spec(spec)
    sys.modules["eel2"] = L
    spec.loader.exec_module(L)
    assert em.SCHEMA_VERSION == L.SCHEMA_VERSION
    key = "kalshi:v1:" + "ab" * 32
    ev = ExecutionEvidence(source_bet_key=key, order_id_digest="a" * 32, fill_id_digests=("b" * 32,), fill_count=1,
                           first_fill_ts=1_790_000_000, last_fill_ts=1_790_000_001, execution_action="BUY",
                           all_taker=True, flags=())
    assert L.validate_row(em.execution_row(ev, NOW)) == []
    from decimal import Decimal
    rc = ResolvedClose(TRUE_CLOSE, 1_790_000_100, 1, "series_market_candlesticks", as_of_ts=1_790_000_060,
                       yes_bid=Decimal("0.48"), yes_ask=Decimal("0.52"), minutes_before_cutoff=0)
    assert L.validate_row(em.close_row(key, "KXA-1", "YES", rc, NOW)) == []


def test_no_payload_is_built_for_any_destination_even_with_the_switch_on():
    # The compatibility record is the profile's own declaration of an importer. Until a destination has merged the
    # vendored ledger and its profile says so, nothing is built for it, switch or no switch.
    for sport, profile in PROFILES.items():
        assert em.build_payload(profile, [{"x": 1}], {em.EMIT_ENV: "1"}) is None, sport
        assert em.build_payload(profile, [{"x": 1}], {}) is None, sport
