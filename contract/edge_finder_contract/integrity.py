"""Cross-reference integrity across one sport's bundle. Files are consistent or the bundle is not
published: a recommendation whose market is not in markets.json is a bug the UI must never see."""

from __future__ import annotations

from . import SCHEMA_VERSION


def check_bundle(bundle: dict[str, dict]) -> list[str]:
    """``bundle`` maps kind -> document (events, markets, model_prices, recommendations, theses,
    wagers, settlements, runs). Returns every integrity problem; empty means consistent."""
    problems: list[str] = []
    items = {kind: doc.get("items", []) for kind, doc in bundle.items() if isinstance(doc, dict)}
    run_ids = {doc.get("run_id") for doc in bundle.values() if isinstance(doc, dict)}
    if len(run_ids) > 1:
        problems.append(f"files disagree on run_id: {sorted(str(r) for r in run_ids)}")
    versions = {doc.get("schema_version") for doc in bundle.values() if isinstance(doc, dict)}
    if versions - {SCHEMA_VERSION}:
        problems.append(f"unexpected schema_version(s): {sorted(str(v) for v in versions)}")
    sports = {doc.get("sport") for doc in bundle.values() if isinstance(doc, dict)}
    if len(sports) > 1:
        problems.append(f"files disagree on sport: {sorted(str(s) for s in sports)}")

    events = {e["event_id"]: e for e in items.get("events", [])}
    markets = {m["market_id"]: m for m in items.get("markets", [])}
    prices = {p["model_price_id"]: p for p in items.get("model_prices", [])}
    recs = {r["recommendation_id"]: r for r in items.get("recommendations", [])}
    theses = {t["thesis_id"]: t for t in items.get("theses", [])}
    wagers = {w["wager_id"]: w for w in items.get("wagers", [])}
    settlements = {s["settlement_id"]: s for s in items.get("settlements", [])}
    runs = {r["run_id"]: r for r in items.get("runs", [])}

    for kind, table, key in (("events", items.get("events", []), "event_id"), ("markets", items.get("markets", []), "market_id"),
                             ("model_prices", items.get("model_prices", []), "model_price_id"),
                             ("recommendations", items.get("recommendations", []), "recommendation_id"),
                             ("wagers", items.get("wagers", []), "wager_id"),
                             ("settlements", items.get("settlements", []), "settlement_id")):
        seen = [row[key] for row in table]
        if len(seen) != len(set(seen)):
            dupes = sorted({x for x in seen if seen.count(x) > 1})[:5]
            problems.append(f"{kind}: duplicate {key}: {dupes}")

    participants = {}
    for ev in events.values():
        for p in ev.get("participants", []):
            participants[p["participant_id"]] = p
        for slot in ("home_participant", "away_participant"):
            pid = ev.get(slot)
            if pid and pid not in {p["participant_id"] for p in ev.get("participants", [])}:
                problems.append(f"event {ev['event_id']}: {slot} {pid} is not among its participants")
    for m in markets.values():
        if m.get("event_id") and m["event_id"] not in events:
            problems.append(f"market {m['market_id']}: event_id {m['event_id']} not in events")
    for p in prices.values():
        if p["market_id"] not in markets:
            problems.append(f"model_price {p['model_price_id']}: market_id {p['market_id']} not in markets")
        if p.get("event_id") and p["event_id"] not in events:
            problems.append(f"model_price {p['model_price_id']}: event_id {p['event_id']} not in events")
        if runs and p["run_id"] not in runs:
            problems.append(f"model_price {p['model_price_id']}: run_id {p['run_id']} not in runs")
    for r in recs.values():
        if r["market_id"] not in markets:
            problems.append(f"recommendation {r['recommendation_id']}: market_id {r['market_id']} not in markets")
        if r["event_id"] not in events:
            problems.append(f"recommendation {r['recommendation_id']}: event_id {r['event_id']} not in events")
        if r.get("thesis_id") and r["thesis_id"] not in theses:
            problems.append(f"recommendation {r['recommendation_id']}: thesis_id {r['thesis_id']} not in theses")
    for t in theses.values():
        if t["event_id"] not in events:
            problems.append(f"thesis {t['thesis_id']}: event_id {t['event_id']} not in events")
    for w in wagers.values():
        if w["market_id"] not in markets:
            problems.append(f"wager {w['wager_id']}: market_id {w['market_id']} not in markets")
        if w.get("event_id") and w["event_id"] not in events:
            problems.append(f"wager {w['wager_id']}: event_id {w['event_id']} not in events")
        if w.get("settlement_id") and w["settlement_id"] not in settlements:
            problems.append(f"wager {w['wager_id']}: settlement_id {w['settlement_id']} not in settlements")
        if w.get("recommendation_id") and w["recommendation_id"] not in recs:
            problems.append(f"wager {w['wager_id']}: recommendation_id {w['recommendation_id']} not in recommendations")
        if w.get("model_price_id") and w["model_price_id"] not in prices:
            problems.append(f"wager {w['wager_id']}: model_price_id {w['model_price_id']} not in model_prices")
        if w.get("settlement_status") == "SETTLED" and not w.get("settlement_id"):
            problems.append(f"wager {w['wager_id']}: SETTLED without a settlement_id")
    for s in settlements.values():
        if s["wager_id"] not in wagers:
            problems.append(f"settlement {s['settlement_id']}: wager_id {s['wager_id']} not in wagers")
        elif wagers[s["wager_id"]]["market_id"] != s["market_id"]:
            problems.append(f"settlement {s['settlement_id']}: market_id disagrees with its wager")
    return problems


def check_manifest(manifest: dict, documents: dict[str, dict], digests: dict[str, str]) -> list[str]:
    """The manifest names every file, with the run_id, count and sha256 the files actually have."""
    problems: list[str] = []
    for name, entry in manifest.get("files", {}).items():
        doc = documents.get(name)
        if doc is None:
            problems.append(f"manifest names {name} but no such document was produced")
            continue
        if doc.get("run_id") != manifest.get("run_id"):
            problems.append(f"{name}: run_id {doc.get('run_id')} != manifest run_id {manifest.get('run_id')}")
        if entry.get("kind") != doc.get("kind"):
            problems.append(f"{name}: kind {doc.get('kind')} != manifest kind {entry.get('kind')}")
        if "items" in doc and entry.get("count") != len(doc["items"]):
            problems.append(f"{name}: count {len(doc['items'])} != manifest count {entry.get('count')}")
        if digests.get(name) and entry.get("sha256") != digests[name]:
            problems.append(f"{name}: sha256 does not match the manifest")
    for name in documents:
        if name not in manifest.get("files", {}) and name not in ("manifest", "health"):
            problems.append(f"document {name} is not listed in the manifest")
    return problems
