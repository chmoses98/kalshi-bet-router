"""The 2026-10-08 router-health repair: what the 30 BLOCKED wagers were, and what NHL/SOCCER settlements are.

PRODUCTION STATE (delivery run 37714804665, settlement run 37713992507):

    BLOCKED: 30   refused_sport_unresolved: 29 (unresolved_competition_absent: 29)
                  refused_game_date_not_established: 1
    NHL: 19 wagers proposed-not-merged, 19 settlements REFUSED (await their wager on the canonical ledger)
    SOCCER: 9 wagers proposed-not-merged, 9 settlements REFUSED

The 29 were all multivariate COMBO markets ("Exotics"; `mve_selected_legs`), every leg of which the router's own
profiler had already classified -- NFL 24, CFB 4, MLB 1. The combo's own event names no competition by design.
`classify.classify_with_legs` lets a combo inherit a UNANIMOUS, COMPLETE leg verdict, and nothing weaker.

The NHL/SOCCER settlements were sequencing, not failure: every wager imported cleanly onto a router proposal that
passes every gate condition and is held only by the destinations' observation period. They are now
WAITING_FOR_PARENT_WAGER (`kalshi_router.settlement_parents`), which is visible, re-offered, and not a failure.

Numbered as the mission's required coverage (1-12). End-to-end coverage of 2, 6, 7 and 8 through the COMMITTED
settle bash is in tests/test_settlement_unmatched_parents.py (W1-W4).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from kalshi_router.classify import (
    EvidenceLevel,
    MarketContext,
    UnresolvedReason,
    classify_market,
    classify_with_legs,
)
from kalshi_router.destinations import PROFILES, combo_destination_names, profile_for
from kalshi_router.production import (
    NEEDS_ATTENTION_REFUSALS,
    ProductionDiagnostics,
    ProductionRefusal,
)
from kalshi_router.settlement_parents import (
    CANONICAL_PARENT,
    NO_VALID_PARENT,
    WAITING_FOR_PARENT_WAGER,
    Proposal,
    partition,
)
from kalshi_router.sports import Sport
from kalshi_router.wager import GameDateSource, resolve_combo_game_date

from .synthetic import SENSITIVE_TOKENS, make_event, make_event_metadata, make_fill, make_market, make_series
from .test_audit import build_metadata
from .test_cli import install_fake_api, local_env, run  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "contract"))
_spec = importlib.util.spec_from_file_location("publish_router_health", ROOT / "scripts" / "publish_router_health.py")
prh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prh)

from edge_finder_contract.validate import validate  # noqa: E402

REPO = "chmoses98/NHL-edge-finder"
LEDGER = "accounting-data"
BRANCH = "kalshi-router/NHL"
SHA = "6ec536a48467fe97ccc8535bf3474427f85dc399"


# ------------------------------------------------------------------------------- settlement parents (1-8)

def _pull(**over):
    pull = {"number": 8, "state": "open", "draft": False,
            "head": {"ref": BRANCH, "sha": SHA, "repo": {"full_name": REPO}}, "base": {"ref": LEDGER}}
    pull.update(over)
    return pull


def _proposal(records, **pull_over):
    return Proposal(branch=BRANCH, fetched_sha=SHA, pull=_pull(**pull_over), records=tuple(records))


def _wager(key, ticker="KXNHLGAME-26OCT02SYNAHOM-HOM", side="YES"):
    return {"source_bet_key": key, "market_ticker": ticker, "side": side, "wager_id": f"nhlw-{key}"}


def _settled(key, ticker="KXNHLGAME-26OCT02SYNAHOM-HOM", side="YES"):
    return {"source_bet_key": key, "market_ticker": ticker, "side": side, "result": "WIN", "venue": "kalshi"}


def _split(rows, canonical=(), proposal=None):
    return partition(rows, set(canonical), proposal, repo=REPO, ledger_branch=LEDGER)


def test_1_a_canonical_parent_wager_imports_normally():
    out = _split([_settled("k1")], canonical={"k1"}, proposal=_proposal([_wager("k1")]))
    assert out.counts[CANONICAL_PARENT] == 1 and [r["source_bet_key"] for r in out.ready] == ["k1"]
    assert out.waiting == []


def test_2_a_parent_on_the_valid_router_proposal_waits_and_is_not_refused():
    out = _split([_settled("k1"), _settled("k2")], proposal=_proposal([_wager("k1"), _wager("k2")]))
    assert out.counts == {CANONICAL_PARENT: 0, WAITING_FOR_PARENT_WAGER: 2, NO_VALID_PARENT: 0}
    assert out.ready == [] and out.waiting_keys() == ["k1", "k2"]
    assert out.proposal_problem is None


@pytest.mark.parametrize("proposal, reason", [
    (None, "no_proposal_branch"),
    (Proposal(branch=BRANCH, fetched_sha=None, pull=None), "no_proposal_branch"),
    (Proposal(branch=BRANCH, fetched_sha=SHA, pull=None, records=(_wager("k1"),)), "no_open_pull_request"),
    (Proposal(branch=BRANCH, fetched_sha=SHA, pull=None, records=(_wager("k1"),), unverifiable="URLError"),
     "pull_request_unverifiable"),
])
def test_3_a_missing_or_unreadable_proposal_is_refused_through_the_importer(proposal, reason):
    out = _split([_settled("k1")], proposal=proposal)
    assert out.counts[NO_VALID_PARENT] == 1 and out.reasons == {reason: 1}
    assert [r["source_bet_key"] for r in out.ready] == ["k1"], "it goes to the importer, which refuses it"
    assert out.waiting == []


@pytest.mark.parametrize("over, reason", [
    ({"state": "closed"}, "pull_request_not_open"),
    ({"draft": True}, "pull_request_is_draft"),
    ({"base": {"ref": "main"}}, "pull_request_targets_another_branch"),
    ({"head": {"ref": "someone-else", "sha": SHA, "repo": {"full_name": REPO}}},
     "pull_request_head_is_not_the_router_branch"),
    ({"head": {"ref": BRANCH, "sha": SHA, "repo": {"full_name": "fork/NHL-edge-finder"}}},
     "pull_request_from_another_repository"),
    ({"head": {"ref": BRANCH, "sha": "f" * 40, "repo": {"full_name": REPO}}},
     "proposal_moved_since_it_was_read"),
])
def test_3_an_invalid_proposal_is_not_a_parent(over, reason):
    out = _split([_settled("k1")], proposal=_proposal([_wager("k1")], **over))
    assert out.reasons == {reason: 1} and out.waiting == []


def test_4_a_parent_the_wager_importer_refused_is_not_on_the_proposal_and_fails_closed():
    """A REFUSED wager row is never written, so the proposal does not hold it."""
    out = _split([_settled("refused-parent")], proposal=_proposal([_wager("k-other")]))
    assert out.reasons == {"parent_not_on_proposal": 1} and out.waiting == []


@pytest.mark.parametrize("records, reason", [
    ([_wager("k1"), _wager("k1")], "parent_ambiguous_on_proposal"),
    ([_wager("k1", ticker="KXNHLGAME-26OCT02OTHER-OTH")], "parent_identity_mismatch"),
    ([_wager("k1", side="NO")], "parent_identity_mismatch"),
])
def test_5_ambiguous_or_mismatched_identity_fails_closed(records, reason):
    out = _split([_settled("k1")], proposal=_proposal(records))
    assert out.reasons == {reason: 1} and out.waiting == []
    no_key = _split([{"market_ticker": "x", "side": "YES"}], proposal=_proposal(records))
    assert no_key.reasons == {"no_source_key": 1}


def test_6_7_8_waiting_is_re_offered_then_imports_once_the_parent_is_canonical():
    rows = [_settled("k1")]
    proposal = _proposal([_wager("k1")])
    assert _split(rows, proposal=proposal).waiting_keys() == ["k1"]
    assert _split(rows, proposal=proposal).waiting_keys() == ["k1"], "re-offered unchanged on the next run"
    merged = _split(rows, canonical={"k1"}, proposal=None)
    assert merged.counts[CANONICAL_PARENT] == 1 and merged.waiting == []
    # A row is in exactly one state, so it can be handed to the importer only once per run.
    assert len(merged.ready) == 1


def test_the_partition_summary_is_counts_and_reason_codes_only():
    out = _split([_settled("kalshi:v1:" + "a" * 64)], proposal=_proposal([_wager("kalshi:v1:" + "a" * 64)]))
    text = json.dumps(out.summary())
    for token in ("kalshi:v1", "KXNHL", *SENSITIVE_TOKENS):
        assert token not in text


# ---------------------------------------------------------------------------------------- health (9-10)

def _line(prefix, payload):
    return f"job\tstep\t2026-10-08T02:00:00.000Z {prefix}{json.dumps(payload)}"


#: Destinations still in their OBSERVATION period (auto_merge off) stand in for the 2026-10-08 NHL/SOCCER shape:
#: NHL and SOCCER themselves graduated the same day, once their first real batches had been read and merged.
OBS_A, OBS_B = "NBA", "TENNIS"


def _deliver_log(health="delivered", blocked=0):
    status = {"health": health, "import_batch_id": "kalshi-router-v1",
              "payload_rows": {"CFB": 119, "MLB": 113, "NFL": 91, OBS_A: 19, OBS_B: 9},
              "production": {"eligible": 351, "blocked_orders": blocked, "deferred_orders": 0}}
    lines = [f"job\tstep\t2026-10-08T02:00:00.000Z HEALTH={health}", _line("ROUTER_STATUS_JSON=", status)]
    for sport, rows, on_ledger in (("CFB", 119, 119), ("MLB", 113, 113), ("NFL", 91, 91), (OBS_A, 19, 0),
                                   (OBS_B, 9, 0)):
        lines.append(_line("ROUTER_DELIVERY_JSON=", {"sport": sport, "verdict": "PASS", "dry_run": False, "note": ""}))
        lines.append(_line("ROUTER_RECONCILE_JSON=", {
            "sport": sport, "kind": "wagers", "payload_rows": rows, "on_ledger": on_ledger,
            "proposed_not_merged": rows - on_ledger, "waiting_for_parent_wager": 0, "refused": 0,
            "refused_awaiting_parent": None, "unaccounted": 0}))
    return "\n".join(lines)


def _settle_log(nhl_error=False):
    lines = ["job\tstep\t2026-10-08T01:45:00.000Z settlement payloads written (rows per destination; rows are NOT printed):"]
    for sport, rows in (("CFB", 117), ("NFL", 91), (OBS_A, 19), (OBS_B, 9)):
        lines.append(f"job\tstep\t2026-10-08T01:45:00.000Z   {sport}: {rows}")
    lines.append("job\tstep\t2026-10-08T01:45:00.000Z ")
    for sport, rows, waiting in (("CFB", 117, 0), ("NFL", 91, 0), (OBS_A, 19, 19), (OBS_B, 9, 9)):
        lines.append(_line("ROUTER_RECONCILE_JSON=", {
            "sport": sport, "kind": "settlements", "payload_rows": rows, "on_ledger": rows - waiting,
            "proposed_not_merged": 0, "waiting_for_parent_wager": waiting, "refused": 0,
            "refused_awaiting_parent": 0, "unaccounted": 0}))
    if nhl_error:
        lines.append(f"job\tstep\t2026-10-08T01:48:00.000Z ##[error]{OBS_A}: the destination importer refused at least one settlement.")
    return "\n".join(lines)


RUN = {"databaseId": 37714804665, "status": "completed", "conclusion": "success",
       "createdAt": "2026-10-08T01:49:11Z", "updatedAt": "2026-10-08T02:03:01Z", "url": "https://example.invalid/d"}
SRUN = {"databaseId": 37713992507, "status": "completed", "conclusion": "success",
        "createdAt": "2026-10-08T01:39:12Z", "updatedAt": "2026-10-08T01:48:27Z", "url": "https://example.invalid/s"}
NOW = "2026-10-08T02:10:00Z"


def _build(deliver_log, settle_log, settle_run=SRUN, deliver_run=RUN):
    health, recent = prh.build_documents(deliver_run=deliver_run, deliver_parsed=prh.parse_log(deliver_log),
                                         settle_run=settle_run, settle_parsed=prh.parse_log(settle_log),
                                         previous_recent=[], now=NOW, commit_sha="abc")
    validate(health, "router_health")
    validate(recent, "recent_deliveries")
    return health, recent


def test_9_waiting_parent_settlements_and_awaiting_merge_wagers_leave_the_router_healthy():
    health, recent = _build(_deliver_log(), _settle_log())
    assert health["overall_status"] == "HEALTHY"
    assert health["errors"] == []
    for sport, n in ((OBS_A, 19), (OBS_B, 9)):
        route = health["by_sport"][sport]
        assert route["status"] == "AWAITING_MANUAL_MERGE"
        assert route["delivered"] == n and route["proposed_not_merged"] == n and route["on_ledger"] == 0
        assert route["settlement"]["status"] == "WAITING_FOR_PARENT_WAGER"
        assert route["settlement"]["waiting_for_parent_wager"] == n and route["settlement"]["refused"] == 0
        assert any(w.startswith(f"{sport}: {n} settlement(s) WAITING_FOR_PARENT_WAGER") for w in health["warnings"])
    assert health["awaiting_manual_merge"] == 28 and health["waiting_for_parent_wager"] == 28
    assert health["by_sport"]["NFL"]["status"] == "DELIVERED"
    assert health["by_sport"]["CFB"]["settlement"]["status"] == "SETTLED"
    assert health["delivered"] == 351 and health["blocked"] == 0
    items = {(i["sport"], i["status"]) for i in recent["items"]}
    assert (OBS_A, "WAITING_FOR_PARENT_WAGER") in items and (OBS_A, "AWAITING_MANUAL_MERGE") in items
    nhl_settle = next(i for i in recent["items"] if i["sport"] == OBS_A and i["status"] == "WAITING_FOR_PARENT_WAGER")
    assert nhl_settle["retry_status"] == "WILL_RETRY"


def test_10_a_genuinely_blocked_wager_keeps_the_router_degraded_even_when_settlements_only_wait():
    health, _ = _build(_deliver_log(health="blocked", blocked=1), _settle_log())
    assert health["overall_status"] == "DEGRADED" and health["router_health_state"] == "blocked"
    assert health["blocked"] == 1
    assert any("BLOCKED" in w for w in health["warnings"])


def test_10_a_genuine_settlement_failure_still_degrades():
    health, _ = _build(_deliver_log(), _settle_log(nhl_error=True), settle_run=dict(SRUN, conclusion="failure"))
    assert health["overall_status"] == "DEGRADED"
    assert health["by_sport"][OBS_A]["settlement"]["status"] == "FAILED"
    assert any(e.startswith(f"settle {OBS_A}:") for e in health["errors"])


def test_a_graduated_destination_with_an_in_flight_proposal_is_delivered_not_awaiting_a_person():
    """NHL graduated 2026-10-08 (auto_merge on): a proposal not yet merged is the gate waiting on its own,
    which is DELIVERED -- never 'awaiting a person's merge'."""
    log = _deliver_log().replace(f'"sport": "{OBS_A}"', '"sport": "NHL"').replace(f'"{OBS_A}": 19', '"NHL": 19')
    health, _ = _build(log, _settle_log())
    assert health["by_sport"]["NHL"]["status"] == "DELIVERED"
    assert health["by_sport"]["NHL"]["proposed_not_merged"] == 19
    assert not any(w.startswith("NHL: 19 wager(s) delivered to the open proposal") for w in health["warnings"])


