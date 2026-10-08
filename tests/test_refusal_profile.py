"""The refused-for-sport orders must be describable, not just countable.

REAL FAILURE SHAPE (deliver run 36510046293, 2026-09-29):

    refused: 1769
      sport unresolved: 14
    why those markets were unresolved (distinct MARKETS ...):
      competition absent: 9
    BLOCKED (cannot be recorded, and waiting will not help): 14

and nothing else -- so a week of BLOCKED could not be told apart from a week of
correct refusals. Every MLB series Kalshi lists carries tags='Baseball', which
yields AMBIGUOUS_FAMILY, not silence; the shape that yields silence is a
multivariate COMBO market whose series names no sport and whose legs do.
"""

from __future__ import annotations

from kalshi_router.refusals import RefusalProfile, RefusedMarketProfile, describe_series

from .synthetic import (
    SENSITIVE_TOKENS,
    make_event,
    make_event_metadata,
    make_fill,
    make_market,
    make_series,
)
from .test_audit import build_metadata
from .test_cli import install_fake_api, local_env, run  # noqa: F401  (fixture)

COMBO_SERIES = "KXMVESPORTSMULTIGAMEEXTENDED"
COMBO_EVENT = f"{COMBO_SERIES}-S2026SYNTH01"
COMBO_MARKET = f"{COMBO_EVENT}-SYNTHCOMBO"
LEG_A = "KXMLBGAME-26SEP221910SYNAWYHOM-HOM"
LEG_B = "KXMLBTEAMTOTAL-26SEP222010SYNBAWSYNBHM-SYNBHM4"


def _combo_metadata() -> dict:
    metadata = build_metadata()
    for leg, event, series in (
        (LEG_A, "KXMLBGAME-26SEP221910SYNAWYHOM", "KXMLBGAME"),
        (LEG_B, "KXMLBTEAMTOTAL-26SEP222010SYNBAWSYNBHM", "KXMLBTEAMTOTAL"),
    ):
        metadata[leg] = make_market(leg, event)
        metadata[event] = make_event(event, series)
        metadata[f"{event}/metadata"] = make_event_metadata("Pro Baseball", "Game")
        metadata.setdefault(series, make_series(series, category="Sports", tags=["Baseball"]))
    metadata[COMBO_MARKET] = make_market(
        COMBO_MARKET,
        COMBO_EVENT,
        mve_collection_ticker="KXMVESPORTSMULTIGAMEEXTENDED-R",
        mve_selected_legs=[
            {"event_ticker": "KXMLBGAME-26SEP221910SYNAWYHOM", "market_ticker": LEG_A, "side": "yes"},
            {"event_ticker": "KXMLBTEAMTOTAL-26SEP222010SYNBAWSYNBHM", "market_ticker": LEG_B,
             "side": "yes"},
        ],
    )
    metadata[COMBO_EVENT] = make_event(COMBO_EVENT, COMBO_SERIES, title="Synthetic combo")
    # The live shape: the combo's event has metadata, and no competition.
    metadata[f"{COMBO_EVENT}/metadata"] = make_event_metadata(None)
    metadata[COMBO_SERIES] = make_series(
        COMBO_SERIES, category="Sports", title="Multi Game Extended", tags=[]
    )
    return metadata


def _pages():
    return [[
        make_fill(1, ticker=COMBO_MARKET, created_time="2026-09-22T23:10:00Z", fee_cost="0.0200"),
        make_fill(2, ticker=COMBO_MARKET, created_time="2026-09-23T01:10:00Z", fee_cost="0.0200"),
    ]]


def test_deliver_profiles_a_combo_whose_legs_are_all_mlb(monkeypatch, local_env, tmp_path):
    install_fake_api(monkeypatch, _pages(), metadata=_combo_metadata())
    code, out, err = run(
        ["deliver", "--out-dir", str(tmp_path / "payloads"), "--allow-stabilization"]
    )
    assert code == 0, err
    # 2026-10-08: the legs PROVE this combo is MLB (classify_with_legs), so it is no longer "sport
    # unresolved". It is still BLOCKED -- MLB settles its own wagers and cannot grade a combo -- under the
    # reason that is actually true.
    assert "sport unresolved: 0" in out
    assert "competition absent: 0" in out
    assert "combo the destination cannot record: 2" in out
    assert "BLOCKED (cannot be recorded, and waiting will not help): 2" in out
    assert "HEALTH=blocked" in out


def test_the_profile_names_no_market_event_or_series(monkeypatch, local_env, tmp_path):
    install_fake_api(monkeypatch, _pages(), metadata=_combo_metadata())
    code, out, _ = run(
        ["deliver", "--out-dir", str(tmp_path / "payloads"), "--allow-stabilization"]
    )
    assert code == 0
    for token in (COMBO_MARKET, COMBO_EVENT, COMBO_SERIES, LEG_A, LEG_B, *SENSITIVE_TOKENS):
        assert token not in out, token


def test_a_non_sports_series_is_described_by_category_alone():
    category, title, tags = describe_series(
        {"category": "Economics", "title": "Fed decision in October", "tags": ["Fed"]}
    )
    assert (category, title, tags) == ("Economics", None, ())


def test_legs_that_disagree_are_not_called_one_sport():
    mixed = RefusedMarketProfile(verdict="UNRESOLVED", reason="competition_absent", orders=1,
                                 combo=True, legs_stated=2, leg_sports={"MLB": 1, "NFL": 1})
    missing = RefusedMarketProfile(verdict="UNRESOLVED", reason="competition_absent", orders=1,
                                   combo=True, legs_stated=3, leg_sports={"MLB": 2})
    single = RefusedMarketProfile(verdict="UNRESOLVED", reason="competition_absent", orders=1)
    assert mixed.all_legs_one_sport is None
    assert missing.all_legs_one_sport is None  # a leg the classifier never saw
    assert single.all_legs_one_sport is None
    profile = RefusalProfile(markets=[mixed, missing, single])
    assert profile.combos_by_leg_sport() == {"mixed/unresolved": 2}


def test_deliver_prints_one_coverage_line_per_destination(monkeypatch, local_env, tmp_path):
    """PROD-9 in edge-finder-api reads these; the refused MLB combo is counted
    as provably MLB rather than disappearing into 'sport unresolved'."""
    install_fake_api(monkeypatch, _pages(), metadata=_combo_metadata())
    code, out, err = run(
        ["deliver", "--out-dir", str(tmp_path / "payloads"), "--allow-stabilization"]
    )
    assert code == 0, err
    lines = [line for line in out.splitlines() if line.startswith("ROUTER_COVERAGE ")]
    assert any(line.startswith("ROUTER_COVERAGE sport=MLB ") for line in lines), out
    mlb = next(line for line in lines if " sport=MLB " in line)
    assert "refused_provably=2" in mlb
    assert "eligible=0" in mlb and "newest_game_date=none" in mlb
