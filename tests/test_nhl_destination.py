"""NHL as a routed destination: ACCOUNTING ONLY.

Classification (Pro Hockey / NHL -> NHL; college and foreign hockey never NHL; the collision gate exactly as strict),
the row builder, the destination profile and its exact merge paths, v2 settlement rows, receipt normalisation,
reconciliation, and -- when a checkout of NHL-edge-finder is available -- the destination's own importers and
validator run end to end on router-built payloads.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from kalshi_router.classify import MarketContext, UnresolvedReason, classify_market
from kalshi_router.competitions import (
    possible_sports,
    sport_from_competition,
    sport_from_taxonomy_sport,
)
from kalshi_router.destination import ROUTER_IMPORT_BATCH_ID, write_payloads, write_settlement_payloads
from kalshi_router.destinations import PROFILES, describe, profile_for, render_command, routable_sport_names
from kalshi_router.production import (
    NHL_ROW_FIELDS,
    ROW_BUILDERS,
    OrderFinality,
    ProductionWager,
    to_cfb_import_row,
    to_nhl_import_row,
)
from kalshi_router.receipts import IDEMPOTENT_RERUN_VERDICTS, normalise
from kalshi_router.series_registry import lookup_series_ticker
from kalshi_router.settlement import ECONOMICS_V2, SETTLED, WagerSettlement
from kalshi_router.sports import REPORT_ORDER, ROUTABLE_SPORTS, Sport
from kalshi_router.taxonomy import parse_filters_by_sport

from .synthetic import make_event, make_event_metadata, make_market, make_series, make_taxonomy

ROOT = Path(__file__).resolve().parents[1]

#: The live taxonomy's Hockey heading as probed on 2026-09-29 (NHL-edge-finder docs/probe), plus the historical
#: mis-filing of "Pro Baseball" under Hockey that this router observed on 2026-09-15.
LIVE_LIKE = parse_filters_by_sport(make_taxonomy({
    "Baseball": ["Japan NPB", "Korea KBO", "Pro Baseball"],
    "Football": ["CFL", "NCAA Football", "Pro Football"],
    "Hockey": ["Czech Extraliga", "Finland Liiga", "Germany DEL", "KHL", "Pro Hockey", "SHL",
               "Switzerland National League", "Pro Baseball"],
    "Basketball": ["Pro Basketball (M)"],
}))


def ctx(competition=None, series="KXNHLGAME", event="KXNHLGAME-26OCT01BOSTOR", market=None, series_extra=None,
        event_extra=None):
    market = market or f"{event}-TOR"
    return MarketContext(
        market_ticker=market,
        market=make_market(market, event),
        event=make_event(event, series, **(event_extra or {})),
        series=make_series(series, **(series_extra or {})),
        event_metadata=make_event_metadata(competition) if competition is not None else None,
    )


# ------------------------------------------------------------------------------------------------ vocabulary
def test_nhl_is_a_sport_routable_and_reported():
    assert Sport.NHL.value == "NHL" and Sport.NHL in ROUTABLE_SPORTS and Sport.NHL in REPORT_ORDER
    assert "NHL" in routable_sport_names()
    # TENNIS gained a profile on 2026-10-02 (tests/test_shared_destinations.py); OTHER never has one.
    assert "OTHER" not in routable_sport_names() and Sport.OTHER not in PROFILES


# ------------------------------------------------------------------------------------------- classification
@pytest.mark.parametrize("competition", ["Pro Hockey", "NHL", "National Hockey League", "  pro   hockey "])
def test_nhl_competitions_resolve_to_nhl(competition):
    assert sport_from_competition(competition) is Sport.NHL
    assert classify_market(ctx(competition), taxonomy=LIVE_LIKE).sport is Sport.NHL


@pytest.mark.parametrize("competition", ["College Hockey", "College Hockey (M)", "NCAA Hockey", "KHL", "SHL",
                                         "Finland Liiga", "Germany DEL", "Czech Extraliga",
                                         "Switzerland National League", "AHL", "PWHL", "Field Hockey"])
def test_college_and_foreign_hockey_are_never_nhl(competition):
    verdict = classify_market(ctx(competition, series="KXSOMEHOCKEY", event="KXSOMEHOCKEY-26OCT01AAABBB"),
                              taxonomy=LIVE_LIKE)
    assert verdict.sport is not Sport.NHL
    assert verdict.sport is Sport.OTHER


def test_an_unknown_hockey_competition_fails_closed_not_to_nhl():
    tax = parse_filters_by_sport(make_taxonomy({"Hockey": ["Pro Hockey", "Slovakia Extraliga"]}))
    verdict = classify_market(ctx("Slovakia Extraliga", series="KXSVKHOCKEY", event="KXSVKHOCKEY-26OCT01AAABBB"),
                              taxonomy=tax)
    assert verdict.sport is Sport.UNRESOLVED
    assert verdict.unresolved_reason is UnresolvedReason.COMPETITION_UNKNOWN


def test_the_hockey_heading_alone_never_names_the_nhl():
    assert sport_from_taxonomy_sport("Hockey") is None  # ambiguous family: NHL and leagues that are not ours
    assert possible_sports("hockey") == frozenset({Sport.NHL})


def test_pro_baseball_filed_under_hockey_is_still_mlb():
    """The live catalogue filed "Pro Baseball" under Hockey (2026-09-15). Hockey now holds the NHL, but the NHL is
    recognised only by its exact names, so Hockey offers no rival reading of "Pro Baseball": MLB is unaffected."""
    verdict = classify_market(ctx("Pro Baseball", series="KXMLBGAME", event="KXMLBGAME-26OCT01NYYBOS"),
                              taxonomy=LIVE_LIKE)
    assert verdict.sport is Sport.MLB


def test_the_collision_gate_is_as_strict_as_before_for_open_vocabulary_rivals():
    # "Pro Hockey" claimed by Hockey AND Baseball: Baseball holds MLB, an open-vocabulary league -> refused.
    tax = parse_filters_by_sport(make_taxonomy({"Hockey": ["Pro Hockey"], "Baseball": ["Pro Hockey"]}))
    verdict = classify_market(ctx("Pro Hockey"), taxonomy=tax)
    assert verdict.sport is Sport.UNRESOLVED and verdict.unresolved_reason is UnresolvedReason.COMPETITION_AMBIGUOUS
    # "Pro Football" claimed by Football and Hockey: Hockey cannot mean NFL/CFB and NHL cannot be "Pro Football";
    # Football affirms NFL -> NFL, exactly as a Football-only listing would.
    tax = parse_filters_by_sport(make_taxonomy({"Football": ["Pro Football"], "Hockey": ["Pro Football"]}))
    assert classify_market(ctx("Pro Football", series="KXNFLGAME", event="KXNFLGAME-26OCT01KCBUF"),
                           taxonomy=tax).sport is Sport.NFL
    # An unknown claimant still refuses.
    tax = parse_filters_by_sport(make_taxonomy({"Hockey": ["Pro Hockey"], "Kabaddi": ["Pro Hockey"]}))
    assert classify_market(ctx("Pro Hockey"), taxonomy=tax).sport is Sport.UNRESOLVED


def test_series_metadata_names_the_nhl_only_by_league_tokens():
    nhl = ctx(None, series_extra={"title": "NHL Game", "tags": ["Hockey"]})
    assert classify_market(nhl).sport is Sport.NHL
    college = ctx(None, series="KXNCAAHOCKEYGAME", event="KXNCAAHOCKEYGAME-26OCT01AAABBB",
                  series_extra={"title": "College Hockey Game", "tags": ["Hockey"]})
    assert classify_market(college).sport is Sport.OTHER
    generic = ctx(None, series="KXHOCKEYX", event="KXHOCKEYX-26OCT01AAABBB", series_extra={"title": "Hockey Game"})
    verdict = classify_market(generic)
    assert verdict.sport is Sport.UNRESOLVED and verdict.unresolved_reason is UnresolvedReason.AMBIGUOUS_FAMILY


@pytest.mark.parametrize("series", ["KXNHLGAME", "KXNHLSPREAD", "KXNHLTOTAL", "KXNHLTEAMTOTAL", "KXNHLOT"])
def test_verified_nhl_series_are_in_the_registry(series):
    entry = lookup_series_ticker(series)
    assert entry is not None and entry.sport is Sport.NHL and entry.verified


def test_the_registry_never_overrides_a_contradicting_competition():
    verdict = classify_market(ctx("College Hockey"), taxonomy=LIVE_LIKE)  # KXNHLGAME ticker, college competition
    assert verdict.sport is not Sport.NHL


# ------------------------------------------------------------------------------------------------ row builder
def production_wager(**kw) -> ProductionWager:
    base = dict(source_key="kalshi:0:KXNHLGAME-26OCT01BOSTOR-TOR:ord-1", market_ticker="KXNHLGAME-26OCT01BOSTOR-TOR",
                sport="NHL", game_date="2026-10-01", side="YES", contracts=Decimal("25"), vwap_price=Decimal("0.57"),
                total_fees=Decimal("0.36"), stake=Decimal("14.61"), first_execution_time=Decimal("1790892903"),
                last_execution_time=Decimal("1790892905"), fill_count=2, finality=OrderFinality.FINAL_MARKET_CLOSED)
    base.update(kw)
    return ProductionWager(**base)


def test_nhl_row_builder_emits_exactly_the_nhl_schema_and_no_model_fields():
    row = to_nhl_import_row(production_wager(), ROUTER_IMPORT_BATCH_ID)
    assert tuple(row) == NHL_ROW_FIELDS
    assert row["entry_method"] == "IMPORTED_RECEIPT" and row["venue"] == "kalshi" and row["fees_are_estimated"] is False
    assert row["executed_at"].endswith("Z") and row["game_date"] == "2026-10-01"
    assert row["stake"] == pytest.approx(row["contracts"] * row["execution_price"] + row["fees_paid"])
    assert not {k for k in row if "model" in k or "recommend" in k or k in ("edge", "wager_id", "season")}
    assert ROW_BUILDERS["NHL"] is to_nhl_import_row and ROW_BUILDERS["NHL"] is not to_cfb_import_row
    with pytest.raises(ValueError):
        to_nhl_import_row(production_wager(), "  ")


def test_nhl_production_payload_is_written_in_nhl_words(tmp_path):
    counts = write_payloads([production_wager(), production_wager(source_key="kalshi:0:x:ord-0")], str(tmp_path))
    assert counts == {"NHL": 2}
    payload = json.loads((tmp_path / "NHL.json").read_text())
    assert payload["importBatchId"] == ROUTER_IMPORT_BATCH_ID
    assert [r["source_bet_key"] for r in payload["rows"]] == sorted(r["source_bet_key"] for r in payload["rows"])
    assert all(tuple(r) == tuple(sorted(NHL_ROW_FIELDS)) for r in payload["rows"])  # json sort_keys


# ------------------------------------------------------------------------------------------------ profile
def test_nhl_destination_profile():
    p = profile_for("NHL")
    assert p.repo == "chmoses98/NHL-edge-finder" and p.ledger_branch == "accounting-data" and p.code_branch == "main"
    assert p.mergeable_paths == frozenset({"data/accounting/wagers.jsonl", "data/accounting/settlements.jsonl"})
    assert p.committable_prefixes == ("data/accounting/",) and p.mergeable_patterns == ()
    assert p.settlement_economics == ECONOMICS_V2 and p.requires_season is False and p.record_layout == "jsonl"
    assert p.row_identity_field == "source_bet_key" and p.ledger_branch_runs_ci is False
    assert p.ledger_validator is not None and p.settlement_importer is not None
    cmd = render_command(p.wager_importer, payload="/p.json", work="/w", code="/c", receipts="/r.json")
    assert cmd == ["python", "/c/scripts/accounting/import_routed_wagers.py", "--payload", "/p.json", "--base-dir", "/w",
                   "--receipts-out", "/r.json"]
    assert render_command(p.settlement_importer, payload="/s.json", work="/w", code="/c", receipts="/r.json")[1] == \
        "/c/scripts/accounting/import_routed_settlements.py"
    assert render_command(p.ledger_validator, payload="", work="/w", code="/c", receipts="/v.json")[1] == \
        "/c/scripts/accounting/validate_routed_ledger.py"
    assert describe("NHL")["needs_separate_code_checkout"] is True


def test_nhl_exact_merge_paths_exclude_everything_else():
    p = PROFILES[Sport.NHL]
    for path in ("data/accounting/wagers.jsonl", "data/accounting/settlements.jsonl"):
        assert path in p.mergeable_paths
    for path in ("data/accounting/other.jsonl", "data/archive/predictions.jsonl", "README.md",
                 "data/accounting/wagers.jsonl.bak", "src/nhl_edge/__init__.py"):
        assert path not in p.mergeable_paths


# ------------------------------------------------------------------------------------------------ settlements
def settlement(**kw) -> WagerSettlement:
    base = dict(source_bet_key="kalshi:0:KXNHLGAME-26OCT01BOSTOR-TOR:ord-1", market_ticker="KXNHLGAME-26OCT01BOSTOR-TOR",
                side="YES", settlement_status=SETTLED, settled_at="2026-10-02T02:40:00Z", result="WON",
                gross_return=Decimal("25"), net_profit_loss=Decimal("10.39"), refusals=(), economics_version=ECONOMICS_V2)
    base.update(kw)
    return WagerSettlement(**base)


def test_nhl_settlement_payload_is_v2(tmp_path):
    counts = write_settlement_payloads([settlement()], str(tmp_path), {settlement().source_bet_key: "NHL"})
    assert counts == {"NHL": 1}
    row = json.loads((tmp_path / "NHL-settlements.json").read_text())["settlements"][0]
    assert row["economics_version"] == ECONOMICS_V2 and row["venue"] == "kalshi"
    assert set(row) == {"source_bet_key", "market_ticker", "side", "settlement_status", "settled_at", "result",
                        "gross_return", "net_profit_loss", "refusals", "venue", "economics_version"}


# ------------------------------------------------------------------------------------------------ receipts
def test_nhl_receipts_normalise():
    receipts = {"kind": "wagers", "written": 1, "alreadyPresent": 1, "refused": 1, "rows": [
        {"row": 0, "source_bet_key": "k1", "wager_id": "nhlw-aaa", "status": "NEW", "duplicate_status": "NEW", "success": True},
        {"row": 1, "source_bet_key": "k2", "wager_id": "nhlw-bbb", "status": "DUPLICATE_NOOP", "duplicate_status": "DUPLICATE_NOOP", "success": True},
        {"row": 2, "source_bet_key": "k3", "wager_id": "nhlw-ccc", "status": "CONFLICT", "duplicate_status": "CONFLICT",
         "success": False, "reason": "different economics", "conflicting_fields": ["stake"]},
    ]}
    r = normalise(receipts)
    assert [x.identity for x in r] == ["nhlw-aaa", "nhlw-bbb", "nhlw-ccc"]
    assert [x.needs_judgement for x in r] == [False, False, True]
    assert r[1].verdict in IDEMPOTENT_RERUN_VERDICTS and r[2].conflicting_fields == ("stake",)
    s = normalise({"rows": [{"source_bet_key": "k1", "settlement_id": "nhls-aaa", "status": "DUPLICATE_NOOP",
                             "duplicate_status": "DUPLICATE_NOOP", "success": True}]})
    assert s[0].identity == "nhls-aaa" and not s[0].needs_judgement


# ------------------------------------------------------------------------------------------------ reconciliation
def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def test_nhl_reconciliation_reads_the_exact_ledger_paths(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    import reconcile_delivery as rd

    work = tmp_path / "work"
    (work / "data" / "accounting").mkdir(parents=True)
    _git(work, "init", "-q")
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "data/accounting/wagers.jsonl").write_text(json.dumps({"source_bet_key": "k-on-ledger"}) + "\n")
    (work / "data/accounting/settlements.jsonl").write_text(json.dumps({"source_bet_key": "k-on-ledger"}) + "\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "ledger")
    prof = profile_for("NHL")
    assert rd.ledger_keys(str(work), "HEAD", prof, "wagers") == {"k-on-ledger"}
    assert rd.ledger_keys(str(work), "HEAD", prof, "settlements") == {"k-on-ledger"}
    receipts = normalise({"rows": [{"source_bet_key": "k-new", "wager_id": "nhlw-1", "duplicate_status": "NEW", "success": True}]})
    out = rd.classify(["k-on-ledger", "k-new", "k-lost"], {"k-on-ledger"}, receipts)
    assert out["counts"] == {"ON_LEDGER": 1, "PROPOSED_NOT_MERGED": 1, "REFUSED": 0, "UNACCOUNTED": 1}


# ------------------------------------------------------------------ end to end with the destination's own code
def _nhl_checkout() -> Path | None:
    for cand in (os.environ.get("NHL_EDGE_FINDER_DIR"), ROOT.parent / "NHL-edge-finder"):
        if cand and (Path(cand) / "scripts" / "accounting" / "import_routed_wagers.py").exists():
            return Path(cand)
    return None


NHL = _nhl_checkout()


@pytest.mark.skipif(NHL is None, reason="no NHL-edge-finder checkout (set NHL_EDGE_FINDER_DIR); CI proves the router side only")
def test_end_to_end_with_the_nhl_destination_importers(tmp_path):
    prof = profile_for("NHL")
    work = tmp_path / "accounting-data"
    work.mkdir()
    payload_dir = tmp_path / "out"
    write_payloads([production_wager()], str(payload_dir))
    write_settlement_payloads([settlement()], str(payload_dir), {settlement().source_bet_key: "NHL"})

    def run(template, payload, receipts):
        cmd = render_command(template, payload=str(payload), work=str(work), code=str(NHL), receipts=str(receipts))
        cmd[0] = sys.executable
        return subprocess.run(cmd, capture_output=True, text=True)

    def ledger_bytes():
        return tuple((work / p).read_bytes() for p in sorted(prof.mergeable_paths) if (work / p).exists())

    r1 = run(prof.wager_importer, payload_dir / "NHL.json", tmp_path / "w1.json")
    assert r1.returncode == 0, r1.stdout + r1.stderr
    after_first = ledger_bytes()
    r2 = run(prof.wager_importer, payload_dir / "NHL.json", tmp_path / "w2.json")
    assert r2.returncode == 0 and ledger_bytes() == after_first
    w1, w2 = (normalise(json.loads((tmp_path / f).read_text())) for f in ("w1.json", "w2.json"))
    assert [x.verdict for x in w1] == ["NEW"] and [x.verdict for x in w2] == ["DUPLICATE_NOOP"]
    assert w1[0].identity == w2[0].identity and w1[0].identity.startswith("nhlw-")

    s1 = run(prof.settlement_importer, payload_dir / "NHL-settlements.json", tmp_path / "s1.json")
    assert s1.returncode == 0, s1.stdout + s1.stderr
    after_settle = ledger_bytes()
    s2 = run(prof.settlement_importer, payload_dir / "NHL-settlements.json", tmp_path / "s2.json")
    assert s2.returncode == 0 and ledger_bytes() == after_settle
    assert [x.verdict for x in normalise(json.loads((tmp_path / "s2.json").read_text()))] == ["DUPLICATE_NOOP"]

    orphan_dir = tmp_path / "orphan"
    orphan = settlement(source_bet_key="kalshi:0:nobody:ord-9")
    write_settlement_payloads([orphan], str(orphan_dir), {orphan.source_bet_key: "NHL"})
    o = run(prof.settlement_importer, orphan_dir / "NHL-settlements.json", tmp_path / "o.json")
    assert o.returncode == 1 and ledger_bytes() == after_settle
    assert [x.verdict for x in normalise(json.loads((tmp_path / "o.json").read_text()))] == ["REFUSED"]

    v = run(prof.ledger_validator, "", tmp_path / "v.json")
    assert v.returncode == 0, v.stdout
    written = {str(p.relative_to(work)) for p in work.rglob("*") if p.is_file()}
    assert written == set(prof.mergeable_paths)  # containment: nothing outside the two exact ledger paths
    for text in (r1.stdout, r2.stdout, s1.stdout, o.stdout, v.stdout):
        for secret in ("KXNHLGAME", "14.61", "0.57", "10.39", "ord-1"):
            assert secret not in text


# ------------------------------------------------------------------ the live-sample probe (fake client)
class _FakeClient:
    def __init__(self, competition="Pro Hockey"):
        self.competition = competition

    def get_market(self, ticker):
        return {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "status": "active"}

    def get_event(self, event_ticker):
        return {"event_ticker": event_ticker, "series_ticker": event_ticker.split("-")[0], "title": "Chicago at Vegas"}

    def get_event_metadata(self, event_ticker):
        return {"competition": self.competition, "competition_scope": "Game"}

    def get_series(self, series_ticker):
        return {"ticker": series_ticker, "title": "NHL", "category": "Sports", "tags": ["Hockey"]}


def test_series_probe_nhl_live_sample_reports_counts_only():
    from kalshi_router.cli import _nhl_live_sample
    from kalshi_router.series_probe import NHL_LIVE_SAMPLE_MARKETS

    text = _nhl_live_sample(_FakeClient(), LIVE_LIKE)
    assert "NHL_LIVE_SAMPLE_ALL_NHL=true" in text and f"'NHL': {len(NHL_LIVE_SAMPLE_MARKETS)}" in text
    assert not any(t in text for t in NHL_LIVE_SAMPLE_MARKETS)
    assert "NHL_LIVE_SAMPLE_ALL_NHL=false" in _nhl_live_sample(_FakeClient("College Hockey"), LIVE_LIKE)