def test_a_health_document_published_before_1_3_0_still_validates():
    """Contract 1.3.0 is additive: strip every 1.3.0 field and value and the document still validates."""
    health, _ = _build(_deliver_log(), _settle_log())
    for key in ("awaiting_manual_merge", "waiting_for_parent_wager"):
        health.pop(key)
    for route in health["by_sport"].values():
        for key in ("on_ledger", "proposed_not_merged", "settlement"):
            route.pop(key)
        if route["status"] == "AWAITING_MANUAL_MERGE":
            route["status"] = "DELIVERED"
    validate(health, "router_health")


def test_blocked_and_deferred_reach_the_status_json():
    diagnostics = ProductionDiagnostics(refused_sport_unresolved=29, refused_game_date_not_established=1,
                                        refused_order_not_final=2)
    data = diagnostics.as_dict()
    assert data["blocked_orders"] == 30 and data["deferred_orders"] == 2


# ------------------------------------------------------------------------------- combos (11-12)

LEG_DATE = "26SEP27"


def _leg(ticker, event, series, competition):
    return {
        ticker: make_market(ticker, event),
        event: make_event(event, series),
        f"{event}/metadata": make_event_metadata(competition, "Game"),
        series: make_series(series, category="Sports", tags=["Football"]),
    }


NFL_LEGS = {
    "KXNFLGAME-26SEP27SYNAAWYHOM-HOM": ("KXNFLGAME-26SEP27SYNAAWYHOM", "KXNFLGAME", "Pro Football"),
    "KXNFLSPREAD-26SEP27SYNBAWYHOM-HOM3": ("KXNFLSPREAD-26SEP27SYNBAWYHOM", "KXNFLSPREAD", "Pro Football"),
}
CFB_LEG = {"KXNCAAFGAME-26SEP27SYNCAWYHOM-HOM": ("KXNCAAFGAME-26SEP27SYNCAWYHOM", "KXNCAAFGAME", "College Football")}
COMBO_SERIES = "KXMVENFLMULTIGAMEEXTENDED"
COMBO_EVENT = f"{COMBO_SERIES}-S2026SYNTH02"
COMBO = f"{COMBO_EVENT}-SYNTHCOMBO"


