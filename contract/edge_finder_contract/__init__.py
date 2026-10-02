"""edge_finder_contract -- the Edge Finder app contract, version ``edge_finder.app.v1``.

ONE SOURCE OF TRUTH. This package is authored in ``chmoses98/kalshi-bet-router`` under
``contract/edge_finder_contract`` and vendored byte-for-byte into every sport repository at the
same relative path. ``MANIFEST.json`` carries a sha256 per file; each repository's
``tests/test_app_contract_v1.py`` fails if its copy drifts from its own manifest, and
``python -m edge_finder_contract.sync --check <other repo>`` compares two copies.

Zero third-party dependencies, on purpose: the eight repositories agree on nothing except
Python 3.11, and a contract that needs ``jsonschema`` or ``pydantic`` could not be dropped into
``edge-finder-api`` (stdlib only) or ``nfl-edge-finder`` (stdlib handicap layer).

What lives here
    schemas/        JSON Schema (a documented subset of draft 2020-12) for every app-facing object
    validate.py     the validator for that subset
    ids.py          deterministic identities (event, participant, market, recommendation, wager ...)
    timeutil.py     UTC-only timestamps; naive datetimes are refused
    freshness.py    deterministic FRESH / AGING / STALE / UNKNOWN
    build.py        constructors that emit every field (null where unknown) so shapes never vary
    integrity.py    cross-reference checks across a bundle (recommendation.market_id exists ...)
    publish.py      atomic ``app/latest`` publication that never destroys last-known-good output
    health.py       the per-sport health object and its overall-status rule
    board.py        the compact current board and event-detail builders
    performance.py  wager -> settlement -> P&L aggregates (never fabricated)
    linkage.py      temporal wager <-> model/recommendation linkage (never retroactive)
    routed_ledger.py the shared destination accounting ledger (routed wagers + settlements)
    registry.json   where each sport publishes its app output (repo, branch, path)
"""

from __future__ import annotations

SCHEMA_VERSION = "edge_finder.app.v1"
CONTRACT_VERSION = "1.0.0"

#: The canonical sport vocabulary. Nothing else may reach the UI.
SPORTS = ("MLB", "CFB", "NFL", "NBA", "NHL", "SOCCER", "TENNIS")

#: Every app-facing file kind, and the schema that validates it.
KINDS = (
    "manifest", "events", "markets", "model_prices", "recommendations", "theses", "wagers",
    "settlements", "runs", "health", "board", "event_detail", "performance",
    "router_health", "recent_deliveries", "sports_registry",
)

__all__ = ["SCHEMA_VERSION", "CONTRACT_VERSION", "SPORTS", "KINDS"]
