#!/usr/bin/env python3
"""Build ``edge_finder_app_fixture.v1.json``: one development fixture for a frontend developer, assembled
from the real app exports of every sport (and the router's health), trimmed to a few events per sport.

    python scripts/build_app_fixture.py --out docs/fixtures/edge_finder_app_fixture.v1.json \
        --sport MLB=/path/to/edge-finder-api/app/latest --sport NFL=/path/to/... [--router /path/to/app-data/app/latest]
        [--events-per-sport 3]

Every sport block is the sport's own validated documents, filtered to the chosen events (plus every market,
model price, recommendation, thesis, wager and settlement that references them). Nothing is invented: a sport
whose export has no recommendations contributes none, and the fixture says so in ``coverage``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "contract"))

from edge_finder_contract import SCHEMA_VERSION, SPORTS  # noqa: E402
from edge_finder_contract.timeutil import now_utc, to_iso  # noqa: E402
from edge_finder_contract.validate import validate_document  # noqa: E402

COLLECTIONS = ("events", "markets", "model_prices", "recommendations", "theses", "wagers", "settlements", "runs")


def load(root: Path, name: str) -> dict | None:
    path = root / f"{name}.json"
    if not path.exists():
        return None
    doc = json.loads(path.read_text(encoding="utf-8"))
    validate_document(doc)
    return doc


def pick_events(board: dict | None, events: list[dict], n: int) -> list[str]:
    """Prefer events with recommendations and wagers, then markets, then the soonest."""
    if board:
        # Live events first (model prices, recommendations, markets), then make sure at least one event
        # that carries wagers/settlements is in the set so the accounting screens have real rows too.
        rows = sorted(board["items"], key=lambda r: (-r["recommendations_count"], -r["markets_priced"],
                                                     -r["markets_available"], r["start_time_utc"]))
        chosen = [r["event_id"] for r in rows[:n]]
        wagered = sorted((r for r in board["items"] if r["wagers_count"]), key=lambda r: -r["wagers_count"])
        if wagered and n > 1 and not any(r["wagers_count"] for r in rows[:n]):
            chosen = chosen[: n - 1] + [wagered[0]["event_id"]]
        if chosen:
            return chosen
    return [e["event_id"] for e in sorted(events, key=lambda e: e["start_time_utc"])[:n]]


def sport_block(sport: str, root: Path, n: int) -> dict:
    docs = {name: load(root, name) for name in COLLECTIONS}
    board = load(root, "board")
    health = load(root, "health")
    manifest = load(root, "manifest")
    performance = load(root, "performance")
    events = (docs["events"] or {}).get("items", [])
    keep = set(pick_events(board, events, n))
    markets = [m for m in (docs["markets"] or {}).get("items", []) if m.get("event_id") in keep]
    wagers = [w for w in (docs["wagers"] or {}).get("items", []) if w.get("event_id") in keep]
    # a wager's market must travel with it even when the market left the board
    market_ids = {m["market_id"] for m in markets} | {w["market_id"] for w in wagers}
    markets = [m for m in (docs["markets"] or {}).get("items", []) if m["market_id"] in market_ids]
    wager_ids = {w["wager_id"] for w in wagers}
    block = {
        "sport": sport,
        "source": {"repo": manifest["source_repo"] if manifest else None, "branch": manifest["source_branch"] if manifest else None,
                   "run_id": manifest["run_id"] if manifest else None, "generated_at": manifest["generated_at"] if manifest else None,
                   "commit_sha": manifest.get("commit_sha") if manifest else None},
        "events": [e for e in events if e["event_id"] in keep],
        "markets": markets,
        "model_prices": [p for p in (docs["model_prices"] or {}).get("items", []) if p["market_id"] in market_ids],
        "recommendations": [r for r in (docs["recommendations"] or {}).get("items", []) if r["event_id"] in keep],
        "theses": [t for t in (docs["theses"] or {}).get("items", []) if t["event_id"] in keep],
        "wagers": wagers,
        "settlements": [s for s in (docs["settlements"] or {}).get("items", []) if s["wager_id"] in wager_ids],
        "runs": (docs["runs"] or {}).get("items", []),
        "board": {**board, "items": [r for r in board["items"] if r["event_id"] in keep], "count": len(keep)} if board else None,
        "health": health,
        "performance": performance,
        "event_detail": {},
        "coverage": {name: len((docs[name] or {}).get("items", [])) for name in COLLECTIONS},
    }
    for eid in keep:
        detail = root / "event_detail" / f"{eid}.json"
        if detail.exists():
            d = json.loads(detail.read_text(encoding="utf-8"))
            validate_document(d)
            block["event_detail"][eid] = d
    return block


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True)
    ap.add_argument("--sport", action="append", default=[], help="SPORT=/path/to/app/latest")
    ap.add_argument("--router", default=None, help="/path/to/app-data/app/latest")
    ap.add_argument("--events-per-sport", type=int, default=3)
    a = ap.parse_args(argv)
    sports = {}
    for spec in a.sport:
        sport, _, path = spec.partition("=")
        if sport not in SPORTS:
            raise SystemExit(f"unknown sport {sport}")
        sports[sport] = sport_block(sport, Path(path), a.events_per_sport)
    router = None
    if a.router:
        router = {name: load(Path(a.router), name) for name in ("router_health", "recent_deliveries", "sports_registry")}
    registry = json.loads((ROOT / "contract" / "edge_finder_contract" / "registry.json").read_text(encoding="utf-8"))
    fixture = {
        "schema_version": SCHEMA_VERSION,
        "kind": "app_fixture",
        "fixture_version": "edge_finder_app_fixture.v1",
        "generated_at": to_iso(now_utc()),
        "description": "Real exports from every sport repository, trimmed to a few events each. Every object validates "
                       "against the edge_finder.app.v1 schemas; a frontend built on this fixture renders the live data unchanged.",
        "sports_registry": registry,
        "sports": sports,
        "router": router,
        "missing_sports": sorted(set(SPORTS) - set(sports)),
    }
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fixture, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    size = out.stat().st_size
    print(f"wrote {out} ({size / 1024:.0f} KB): sports {sorted(sports)}; missing {fixture['missing_sports']}")
    for sport, block in sports.items():
        c = block["coverage"]
        print(f"  {sport}: events {len(block['events'])}/{c['events']} markets {len(block['markets'])}/{c['markets']} "
              f"model_prices {len(block['model_prices'])}/{c['model_prices']} recs {len(block['recommendations'])}/{c['recommendations']} "
              f"wagers {len(block['wagers'])}/{c['wagers']} settlements {len(block['settlements'])}/{c['settlements']} "
              f"health {block['health']['overall_status'] if block['health'] else None}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