def _contexts(legs: dict, combo_competition=None, extra_legs=()):
    metadata = {}
    for ticker, (event, series, competition) in legs.items():
        metadata.update(_leg(ticker, event, series, competition))
    stated = [{"event_ticker": event, "market_ticker": ticker, "side": "yes"}
              for ticker, (event, _s, _c) in legs.items()] + list(extra_legs)
    metadata[COMBO] = make_market(COMBO, COMBO_EVENT, mve_collection_ticker=f"{COMBO_SERIES}-R",
                                  mve_selected_legs=stated)
    metadata[COMBO_EVENT] = make_event(COMBO_EVENT, COMBO_SERIES, title="Synthetic combo")
    metadata[f"{COMBO_EVENT}/metadata"] = make_event_metadata(combo_competition)
    metadata[COMBO_SERIES] = make_series(COMBO_SERIES, category="Exotics", title="Multi Game Extended")
    return metadata


def _ctx(metadata, ticker):
    market = metadata.get(ticker)
    event_ticker = (market or {}).get("event_ticker")
    event = metadata.get(event_ticker)
    series = metadata.get((event or {}).get("series_ticker"))
    return MarketContext(market_ticker=ticker, market=market, event=event, series=series,
                         event_metadata=metadata.get(f"{event_ticker}/metadata"))


