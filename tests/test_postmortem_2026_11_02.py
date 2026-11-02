"""Regression tests for the 2026-11-02 postmortem (issue #116): combo decoding, immutable reconciliation, the NBA
settlement-parent state with real PR #48 row shapes, workflow clone resilience, and the status report."""

from __future__ import annotations

from pathlib import Path

import pytest

from kalshi_router.classify import MarketContext
from kalshi_router.combo_decode import ComboStatus, decode_combo
from kalshi_router.contract_reconciliation import build_reconciliation, economics_check, summarize
from kalshi_router.postmortem_report import Item, render_dashboard, to_report, validate
from kalshi_router.settlement_parents import (
    CANONICAL_PARENT, NO_VALID_PARENT, WAITING_FOR_PARENT_WAGER, Proposal, partition)
from kalshi_router.sports import Sport

from .synthetic import make_event, make_event_metadata, make_market, make_series

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------------------------------ combo decoding
def _leg_meta(ticker, event, series, competition, tags):
    return {ticker: make_market(ticker, event), event: make_event(event, series),
            f"{event}/metadata": make_event_metadata(competition, "Game"),
            series: make_series(series, category="Sports", tags=tags)}


NFL = ("KXNFLGAME-26SEP27SYNAAWYHOM-HOM", "KXNFLGAME-26SEP27SYNAAWYHOM", "KXNFLGAME", "Pro Football", ["Football"])
CFB = ("KXNCAAFGAME-26SEP27SYNCAWYHOM-HOM", "KXNCAAFGAME-26SEP27SYNCAWYHOM", "KXNCAAFGAME", "College Football",
       ["Football"])
COMBO_SERIES = "KXMVECROSSCATEGORY"
COMBO_EVENT = f"{COMBO_SERIES}-S2026SYNTH01"
COMBO = f"{COMBO_EVENT}-SYNTHCOMBO"


def _combo(legs, stated=None):
    meta = {}
    for ticker, event, series, comp, tags in legs:
        meta.update(_leg_meta(ticker, event, series, comp, tags))
    stated = stated if stated is not None else [
        {"event_ticker": e, "market_ticker": t, "side": "yes"} for t, e, *_ in legs]
    meta[COMBO] = make_market(COMBO, COMBO_EVENT, mve_collection_ticker=f"{COMBO_SERIES}-R",
                              mve_selected_legs=stated)
    meta[COMBO_EVENT] = make_event(COMBO_EVENT, COMBO_SERIES, title="Synthetic combo")
    meta[f"{COMBO_EVENT}/metadata"] = make_event_metadata(None)
    meta[COMBO_SERIES] = make_series(COMBO_SERIES, category="Exotics", title="Cross category")

    def ctx(t):
        m = meta.get(t)
        ev = meta.get((m or {}).get("event_ticker"))
        se = meta.get((ev or {}).get("series_ticker"))
        return MarketContext(market_ticker=t, market=m, event=ev, series=se,
                             event_metadata=meta.get(f"{(m or {}).get('event_ticker')}/metadata"))
    return ctx, meta


def test_cross_category_combo_is_decoded_into_legs_and_never_defaulted_to_one_sport():
    ctx, _ = _combo([NFL, CFB])
    d = decode_combo(ctx(COMBO), ctx)
    assert d.status is ComboStatus.DECODED_CROSS_CATEGORY
    assert d.sport is None and d.sports == ("CFB", "NFL")
    assert {l.sport for l in d.legs} == {Sport.NFL, Sport.CFB}
    assert all(l.required_side == "yes" for l in d.legs)


def test_single_sport_combo_decodes_to_that_sport():
    ctx, _ = _combo([NFL])
    d = decode_combo(ctx(COMBO), ctx)
    assert d.status is ComboStatus.DECODED_SINGLE_SPORT and d.sport is Sport.NFL


def test_combo_without_stated_legs_is_undecoded_not_guessed():
    ctx, _ = _combo([NFL], stated=[])
    d = decode_combo(ctx(COMBO), ctx)
    assert d.status is ComboStatus.UNDECODED_COMBO and "MISSING_COMBO_LEGS" in d.flags and not d.decoded


def test_combo_with_an_unresolvable_leg_is_undecoded_but_lists_every_leg():
    ctx, _ = _combo([NFL], stated=[
        {"event_ticker": NFL[1], "market_ticker": NFL[0], "side": "yes"},
        {"event_ticker": "KXGHOST-1", "market_ticker": "KXGHOST-1-A", "side": "no"}])
    d = decode_combo(ctx(COMBO), ctx)
    assert d.status is ComboStatus.UNDECODED_COMBO and "LEG_UNRESOLVED" in d.flags
    assert len(d.legs) == 2 and d.legs[1].sport is Sport.UNRESOLVED and d.legs[1].required_side == "no"


