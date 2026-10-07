"""The edge_finder.app.v1 contract: schemas, identities, timestamps, freshness, health, integrity,
atomic publication, and the per-sport fixtures that every adapter must be able to produce."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import edge_finder_contract as efc
from edge_finder_contract import board, build, freshness, health, ids, integrity, linkage, performance, publish, sync, timeutil
from edge_finder_contract import schema_defs, validate
from tests.contract.fixtures import CAPTURED, MODELLED, NOW, SPORT_CASES, bundle_for, documents_for

PKG = Path(efc.__file__).resolve().parent


# ------------------------------------------------------------------ schemas

def test_committed_schemas_equal_the_generator():
    for kind, schema in schema_defs.all_schemas().items():
        on_disk = (PKG / "schemas" / f"{kind}.schema.json").read_text(encoding="utf-8")
        assert on_disk == schema_defs.render(schema), f"{kind}.schema.json drifted from schema_defs.py"


def test_every_schema_keyword_is_one_the_validator_implements():
    for kind in validate.schema_kinds():
        unsupported = validate.schema_keywords(validate.load_schema(kind)) - validate.SUPPORTED_KEYWORDS
        assert not unsupported, (kind, unsupported)


def test_every_kind_has_a_schema():
    for kind in efc.KINDS:
        validate.load_schema(kind)


def test_vendored_manifest_matches_the_package():
    assert sync.check() == []


# ------------------------------------------------------------------ per-sport fixtures (W2..W7)

@pytest.mark.parametrize("sport", efc.SPORTS)
def test_each_sport_produces_a_valid_bundle(sport):
    run_id, docs = documents_for(sport)
    for kind, doc in docs.items():
        validate.validate_document(doc)
        assert doc["schema_version"] == efc.SCHEMA_VERSION
        assert doc["sport"] == sport
        assert doc["run_id"] == run_id
    assert integrity.check_bundle(docs) == []


def test_tennis_event_has_participants_but_no_home_away():
    ev = bundle_for("TENNIS")["events"][0]
    assert ev["home_participant"] is None and ev["away_participant"] is None
    assert [p["participant_type"] for p in ev["participants"]] == ["PLAYER", "PLAYER"]


def test_model_price_recommendation_and_wager_are_distinct_objects():
    b = bundle_for("NFL")
    assert set(b["model_prices"][0]) != set(b["recommendations"][0]) != set(b["wagers"][0])
    assert "fair_probability" in b["model_prices"][0] and "stake" not in b["model_prices"][0]
    assert "stake" in b["wagers"][0] and "bet_up_to_price" not in b["wagers"][0]
    assert b["recommendations"][0]["research_only"] is True


# ------------------------------------------------------------------ identities (W9)

def test_ids_are_deterministic_and_distinct():
    a = ids.event_id("NFL", "nflverse_game_id", "2026_04_PIT_CLE")
    b = ids.event_id("nfl", "nflverse_game_id", "2026_04_PIT_CLE")
    assert a == b and a.startswith("evt_") and len(a) == 24
    assert a != ids.event_id("NFL", "espn_event_id", "2026_04_PIT_CLE")
    assert ids.market_id("kxnflspread-26oct01pitcle-cle10") == "mkt_kalshi_KXNFLSPREAD-26OCT01PITCLE-CLE10"
    w = ids.wager_id("NFL", "kalshi:v1:abc")
    assert w == ids.wager_id("MLB", "kalshi:v1:abc"), "a router source key identifies the order regardless of sport"
    assert ids.settlement_id(w) == ids.settlement_id(w)
    with pytest.raises(ValueError):
        ids.event_id("NFL", "", "x")
    with pytest.raises(ValueError):
        ids.normalize_sport("college hoops")
    with pytest.raises(ValueError):
        ids.market_id("not a ticker!")


def test_length_prefixed_digest_cannot_be_forged_with_separators():
    assert ids.digest("A", "B:C") != ids.digest("A:B", "C")
    assert ids.digest("ab", "c") != ids.digest("a", "bc")


def test_sport_aliases_normalise_to_the_enum():
    assert ids.normalize_sport("Pro Football") == "NFL"
    assert ids.normalize_sport("soccer") == "SOCCER"
    assert ids.normalize_sport("Tennis") == "TENNIS"


# ------------------------------------------------------------------ timestamps (W8)

def test_naive_timestamps_are_refused_everywhere():
    with pytest.raises(timeutil.NaiveTimestampError):
        timeutil.to_iso("2026-10-02T15:00:00")
    with pytest.raises(timeutil.NaiveTimestampError):
        build.event(sport="NFL", source="x", source_id="1", start_time_utc="2026-10-02 15:00", participants=[])
    assert timeutil.to_iso("2026-10-02T11:00:00-04:00") == "2026-10-02T15:00:00Z"
    assert timeutil.to_iso("2026-10-02T15:00:00.123456+00:00") == "2026-10-02T15:00:00Z"
    assert timeutil.to_iso("2026-10-02T15:00:00.5Z", precision="microseconds") == "2026-10-02T15:00:00.500000Z"


def test_schema_rejects_non_utc_timestamps():
    doc = documents_for("NHL")[1]["events"]
    doc["items"][0]["start_time_utc"] = "2026-10-02T18:30:00-04:00"
    errors = validate.errors_for(doc, "events")
    assert any("canonical UTC" in e for e in errors)


# ------------------------------------------------------------------ unknown / null optional fields (W10)

def test_nulls_are_allowed_where_the_source_has_nothing():
    mk = build.market(sport="CFB", kalshi_ticker="KXNCAAFGAME-26OCT03OSUIOWA-OSU", market_family="game_moneyline",
                      yes_description="Ohio State wins", source="catalog")
    assert mk["yes_bid"] is None and mk["market_probability"] is None and mk["captured_at"] is None
    mp = build.model_price(run_id="run_" + "0" * 20, market_id=mk["market_id"], fair_probability=0.6, generated_at=NOW)
    assert mp["uncertainty"] is None and mp["edge"] is None, "never invented"


def test_unknown_fields_are_rejected():
    ev = bundle_for("MLB")["events"][0]
    ev["homeTeam"] = "ATL"
    assert any("additional property" in e for e in validate.errors_for(ev, "event"))


# ------------------------------------------------------------------ freshness and health (W11)

def test_freshness_is_deterministic():
    th = freshness.Thresholds(900, 3600)
    assert freshness.classify(0, th) == "FRESH"
    assert freshness.classify(900, th) == "FRESH"
    assert freshness.classify(901, th) == "AGING"
    assert freshness.classify(3601, th) == "STALE"
    assert freshness.classify(None, th) == "UNKNOWN"
    assert freshness.status_for(CAPTURED, now="2026-10-02T14:59:00Z") == "FRESH"
    assert freshness.status_for(CAPTURED, now="2026-10-02T17:00:00Z") == "STALE"


def _health(**kw):
    base = dict(sport="NHL", run_id="run_" + "1" * 20, bet_authority="RESEARCH_ONLY", last_market_capture=CAPTURED,
                last_model_generated=MODELLED, last_successful_run=NOW, payload_run_id="run_" + "1" * 20,
                payload_available=True, now="2026-10-02T15:00:00Z")
    base.update(kw)
    return health.build_health(**base)


def test_health_is_research_only_when_fresh_and_unpromoted():
    h = _health()
    assert h["overall_status"] == "RESEARCH_ONLY" and h["freshness_status"] == "FRESH"
    assert _health(bet_authority="MANUAL")["overall_status"] == "HEALTHY"


def test_health_is_stale_when_market_data_is_old():
    h = _health(now="2026-10-02T18:00:00Z")
    assert h["overall_status"] == "STALE" and h["market_data_status"] == "STALE"
    assert h["components"]["market_data"]["age_seconds"] > 3600


def test_health_never_healthy_when_required_data_missing():
    h = _health(last_market_capture=None, bet_authority="MANUAL")
    assert h["overall_status"] == "UNAVAILABLE"
    h = _health(payload_available=False)
    assert h["overall_status"] == "UNAVAILABLE"


def test_health_degraded_when_export_failed_but_payload_stands():
    h = _health(export_failed=True, errors=["exporter raised KeyError"], bet_authority="MANUAL")
    assert h["overall_status"] == "DEGRADED"
    assert h["components"]["export"]["status"] == "DEGRADED"


def test_health_market_not_required_for_a_research_sport_without_markets():
    # contract 1.2.0: CBB publishes no executable markets by design; health must not call that a failure
    h = _health(sport="CBB", last_market_capture=None, market_required=False)
    assert h["market_data_status"] == "NOT_APPLICABLE" and h["overall_status"] == "RESEARCH_ONLY"
    assert "extensions" not in h
    h = _health(sport="CBB", last_market_capture=None, market_required=False, last_model_generated=None)
    assert h["overall_status"] == "UNAVAILABLE", "the model stays required: no model data is never research-ready"
    h = _health(sport="CBB", last_market_capture=None, market_required=False,
                extensions={"cbb": {"prospective": {"n": 0}}})
    assert h["extensions"]["cbb"]["prospective"]["n"] == 0
    # the default still requires market data (every existing sport is unchanged)
    assert _health(last_market_capture=None, bet_authority="MANUAL")["overall_status"] == "UNAVAILABLE"


def test_health_files_published_before_1_2_0_still_validate():
    h = _health()
    assert "extensions" not in h
    validate.validate(h, "health")


def test_cbb_is_its_own_sport():
    assert ids.normalize_sport("cbb") == "CBB" and ids.normalize_sport("NCAAB") == "CBB"
    assert ids.normalize_sport("college basketball") == "CBB"
    assert ids.normalize_sport("basketball") == "NBA" and ids.normalize_sport("cfb") == "CFB"


def test_health_model_not_required_for_market_only_sport():
    h = _health(last_model_generated=None, model_required=False, bet_authority="MANUAL")
    assert h["overall_status"] == "HEALTHY" and h["model_status"] == "NOT_APPLICABLE"


# ------------------------------------------------------------------ cross-reference integrity (W16)

def test_integrity_catches_dangling_references():
    _, docs = documents_for("SOCCER")
    docs["recommendations"]["items"][0]["market_id"] = "mkt_kalshi_NOPE"
    docs["settlements"]["items"][0]["wager_id"] = "wgr_" + "9" * 20
    problems = integrity.check_bundle(docs)
    assert any("recommendation" in p and "not in markets" in p for p in problems)
    assert any("settlement" in p and "not in wagers" in p for p in problems)


def test_integrity_catches_run_id_disagreement():
    _, docs = documents_for("NBA")
    docs["markets"]["run_id"] = "run_" + "f" * 20
    assert any("disagree on run_id" in p for p in integrity.check_bundle(docs))


# ------------------------------------------------------------------ manifest + atomic publication (W17, W18)

def _publish(tmp_path, sport="NFL", **kw):
    run_id, docs = documents_for(sport)
    h = _health(sport=sport, run_id=run_id, payload_run_id=run_id)
    return publish.publish(root=tmp_path / "app" / "latest", sport=sport, run_id=run_id, generated_at=NOW,
                           documents=docs, source_repo=SPORT_CASES[sport]["repo"], source_branch="main",
                           commit_sha="abc123", model_version="m1", health=h, **kw)


def test_publish_writes_a_consistent_tree(tmp_path):
    manifest = _publish(tmp_path)
    root = tmp_path / "app" / "latest"
    assert publish.verify_published(root) == []
    for name, entry in manifest["files"].items():
        text = (root / entry["path"]).read_text(encoding="utf-8")
        assert publish.sha256_text(text) == entry["sha256"]
        assert json.loads(text)["run_id"] == manifest["run_id"]
    assert (root / "health.json").exists()
    assert manifest["counts"]["markets"] == 1


def test_failed_publish_leaves_last_known_good_untouched(tmp_path):
    _publish(tmp_path)
    root = tmp_path / "app" / "latest"
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    run_id, docs = documents_for("NFL")
    docs["recommendations"]["items"][0]["market_id"] = "mkt_kalshi_NOPE"
    with pytest.raises(publish.PublishError):
        publish.publish(root=root, sport="NFL", run_id=run_id, generated_at=NOW, documents=docs,
                        source_repo="r", source_branch="main")
    after = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    assert before == after
    assert not list(root.parent.glob(".staging-*"))
    failed = health.build_health(sport="NFL", run_id=run_id, bet_authority="MANUAL", last_market_capture=CAPTURED,
                                 last_model_generated=MODELLED, last_successful_run=NOW, payload_run_id=run_id,
                                 payload_available=True, export_failed=True, errors=["integrity"], now=NOW)
    publish.write_health_only(root, failed)
    assert json.loads((root / "health.json").read_text())["overall_status"] == "DEGRADED"
    assert publish.verify_published(root) == []


def test_publish_removes_event_details_that_left_the_board(tmp_path):
    run_id, docs = documents_for("MLB")
    b = bundle_for("MLB")
    detail = board.build_event_detail(sport="MLB", run_id=run_id, generated_at=NOW, event=b["events"][0],
                                      markets=b["markets"], model_prices=b["model_prices"], recommendations=b["recommendations"],
                                      theses=b["theses"], wagers=b["wagers"], settlements=b["settlements"], data_freshness="FRESH")
    docs[f"event_detail/{b['events'][0]['event_id']}"] = detail
    root = tmp_path / "latest"
    publish.publish(root=root, sport="MLB", run_id=run_id, generated_at=NOW, documents=docs, source_repo="r", source_branch="main")
    assert (root / "event_detail" / f"{b['events'][0]['event_id']}.json").exists()
    run_id, docs = documents_for("MLB")
    publish.publish(root=root, sport="MLB", run_id=run_id, generated_at=NOW, documents=docs, source_repo="r", source_branch="main")
    assert not (root / "event_detail").exists()


# ------------------------------------------------------------------ board, detail, performance, linkage

def test_board_and_event_detail_validate():
    b = bundle_for("NHL")
    run_id = b["runs"][0]["run_id"]
    h = _health(run_id=run_id, payload_run_id=run_id)
    bd = board.build_board(sport="NHL", run_id=run_id, generated_at=NOW, events=b["events"], markets=b["markets"],
                           model_prices=b["model_prices"], recommendations=b["recommendations"], wagers=b["wagers"],
                           health=h, now=NOW)
    row = bd["items"][0]
    assert row["markets_available"] == 1 and row["markets_priced"] == 1 and row["recommendations_count"] == 1
    assert row["data_freshness"] == "FRESH" and row["top_recommendations"][0]["research_only"] is True
    detail = board.build_event_detail(sport="NHL", run_id=run_id, generated_at=NOW, event=b["events"][0],
                                      markets=b["markets"], model_prices=b["model_prices"], recommendations=b["recommendations"],
                                      theses=b["theses"], wagers=b["wagers"], settlements=b["settlements"], data_freshness="FRESH")
    assert len(detail["wagers"]) == 1 and len(detail["settlements"]) == 1


def test_performance_never_fabricates_economics():
    b = bundle_for("CFB")
    pending = build.wager(sport="CFB", kalshi_ticker=b["markets"][0]["kalshi_ticker"], selection="NO", contracts=5, stake=2.8,
                          average_price=0.55, fees=None, placed_at=NOW, source="MANUAL", destination_repo="r", native_id="m1")
    unestablished = build.settlement(wager_id=pending["wager_id"], market_id=pending["market_id"], result="UNKNOWN",
                                     settled_at="2026-10-04T00:00:00Z", source="kalshi", verification_status="REFUSED",
                                     refusals=["shared_position_fee"])
    pending["settlement_status"], pending["settlement_id"] = "SETTLED", unestablished["settlement_id"]
    perf = performance.build_performance(sport="CFB", run_id=b["runs"][0]["run_id"], generated_at=NOW,
                                         wagers=b["wagers"] + [pending], settlements=b["settlements"] + [unestablished],
                                         markets=b["markets"], recommendations=b["recommendations"])
    t = perf["totals"]
    assert t["wagers"] == 2 and t["settled"] == 2 and t["won"] == 1 and t["unknown"] == 1
    assert t["net_pnl"] == 5.3 and t["settled_with_economics"] == 1
    assert perf["data_completeness"]["settlements_without_economics"] == 1
    assert perf["data_completeness"]["wagers_without_fees"] == 1


def test_linkage_is_temporal_never_retroactive():
    b = bundle_for("MLB")
    w = b["wagers"][0]
    linked = linkage.apply_links(w, b["model_prices"], b["recommendations"], b["markets"])
    assert linked["model_price_id"] == b["model_prices"][0]["model_price_id"]
    assert linked["recommendation_id"] == b["recommendations"][0]["recommendation_id"]
    assert linked["linkage"]["market_captured_at"] == CAPTURED
    late_price = dict(b["model_prices"][0], generated_at="2026-10-02T16:00:00Z", inputs_as_of="2026-10-02T16:00:00Z")
    late_rec = dict(b["recommendations"][0], created_at="2026-10-02T16:00:00Z")
    unlinked = linkage.apply_links(w, [late_price], [late_rec], [])
    assert unlinked["model_price_id"] is None and unlinked["model_run_id"] is None
    assert unlinked["recommendation_id"] is None and unlinked["linkage"]["market_captured_at"] is None


# ------------------------------------------------------------------ no secrets (W19)

SECRET_MARKERS = ("PRIVATE KEY", "KALSHI_PRIVATE_KEY", "KALSHI_API_KEY_ID", "ghp_", "github_pat_", "DOWNSTREAM_REPO_TOKEN",
                  "AIRTABLE", "Bearer ")


def test_published_documents_carry_no_secret_shaped_strings(tmp_path):
    _publish(tmp_path)
    for p in (tmp_path / "app" / "latest").rglob("*.json"):
        text = p.read_text(encoding="utf-8")
        for marker in SECRET_MARKERS:
            assert marker not in text, (p.name, marker)


def test_registry_lists_every_sport_and_the_router():
    reg = json.loads((PKG / "registry.json").read_text(encoding="utf-8"))
    validate.validate(reg, "sports_registry")
    assert set(reg["sports"]) == set(efc.SPORTS)
    for sport, loc in reg["sports"].items():
        assert loc["raw_base_url"] == f"https://raw.githubusercontent.com/{loc['repo']}/{loc['branch']}/{loc['app_root']}"