def _classify(metadata):
    return classify_with_legs(_ctx(metadata, COMBO), lambda t: _ctx(metadata, t))


def test_11_a_combo_whose_every_leg_is_nfl_is_nfl_by_its_legs():
    metadata = _contexts(NFL_LEGS)
    single = classify_market(_ctx(metadata, COMBO))
    assert single.sport is Sport.UNRESOLVED and single.unresolved_reason is UnresolvedReason.COMPETITION_ABSENT
    verdict = _classify(metadata)
    assert verdict.sport is Sport.NFL and verdict.resolved_by is EvidenceLevel.COMBO_LEGS
    assert verdict.unresolved_reason is None
    date, source = resolve_combo_game_date(_ctx(metadata, COMBO), lambda t: _ctx(metadata, t))
    assert (date, source) == ("2026-09-27", GameDateSource.COMBO_LEGS)


def test_11_a_combo_whose_every_leg_is_cfb_is_cfb():
    assert _classify(_contexts(CFB_LEG)).sport is Sport.CFB


def test_11_a_single_market_is_classified_exactly_as_before():
    metadata = _contexts(NFL_LEGS)
    ticker = next(iter(NFL_LEGS))
    assert classify_with_legs(_ctx(metadata, ticker), lambda t: _ctx(metadata, t)) == classify_market(_ctx(metadata, ticker))