def test_leg_lookup_exception_is_missing_evidence_not_a_crash():
    ctx, _ = _combo([NFL])

    def boom(_t):
        raise RuntimeError("network")
    d = decode_combo(ctx(COMBO), boom)
    assert d.status is ComboStatus.UNDECODED_COMBO and "LEG_LOOKUP_FAILED" in d.flags


def test_a_plain_market_is_not_a_combo():
    ctx, _ = _combo([NFL])
    assert decode_combo(ctx(NFL[0]), ctx).status is ComboStatus.NOT_A_COMBO


# ------------------------------------------------------------------------------------ reconciliation record
CFB_WAGER = {  # the CFB app-export shape; values synthetic
    "average_price": 0.53, "contracts": 54.8, "stake": 29.9996, "fees": 0.9556, "kalshi_fill_ids": [],
    "kalshi_order_id": None, "kalshi_ticker": "KXNCAAFTOTAL-26SEP11SYNAHOM-55", "selection": "NO",
    "recommendation_id": None, "model_run_id": None, "placed_at": "2026-09-11T23:22:42Z",
    "linkage": {"model_inputs_as_of": None, "recommendation_generated_at": None},
    "extensions": {"fees_are_estimated": False}, "source_bet_key": "kalshi:v1:" + "a" * 64}
SETTLED = {"result": "WON", "gross_payout": 54.8, "net_pnl": 24.8004, "extensions": {"economics_version": "router-settlement-economics.v2"}}


def test_missing_evidence_is_flagged_never_inferred():
    rec = build_reconciliation(CFB_WAGER, SETTLED)
    for flag in ("MISSING_RECOMMENDATION_ID", "MISSING_MODEL_RUN_ID", "MISSING_MODEL_INPUTS_AS_OF",
                 "MISSING_ORDER_ID", "MISSING_FILL_IDS", "MISSING_CLOSE_QUOTE"):
        assert flag in rec.missing
    assert rec.recommendation_id is None and rec.model_run_id is None and rec.clv is None


def test_fee_is_inside_stake_net_is_gross_minus_stake_and_not_double_counted():
    rec = build_reconciliation(CFB_WAGER, SETTLED)
    assert rec.economics_check == "OK" and rec.net_pnl == pytest.approx(54.8 - 29.9996)
    # a v1-style net that subtracts the fee again is FLAGGED, not repaired
    v1 = dict(SETTLED, net_pnl=23.8448)
    bad = build_reconciliation(CFB_WAGER, v1)
    assert bad.economics_check == "NET_DISAGREES_WITH_GROSS_MINUS_STAKE" and bad.net_pnl == 23.8448
    assert economics_check(None, 1.0, 1.0) == "UNESTABLISHED"


def test_amendment_supersedes_filed_figures_without_touching_the_input():
    v1 = dict(SETTLED, net_pnl=23.8448)
    amend = [{"amendment_id": "amd-1", "economics_version": "router-settlement-economics.v2",
              "gross_return": 54.8, "net_profit_loss": 24.8004}]
    rec = build_reconciliation(CFB_WAGER, v1, amendments=amend)
    assert rec.net_pnl == 24.8004 and rec.amendment_ids == ("amd-1",) and rec.economics_check == "OK"
    assert v1["net_pnl"] == 23.8448


def test_clv_is_close_minus_fill_for_the_side_bought_and_hash_tracks_content():
    a = build_reconciliation(CFB_WAGER, SETTLED, close_price_for_side=0.58)
    assert a.clv == pytest.approx(0.05) and "MISSING_CLOSE_QUOTE" not in a.missing
    assert a.record_hash == build_reconciliation(CFB_WAGER, SETTLED, close_price_for_side=0.58).record_hash
    assert a.record_hash != build_reconciliation(CFB_WAGER, SETTLED, close_price_for_side=0.57).record_hash
    with pytest.raises(Exception):
        a.clv = 1.0  # frozen


def test_undecoded_combo_is_reported_separately_and_summary_counts_only():
    ctx, _ = _combo([NFL], stated=[])
    d = decode_combo(ctx(COMBO), ctx)
    rec = build_reconciliation(CFB_WAGER, None, combo=d)
    assert "UNDECODED_COMBO" in rec.missing and "MISSING_SETTLEMENT" in rec.missing
    s = summarize([rec])
    assert s["records"] == 1 and "KXNCAAF" not in str(s)


