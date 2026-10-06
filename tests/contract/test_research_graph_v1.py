"""The research graph (contract 1.1.0): schemas, identities, graph integrity, metric registry, comparison
universes, rankings, time series, UTC, quality/provenance, capability truthfulness, search index, packet
determinism / deduplication / tray resolution / missing data / RESEARCH preservation, no-secret scans,
and backward compatibility with edge_finder.app.v1 publications that carry no explorer."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import edge_finder_contract as efc
from edge_finder_contract import ids, packet as P, publish, research as R, schema_defs, timeutil, validate
from tests.contract.fixtures import CAPTURED, NOW, documents_for
from tests.contract.research_fixtures import publish_app, research_documents

PKG = Path(efc.__file__).resolve().parent


@pytest.fixture(scope="module", params=efc.SPORTS)
def app(request, tmp_path_factory):
    root = tmp_path_factory.mktemp(f"app-{request.param}")
    run_id, index = publish_app(request.param, root)
    return request.param, root, run_id, index


# ------------------------------------------------------------------ schemas and vocabulary

def test_new_kinds_have_committed_schemas_equal_to_the_generator():
    for kind in ("explorer_index", "capability_manifest", "metric_registry", "entity_profile", "event_research", "ranking",
                 "time_series", "market_history", "search_index", "handicap_packet", "handicap_protocol", "research_tray"):
        assert kind in efc.KINDS
        on_disk = (PKG / "schemas" / f"{kind}.schema.json").read_text(encoding="utf-8")
        assert on_disk == schema_defs.render(schema_defs.all_schemas()[kind])


def test_capability_vocabulary_is_closed_and_the_manifest_answers_every_entry(app):
    sport, root, run_id, index = app
    caps = json.loads((root / "explorer" / "capabilities.json").read_text())
    assert [c["capability"] for c in caps["items"]] == list(efc.CAPABILITIES)
    with pytest.raises(ValueError):
        R.capability(capability="teleportation", status="VERIFIED", summary="no")


def test_quality_statuses_are_the_closed_vocabulary():
    assert efc.QUALITY_STATUSES == ("VERIFIED", "PARTIAL", "RESEARCH", "UNAVAILABLE", "UNKNOWN")
    with pytest.raises(ValueError):
        R.quality(status="GREAT", source="x", generated_at=NOW, production=True)
    with pytest.raises(ValueError, match="limitations"):
        R.quality(status="PARTIAL", source="x", generated_at=NOW, production=True)


# ------------------------------------------------------------------ identities

def test_research_ids_are_deterministic_prefixed_and_distinct():
    m = ids.metric_id("nfl", "pass_epa_per_play")
    assert m == "met_nfl.pass_epa_per_play" == ids.metric_id("NFL", "PASS_EPA_PER_PLAY".lower())
    with pytest.raises(ValueError):
        ids.metric_id("NFL", "Pass EPA")
    o1 = ids.observation_id("NFL", m, "prt_x", "SEASON", None, CAPTURED)
    assert o1 == ids.observation_id("NFL", m, "prt_x", "SEASON", None, CAPTURED) and o1.startswith("obs_") and len(o1) == 24
    assert o1 != ids.observation_id("NFL", m, "prt_x", "L3", None, CAPTURED)
    assert ids.ranking_id("NFL", m, "NFL teams", "SEASON") != ids.ranking_id("NFL", m, "NFL teams", "L5")
    assert ids.series_id("NFL", m, "prt_x", "GAME") != ids.series_id("NFL", m, "prt_x", "WEEK")
    assert ids.packet_id("p", "GAME", "evt_b", "evt_a", data_as_of=NOW) == ids.packet_id("p", "GAME", "evt_a", "evt_b", data_as_of=NOW)
    assert ids.tray_item_id("TEAM", "prt_x").startswith("try_")


# ------------------------------------------------------------------ the published graph

def test_published_explorer_verifies_and_is_deterministic(app, tmp_path):
    sport, root, run_id, index = app
    assert R.verify_explorer(root) == []
    again = tmp_path / "again"
    shutil.copytree(root, again)
    _, docs, q = research_documents(sport)
    R.publish_explorer(app_root=again, sport=sport, run_id=run_id, generated_at=NOW, documents=docs, quality=q, as_of=CAPTURED,
                       commit_sha="deadbeef", base_manifest_run_id=run_id)
    assert R.digest_tree(again) == R.digest_tree(root)


def test_index_file_table_matches_the_files_on_disk(app):
    sport, root, run_id, index = app
    for rel, entry in index["files"].items():
        text = (root / "explorer" / rel).read_text(encoding="utf-8")
        assert publish.sha256_text(text) == entry["sha256"]
        assert len(text.encode("utf-8")) == entry["bytes"]
        assert json.loads(text)["kind"] == entry["kind"]
    assert set(index["counts"]) == {"teams", "players", "events", "rankings", "series", "market_history", "metrics"}


def test_cross_reference_integrity_refuses_dead_links_and_unknown_metrics(app, tmp_path):
    sport, root, run_id, index = app
    _, docs, q = research_documents(sport)
    prof = next(d for d in docs if d["kind"] == "entity_profile")
    broken = json.loads(json.dumps(prof))
    broken["links"].append(R.link(rel="PLAYER", target_kind="entity_profile", label="ghost", path="explorer/players/prt_ghost.json"))
    with pytest.raises(R.ExplorerError, match="dead link"):
        R.publish_explorer(app_root=tmp_path, sport=sport, run_id=run_id, generated_at=NOW,
                           documents=[d for d in docs if d is not prof] + [broken], quality=q)
    broken = json.loads(json.dumps(prof))
    broken["metrics"][0]["metric_id"] = f"met_{sport.lower()}.unregistered"
    with pytest.raises(R.ExplorerError, match="not in the registry"):
        R.publish_explorer(app_root=tmp_path, sport=sport, run_id=run_id, generated_at=NOW,
                           documents=[d for d in docs if d is not prof] + [broken], quality=q)
    assert not (tmp_path / "explorer").exists(), "a refused publish leaves nothing behind"


def test_every_profile_link_target_exists_and_the_journey_has_no_dead_end(app):
    """team -> opponent -> event -> market history -> event -> participant -> ranking -> entry -> profile -> series."""
    sport, root, run_id, index = app
    _, docs = R.load_explorer(root)
    published = {R.app_path(rel) for rel in index["files"]}
    start = next(d for d in docs.values() if d["kind"] == "entity_profile" and d["opponents"])
    opp = start["opponents"][0]
    assert opp["path"] in published
    ev_path = start["games"][0]["path"]
    ev = docs[ev_path[len("explorer/"):]]
    assert ev["market_history_path"] in published
    mh = docs[ev["market_history_path"][len("explorer/"):]]
    assert mh["links"][0]["path"] == ev_path
    back = docs[ev["participants"][0]["path"][len("explorer/"):]]
    rk = docs[back["rankings"][0]["path"][len("explorer/"):]]
    assert all(e["path"] in published for e in rk["entries"])
    ser = docs[back["series"][0]["path"][len("explorer/"):]]
    assert ser["links"][0]["path"] in published
    assert ser["points"][-1]["path"] == ev_path


# ------------------------------------------------------------------ metric registry, rankings, context

def test_metric_registry_entries_carry_meaning_and_quality(app):
    sport, root, run_id, index = app
    reg = json.loads((root / "explorer" / "metrics.json").read_text())
    assert reg["count"] == len(reg["items"]) == 2
    for m in reg["items"]:
        assert m["metric_id"].startswith(f"met_{sport.lower()}.")
        assert m["description"] and m["category"] and m["stat_type"] in ("RATE", "RATING")
        assert set(m["supports"]) == set(R.SUPPORT_KEYS)
        assert m["quality"]["status"] in efc.QUALITY_STATUSES
    research = next(m for m in reg["items"] if m["quality"]["status"] == "RESEARCH")
    assert research["quality"]["production"] is False and research["known_limitations"]
    with pytest.raises(ValueError, match="duplicate"):
        R.metric_registry(sport=sport, run_id=run_id, generated_at=NOW, metrics=[reg["items"][0], reg["items"][0]])


def test_ranking_is_a_full_comparison_universe_with_ties_and_summary():
    w = R.window("SEASON")
    q = R.quality(status="VERIFIED", source="t", generated_at=NOW, production=True)
    vals = [{"entity_id": f"prt_{i:020d}", "display_name": f"T{i}", "value": v} for i, v in enumerate([0.5, 0.2, 0.2, None, 0.9])]
    rk = R.ranking(sport="NFL", metric_id="met_nfl.x", universe_label="u", entity_type="TEAM", window=w, as_of=NOW,
                   higher_is_better=True, values=vals, run_id="run_" + "0" * 20, generated_at=NOW, quality=q)
    assert [e["rank"] for e in rk["entries"]] == [1, 2, 3, 3]
    assert rk["universe"]["size"] == 4, "the null value is excluded from the universe it was not ranked in"
    assert [e["percentile"] for e in rk["entries"]] == [100.0, 75.0, 50.0, 50.0]
    assert rk["summary"]["mean"] == 0.45 and rk["summary"]["median"] == 0.35 and rk["summary"]["best_entity_id"] == vals[4]["entity_id"]
    low = R.ranking(sport="NFL", metric_id="met_nfl.x", universe_label="u", entity_type="TEAM", window=w, as_of=NOW,
                    higher_is_better=False, values=vals, run_id="run_" + "0" * 20, generated_at=NOW, quality=q)
    assert low["entries"][0]["value"] == 0.2 and low["summary"]["best_entity_id"] == vals[1]["entity_id"]
    ctx = R.context_from_ranking(low, vals[4]["entity_id"])
    assert ctx["rank"] == 4 and ctx["universe_size"] == 4 and ctx["best_value"] == 0.2 and ctx["worst_value"] == 0.9
    assert R.context_from_ranking(low, "prt_absent00000000000000") is None


def test_observation_context_is_consistent_with_its_published_ranking(app):
    sport, root, run_id, index = app
    _, docs = R.load_explorer(root)
    rankings = {d["ranking_id"]: d for d in docs.values() if d["kind"] == "ranking"}
    checked = 0
    for d in docs.values():
        if d["kind"] != "entity_profile":
            continue
        for o in d["metrics"]:
            if not o.get("context"):
                continue
            rk = rankings[o["context"]["ranking_id"]]
            entry = next(e for e in rk["entries"] if e["entity_id"] == o["entity_id"])
            assert entry["rank"] == o["context"]["rank"] and entry["value"] == o["value"]
            assert o["context"]["universe_size"] == rk["universe"]["size"] == len(rk["entries"])
            assert o["context"]["league_average"] == rk["summary"]["mean"]
            checked += 1
    assert checked >= 3


# ------------------------------------------------------------------ time series and UTC

def test_time_series_points_are_ordered_and_rolling_is_arithmetic(app):
    sport, root, run_id, index = app
    _, docs = R.load_explorer(root)
    for d in docs.values():
        if d["kind"] == "time_series":
            ts = [timeutil.parse_ts(p["t"]) for p in d["points"]]
            assert ts == sorted(ts)
            vals = [p["value"] for p in d["points"]]
            assert d["points"][-1]["rolling_value"] == round(sum(vals) / 3, 6)
            assert d["points"][0]["rolling_value"] == vals[0]
            assert d["points"][-1]["event_id"] and d["points"][-1]["path"].startswith("explorer/events/")


def test_naive_timestamps_are_refused_everywhere():
    q = R.quality(status="VERIFIED", source="t", generated_at=NOW, production=True)
    with pytest.raises(timeutil.NaiveTimestampError):
        R.quality(status="VERIFIED", source="t", generated_at="2026-10-02T15:00:00", production=True)
    with pytest.raises(timeutil.NaiveTimestampError):
        R.point(x="g", t="2026-10-02 15:00", value=1, quality_status="VERIFIED")
    with pytest.raises(timeutil.NaiveTimestampError):
        R.observation(sport="NFL", metric_id="met_nfl.x", entity_id="prt_x", entity_type="TEAM", value=1, window=R.window(),
                      as_of="2026-10-02T15:00:00", source="t", quality_status="VERIFIED")
    with pytest.raises(timeutil.NaiveTimestampError):
        R.price_point(captured_at="2026-10-02T15:00:00")
    with pytest.raises(timeutil.NaiveTimestampError):
        R.market_history(sport="NFL", run_id="run_" + "0" * 20, generated_at="2026-10-02T15:00:00", event_id="evt_x", as_of=NOW,
                         series=[], quality=q)


def test_market_history_points_are_sorted_by_capture_time(app):
    sport, root, run_id, index = app
    _, docs = R.load_explorer(root)
    mh = next(d for d in docs.values() if d["kind"] == "market_history")
    pts = mh["series"][0]["points"]
    assert [p["captured_at"] for p in pts] == sorted(p["captured_at"] for p in pts)
    assert pts[-1]["captured_at"] == CAPTURED


# ------------------------------------------------------------------ capability truthfulness

def test_capability_manifest_truthfulness_is_enforced(app, tmp_path):
    sport, root, run_id, index = app
    _, docs, q = research_documents(sport)
    caps = next(d for d in docs if d["kind"] == "capability_manifest")
    lying = json.loads(json.dumps(caps))
    row = next(c for c in lying["items"] if c["capability"] == "play_by_play")
    row.update(status="VERIFIED", evidence=["explorer/events/nope.json"])
    with pytest.raises(R.ExplorerError, match="play_by_play"):
        R.publish_explorer(app_root=tmp_path, sport=sport, run_id=run_id, generated_at=NOW,
                           documents=[d for d in docs if d is not caps] + [lying], quality=q)
    lying = json.loads(json.dumps(caps))
    row = next(c for c in lying["items"] if c["capability"] == "rankings")
    row.update(status="UNAVAILABLE", evidence=[])
    with pytest.raises(R.ExplorerError, match="UNAVAILABLE but ranking files"):
        R.publish_explorer(app_root=tmp_path, sport=sport, run_id=run_id, generated_at=NOW,
                           documents=[d for d in docs if d is not caps] + [lying], quality=q)
    lying = json.loads(json.dumps(caps))
    row = next(c for c in lying["items"] if c["capability"] == "market_price_history")
    row.update(limitations=[])
    with pytest.raises(R.ExplorerError, match="PARTIAL without limitations"):
        R.publish_explorer(app_root=tmp_path, sport=sport, run_id=run_id, generated_at=NOW,
                           documents=[d for d in docs if d is not caps] + [lying], quality=q)


def test_unassessed_capabilities_are_unknown_not_silent(app):
    sport, root, run_id, index = app
    caps = json.loads((root / "explorer" / "capabilities.json").read_text())
    unknown = [c for c in caps["items"] if c["status"] == "UNKNOWN"]
    assert unknown and all(c["summary"] == "not assessed in this publication" for c in unknown)


# ------------------------------------------------------------------ search index

def test_search_index_entries_point_at_published_files_and_tokens_are_normalised(app):
    sport, root, run_id, index = app
    si = json.loads((root / "explorer" / "search_index.json").read_text())
    published = {R.app_path(rel) for rel in index["files"]}
    assert si["count"] == len(si["items"]) >= 5
    for e in si["items"]:
        assert e["path"] in published
        assert e["tokens"] == sorted(set(e["tokens"])) and all(t == t.lower() for t in e["tokens"])
    assert R.tokens("Baltimore Ravens", "BAL") == ["bal", "baltimore", "ravens"]
    assert R.tokens("Joe Burrow (QB)") == ["burrow", "joe", "qb"]


# ------------------------------------------------------------------ handicap packet

def test_packet_is_deterministic_and_validates(app):
    sport, root, run_id, index = app
    ev = index["events"][0]["event_id"]
    a = P.build(app_root=root, scope_kind="GAME", event_id=ev)
    b = P.build(app_root=root, scope_kind="GAME", event_id=ev)
    assert a == b
    validate.validate_document(a)
    assert a["packet_id"].startswith("pkt_") and a["protocol"]["protocol_id"] == f"edge_finder.handicap.{sport.lower()}.v1"
    assert a["warning"].startswith("Everything in this packet is EVIDENCE")
    assert P.render_text(a) == P.render_text(b)


def test_packet_includes_every_market_in_scope_and_deduplicates(app):
    sport, root, run_id, index = app
    ev = index["events"][0]["event_id"]
    pk = P.build(app_root=root, scope_kind="GAME", event_id=ev)
    markets = json.loads((root / "markets.json").read_text())["items"]
    in_scope = {m["market_id"] for m in markets if m["event_id"] == ev}
    assert {m["market_id"] for m in pk["markets"]} == in_scope
    assert len(pk["markets"]) == len({m["market_id"] for m in pk["markets"]})
    assert len(pk["evidence"]) == len({e["entity_id"] for e in pk["evidence"]}) == 2
    for e in pk["evidence"]:
        keys = [(o["metric_id"], o["window"], o["split"]) for o in e["observations"]]
        assert len(keys) == len(set(keys))
    assert all(m["captured_at"] == CAPTURED and m["freshness"] for m in pk["markets"])
    assert pk["model_evidence"][0]["research_only"] is True and pk["model_evidence"][0]["authority"] == "RESEARCH_ONLY"


def test_packet_preserves_research_status_and_lists_missing_data(app):
    sport, root, run_id, index = app
    ev = index["events"][0]["event_id"]
    pk = P.build(app_root=root, scope_kind="GAME", event_id=ev)
    research_metric = f"met_{sport.lower()}.experimental_rating"
    assert research_metric in pk["quality"]["research_only_items"]
    statuses = {o["metric_id"]: o["quality_status"] for e in pk["evidence"] for o in e["observations"]}
    assert statuses[research_metric] == "RESEARCH" and statuses[f"met_{sport.lower()}.efficiency_index"] == "VERIFIED"
    assert pk["quality"]["missing"] == []
    assert pk["quality"]["capabilities"]["rankings"] == "VERIFIED" and pk["quality"]["capabilities"]["play_by_play"] == "UNAVAILABLE"
    text = P.render_text(pk)
    assert "[RESEARCH]" in text and "[VERIFIED]" in text and "MISSING" not in text


def test_packet_without_an_explorer_degrades_honestly(tmp_path):
    sport = "NFL"
    run_id, documents = documents_for(sport)
    publish.publish(root=tmp_path, sport=sport, run_id=run_id, generated_at=NOW, documents=documents,
                    source_repo="chmoses98/nfl-edge-finder", source_branch="main")
    ev = documents["events"]["items"][0]["event_id"]
    pk = P.build(app_root=tmp_path, scope_kind="GAME", event_id=ev)
    assert pk["evidence"] == [] and pk["markets"] and pk["model_evidence"]
    assert any("no event research" in m for m in pk["quality"]["missing"])
    assert any("no capability manifest" in m for m in pk["quality"]["missing"])
    assert any("no profile for" in m for m in pk["quality"]["missing"])


def test_research_tray_resolves_references_and_reports_the_unresolved(app):
    sport, root, run_id, index = app
    ev = index["events"][0]["event_id"]
    _, docs = R.load_explorer(root)
    ser = next(d for d in docs.values() if d["kind"] == "time_series")
    prof = next(d for d in docs.values() if d["kind"] == "entity_profile")
    tr = P.tray([
        P.tray_item(ref_kind="EVENT", sport=sport, id=ev, added_at=NOW),
        P.tray_item(ref_kind=prof["entity_type"], sport=sport, id=prof["entity"]["participant_id"], added_at=NOW, note="watch this"),
        P.tray_item(ref_kind="CHART_POINT", sport=sport, id=ser["series_id"], added_at=NOW, extra={"series_id": ser["series_id"], "x": "g3"}),
        P.tray_item(ref_kind="METRIC", sport=sport, id=f"met_{sport.lower()}.efficiency_index", added_at=NOW),
        P.tray_item(ref_kind="PLAYER", sport=sport, id="prt_ghost0000000000000000", added_at=NOW),
        P.tray_item(ref_kind="MARKET", sport=sport, id="mkt_kalshi_NOPE", added_at=NOW),
    ], updated_at=NOW)
    validate.validate_document(tr)
    resolved = P.resolve_tray(P.AppRoot(root), tr)
    assert [r["resolved"] for r in resolved] == [True, True, True, True, False, False]
    assert resolved[2]["event_ids"] == [ev]
    pk = P.build(app_root=root, scope_kind="CUSTOM", tray_doc=tr)
    assert pk["scope"]["kind"] == "CUSTOM" and pk["scope"]["event_ids"] == [ev]
    assert [f["resolved"] for f in pk["user_focus"]] == [True, True, True, True, False, False]
    assert sum("tray item" in m for m in pk["quality"]["missing"]) == 2
    assert pk["user_focus"][1]["note"] == "watch this"
    text = P.render_text(pk)
    assert "USER FOCUS" in text and "[UNRESOLVED]" in text
    assert pk == P.build(app_root=root, scope_kind="CUSTOM", tray_doc=tr)


def test_slate_scope_selects_events_by_window(app):
    sport, root, run_id, index = app
    ev = index["events"][0]
    inside = P.build(app_root=root, scope_kind="SLATE", window_start="2026-09-30T00:00:00Z", window_end="2026-10-06T00:00:00Z")
    assert inside["scope"]["event_ids"] == [ev["event_id"]]
    empty = P.build(app_root=root, scope_kind="SLATE", window_start="2027-01-01T00:00:00Z", window_end="2027-01-02T00:00:00Z")
    assert empty["scope"]["event_ids"] == [] and "no current markets for the scope" in empty["quality"]["missing"]
    with pytest.raises(KeyError):
        P.build(app_root=root, scope_kind="GAME", event_id="evt_nope0000000000000000")


def test_packet_budget_trims_in_a_fixed_order_and_never_drops_markets(app):
    sport, root, run_id, index = app
    ev = index["events"][0]["event_id"]
    full = P.build(app_root=root, scope_kind="GAME", event_id=ev)
    small = P.build(app_root=root, scope_kind="GAME", event_id=ev, max_chars=4000)
    assert small["budget"]["truncated"] and len(small["markets"]) == len(full["markets"])
    assert small["budget"]["chars"] <= full["budget"]["chars"]
    assert small["packet_id"] == full["packet_id"], "the budget changes the content, not the identity of the request"


def test_protocols_are_versioned_valid_and_extend_the_core():
    core = P.load_protocol("edge_finder.handicap.core.v1")
    assert core["extends"] is None and core["sport"] == "ALL" and len(core["principles"]) >= 10
    assert any("evidence" in p.lower() and "never automatic bets" in p.lower() for p in core["principles"])
    assert any("pass" in p.lower() for p in core["principles"]) and any("counter" in p.lower() for p in core["principles"])
    for sport in efc.SPORTS:
        ext = P.protocol_for_sport(sport)
        assert ext["protocol_id"] == f"edge_finder.handicap.{sport.lower()}.v1" and ext["extends"] == core["protocol_id"]
        assert ext["principles"] == core["principles"] and ext["sport_notes"]
        validate.validate_document(ext)
    assert len(P.protocol_ids()) == 9


# ------------------------------------------------------------------ security and compatibility

def test_no_secret_shaped_strings_in_any_published_file(app, tmp_path):
    sport, root, run_id, index = app
    assert R.no_secret_shaped_strings(root) == []
    leak = tmp_path / "leak"
    shutil.copytree(root, leak)
    (leak / "explorer" / "metrics.json").write_text('{"token": "ghp_' + "A" * 36 + '"}')
    assert R.no_secret_shaped_strings(leak) == [str(leak / "explorer" / "metrics.json")]


def test_v1_publication_without_explorer_still_verifies_and_old_manifests_validate(tmp_path):
    sport = "MLB"
    run_id, documents = documents_for(sport)
    publish.publish(root=tmp_path, sport=sport, run_id=run_id, generated_at=NOW, documents=documents,
                    source_repo="chmoses98/edge-finder-api", source_branch="main")
    assert publish.verify_published(tmp_path) == ["no health.json"], "the v1 verifier is unchanged"
    assert R.read_index(tmp_path) is None
    assert R.verify_explorer(tmp_path) == ["no explorer/index.json"]
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert set(manifest["files"]) == set(documents) and all(e["kind"] in efc.KINDS for e in manifest["files"].values())


def test_explorer_index_can_be_listed_in_the_v1_manifest_additively(app):
    sport, root, run_id, index = app
    entry = {"path": "explorer/index.json", "kind": "explorer_index", "sha256": publish.sha256_text(json.dumps(index)),
             "bytes": 1, "count": None}
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["files"]["explorer_index"] = entry
    validate.validate_document(manifest)


def test_failed_explorer_publish_preserves_last_known_good(app, tmp_path):
    sport, root, run_id, index = app
    copy = tmp_path / "copy"
    shutil.copytree(root, copy)
    before = R.digest_tree(copy)
    _, docs, q = research_documents(sport)
    with pytest.raises(R.ExplorerError):
        R.publish_explorer(app_root=copy, sport=sport, run_id=run_id, generated_at=NOW,
                           documents=[d for d in docs if d["kind"] != "metric_registry"], quality=q)
    assert R.digest_tree(copy) == before and R.verify_explorer(copy) == []
    assert not any(p.name.startswith(".explorer-staging") for p in copy.iterdir())


def test_tree_bytes_reports_payload_per_directory(app):
    sport, root, run_id, index = app
    sizes = R.tree_bytes(root)
    assert set(sizes) >= {"index.json", "capabilities.json", "metrics.json", "search_index.json", "events", "rankings", "series", "market_history"}
    assert sizes["index.json"] < 20_000


def test_packet_text_groups_ladders_keeps_every_market_and_measures_the_clipboard(app):
    """Contract 1.1.1: the budget is the length of the text the user copies, ladders that differ only by
    their line render as one line with every rung, and no market is ever dropped from the text."""
    sport, root, run_id, index = app
    ev = index["events"][0]["event_id"]
    pk = P.build(app_root=root, scope_kind="GAME", event_id=ev)
    text = P.render_text(pk)
    assert pk["budget"]["chars"] == len(text)
    for m in pk["markets"]:
        assert m["kalshi_ticker"].split("-")[-1] in text
        assert set(m) >= {"period", "side", "line", "threshold"}
    ladder = [
        {"market_id": f"mkt_kalshi_KXT-26OCT05AAABBB-{n}", "kalshi_ticker": f"KXT-26OCT05AAABBB-{n}", "event_id": ev,
         "market_family": "total", "yes_description": f"YES iff total points (FULL) >= {n}.0", "yes_bid": 0.4, "yes_ask": 0.42,
         "mid": 0.41, "last_price": None, "captured_at": CAPTURED, "freshness": "FRESH", "market_status": "OPEN",
         "participant_id": None, "player_id": None, "period": "FULL", "side": "OVER", "line": None, "threshold": float(n)}
        for n in (40, 44, 48)
    ]
    lines = P._render_markets({**pk, "markets": ladder, "model_evidence": []})
    rows = [ln for ln in lines if ln.startswith("- ")]
    assert rows == ["- [total FULL] YES iff total points (FULL) >= X | KXT-26OCT05AAABBB-*: 40 40/42; 44 40/42; 48 40/42"]


def test_v1_publish_leaves_the_explorer_tree_alone(app, tmp_path):
    """Contract 1.1.1: a v1 republish must not prune explorer/ (research.publish_explorer owns it), so a
    research export that fails after a successful v1 publish still leaves the last-known-good explorer."""
    sport, root, run_id, index = app
    copy = tmp_path / "copy"
    shutil.copytree(root, copy)
    before = R.digest_tree(copy)
    _, documents = documents_for(sport)
    publish.publish(root=copy, sport=sport, run_id=run_id, generated_at=NOW, documents=documents,
                    source_repo="chmoses98/edge-finder-api", source_branch="main")
    assert R.digest_tree(copy) == before
    assert R.verify_explorer(copy) == []


def test_refresh_due_skips_rebuilds_until_events_change_or_the_tree_ages(app, tmp_path):
    sport, root, run_id, index = app
    assert R.refresh_due(tmp_path, now=NOW, min_interval_seconds=3600)[0] is True  # nothing published yet
    copy = tmp_path / "copy"
    shutil.copytree(root, copy)
    due, why = R.refresh_due(copy, now=NOW, min_interval_seconds=3600)
    assert due is False and "unchanged" in why
    assert R.refresh_due(copy, now="2026-10-03T12:00:00Z", min_interval_seconds=3600)[0] is True
    events = json.loads((copy / "events.json").read_text())
    events["items"] = []
    (copy / "events.json").write_text(json.dumps(events))
    due, why = R.refresh_due(copy, now=NOW, min_interval_seconds=3600)
    assert due is True and "events changed" in why