def test_12_legs_spanning_two_sports_stay_unresolved():
    verdict = _classify(_contexts({**NFL_LEGS, **CFB_LEG}))
    assert verdict.sport is Sport.UNRESOLVED
    assert verdict.unresolved_reason is UnresolvedReason.COMBO_LEGS_SPAN_SPORTS


def test_12_a_leg_with_an_unknown_competition_fails_the_whole_combo_closed():
    legs = dict(NFL_LEGS)
    legs["KXSYNTHGAME-26SEP27SYNDAWYHOM-HOM"] = ("KXSYNTHGAME-26SEP27SYNDAWYHOM", "KXSYNTHGAME", "Synthetic League")
    verdict = _classify(_contexts(legs))
    assert verdict.sport is Sport.UNRESOLVED and verdict.unresolved_reason is UnresolvedReason.COMBO_LEG_UNRESOLVED


def test_12_a_leg_without_a_ticker_or_no_legs_at_all_fails_closed():
    verdict = _classify(_contexts(NFL_LEGS, extra_legs=[{"event_ticker": "X"}]))
    assert verdict.unresolved_reason is UnresolvedReason.COMBO_LEGS_UNAVAILABLE
    metadata = _contexts(NFL_LEGS)
    metadata[COMBO]["mve_selected_legs"] = []
    assert _classify(metadata).unresolved_reason is UnresolvedReason.COMBO_LEGS_UNAVAILABLE


def test_12_an_unknown_competition_on_the_combo_itself_is_terminal_and_the_legs_cannot_rescue_it():
    verdict = _classify(_contexts(NFL_LEGS, combo_competition="Synthetic League"))
    assert verdict.sport is Sport.UNRESOLVED and verdict.unresolved_reason is UnresolvedReason.COMPETITION_UNKNOWN