# --------------------------------------------- NBA PR #48 rows: pending wagers, settlements wait, never fail
REPO, LEDGER, BRANCH = "chmoses98/nba-edge-finder", "accounting-data", "kalshi-router/NBA"
# Shapes copied from the public diff of nba-edge-finder PR #48 (combo ticker, fractional contracts, a price that is
# not a cent multiple).
PR48 = [
    {"source_bet_key": "kalshi:v1:" + "1" * 64, "market_ticker": "KXNBAGAME-26OCT21MINMIA-MIN", "side": "YES",
     "contracts": 153.86, "execution_price": 0.47},
    {"source_bet_key": "kalshi:v1:" + "2" * 64, "market_ticker": "KXMVECROSSCATEGORY-S2026763BCX3VSRNC63IQ-JDSF6UCZP",
     "side": "YES", "contracts": 169.0, "execution_price": 0.196},
    {"source_bet_key": "kalshi:v1:" + "3" * 64, "market_ticker": "KXNBAPTS-26OCT30HOUDAL-DALCFLAGG32-25", "side": "NO",
     "contracts": 70.54, "execution_price": 0.5495973915508932},
]


def _pull(**over):
    pull = {"state": "open", "draft": False, "head": {"ref": BRANCH, "sha": "abc", "repo": {"full_name": REPO}},
            "base": {"ref": LEDGER}}
    pull.update(over)
    return pull


def _settle(row):
    return {"source_bet_key": row["source_bet_key"], "market_ticker": row["market_ticker"], "side": row["side"]}


def _part(rows, proposal, canonical=()):
    return partition(rows, set(canonical), proposal, repo=REPO, ledger_branch=LEDGER)


def test_pr48_shaped_wagers_make_their_settlements_wait_rather_than_fail():
    prop = Proposal(BRANCH, "abc", _pull(), tuple(PR48))
    out = _part([_settle(r) for r in PR48], prop)
    assert out.counts[WAITING_FOR_PARENT_WAGER] == 3 and out.counts[NO_VALID_PARENT] == 0 and not out.ready


@pytest.mark.parametrize("pull,problem", [
    (_pull(draft=True), "pull_request_is_draft"),
    (_pull(base={"ref": "main"}), "pull_request_targets_another_branch"),
    (_pull(head={"ref": BRANCH, "sha": "moved", "repo": {"full_name": REPO}}), "proposal_moved_since_it_was_read"),
])
def test_an_invalid_proposal_never_defers_a_settlement(pull, problem):
    out = _part([_settle(PR48[0])], Proposal(BRANCH, "abc", pull, tuple(PR48)))
    assert out.counts[NO_VALID_PARENT] == 1 and out.reasons == {problem: 1}


def test_side_flip_between_wager_and_settlement_is_refused_not_deferred():
    bad = dict(_settle(PR48[0]), side="NO")
    out = _part([bad], Proposal(BRANCH, "abc", _pull(), tuple(PR48)))
    assert out.reasons == {"parent_identity_mismatch": 1}


def test_a_merged_wager_is_canonical_and_goes_to_the_importer_once_merged():
    prop = Proposal(BRANCH, "abc", _pull(), tuple(PR48))
    out = _part([_settle(PR48[0])], prop, canonical={PR48[0]["source_bet_key"]})
    assert out.counts[CANONICAL_PARENT] == 1 and len(out.ready) == 1 and not out.waiting


# ------------------------------------------------ workflows: one destination's flaky clone must not skip the rest
@pytest.mark.parametrize("name", ["deliver-wagers.yml", "settle-wagers.yml"])
def test_destination_clones_are_retried_and_scoped_to_their_own_sport(name):
    text = (ROOT / ".github/workflows" / name).read_text()
    assert 'source "${GITHUB_WORKSPACE}/scripts/clone_retry.sh"' in text  # behaviour: tests/test_clone_retry.py
    # no bare clone inside the per-destination loop: every clone goes through the retrying helper
    loop = text[text.index('for payload in "${payloads[@]}"'):]
    assert "git clone" not in loop, "a bare `git clone` under set -e would abort every later destination"
    assert loop.count("if ! clone_retry") == 2 and loop.count("could not clone") == 2


# ------------------------------------------------------------------------------------------ status report
def test_report_refuses_done_without_evidence_and_unresolved_without_blocker():
    bad = [Item(1, "kalshi-bet-router", "DONE"), Item(6, "nba-edge-finder", "BLOCKED"),
           Item(9, "x", "WHATEVER")]
    assert len(validate(bad)) == 4
    good = [Item(1, "kalshi-bet-router", "DONE", ["tests/test_postmortem_2026_11_02.py"]),
            Item(6, "nba-edge-finder", "NEEDS_OWNER_DECISION", blocker="merge of PR #48 is the owner's call")]
    rep = to_report(good)
    assert rep["valid"] and rep["counts"]["DONE"] == 1
    assert "NEEDS_OWNER_DECISION" in render_dashboard(rep)