def test_12_combo_evidence_that_disagrees_with_its_legs_is_a_conflict():
    verdict = _classify(_contexts(NFL_LEGS, combo_competition="College Football"))
    assert verdict.unresolved_reason is UnresolvedReason.EVIDENCE_CONFLICT


def test_12_a_combo_whose_legs_span_two_dates_has_no_game_date():
    legs = dict(NFL_LEGS)
    legs["KXNFLGAME-26SEP28SYNEAWYHOM-HOM"] = ("KXNFLGAME-26SEP28SYNEAWYHOM", "KXNFLGAME", "Pro Football")
    metadata = _contexts(legs)
    assert _classify(metadata).sport is Sport.NFL
    assert resolve_combo_game_date(_ctx(metadata, COMBO), lambda t: _ctx(metadata, t)) == (None, GameDateSource.NONE)


def test_only_destinations_that_can_settle_a_combo_record_one():
    assert combo_destination_names() == frozenset({"CFB", "NFL", "NHL", "NBA", "SOCCER", "TENNIS"})
    assert profile_for("MLB").records_combo_wagers is False
    for profile in PROFILES.values():
        if profile.records_combo_wagers:
            assert profile.settlement_importer is not None
    assert ProductionRefusal.COMBO_NOT_RECORDABLE in NEEDS_ATTENTION_REFUSALS


def _fills():
    return [[make_fill(1, ticker=COMBO, created_time="2026-09-27T16:10:00Z", fee_cost="0.0200")]]


def _metadata_for_cli(legs):
    metadata = build_metadata()
    metadata.update(_contexts(legs))
    return metadata


def test_11_end_to_end_an_nfl_combo_is_delivered_to_nfl_with_its_legs_date(monkeypatch, local_env, tmp_path):
    install_fake_api(monkeypatch, _fills(), metadata=_metadata_for_cli(NFL_LEGS))
    code, out, err = run(["deliver", "--out-dir", str(tmp_path / "payloads"), "--allow-stabilization"])
    assert code == 0, err
    assert "sport unresolved: 0" in out and "competition absent: 0" in out
    assert "eligible orders on COMBO markets (sport proven by every leg): 1" in out
    payload = json.loads((tmp_path / "payloads" / "NFL.json").read_text())
    assert [r["game_date"] for r in payload["rows"]] == ["2026-09-27"]
    status = json.loads(next(l for l in out.splitlines() if l.startswith("ROUTER_STATUS_JSON="))
                        .split("=", 1)[1])
    assert status["production"]["eligible_combo_orders"] == 1 and status["production"]["blocked_orders"] == 0
    for token in (COMBO, COMBO_EVENT, *NFL_LEGS, *SENSITIVE_TOKENS):
        assert token not in out


def test_12_end_to_end_a_mixed_combo_stays_blocked(monkeypatch, local_env, tmp_path):
    install_fake_api(monkeypatch, _fills(), metadata=_metadata_for_cli({**NFL_LEGS, **CFB_LEG}))
    code, out, err = run(["deliver", "--out-dir", str(tmp_path / "payloads"), "--allow-stabilization"])
    assert code == 0, err
    assert "sport unresolved: 1" in out and "combo whose legs span more than one sport: 1" in out
    assert "HEALTH=blocked" in out
    assert not (tmp_path / "payloads" / "NFL.json").exists()


def test_end_to_end_a_combo_spanning_two_dates_is_refused_for_its_date_and_profiled(monkeypatch, local_env, tmp_path):
    legs = dict(NFL_LEGS)
    legs["KXNFLGAME-26SEP28SYNEAWYHOM-HOM"] = ("KXNFLGAME-26SEP28SYNEAWYHOM", "KXNFLGAME", "Pro Football")
    install_fake_api(monkeypatch, _fills(), metadata=_metadata_for_cli(legs))
    code, out, err = run(["deliver", "--out-dir", str(tmp_path / "payloads"), "--allow-stabilization"])
    assert code == 0, err
    assert "game date not established: 1" in out and "HEALTH=blocked" in out
    assert "#1: NFL; orders 1 on 2026-09-27; combo, 3 legs stated, 3 dated, 2 distinct leg date(s)" in out
    for token in (COMBO, COMBO_EVENT, *legs, *SENSITIVE_TOKENS):
        assert token not in out
