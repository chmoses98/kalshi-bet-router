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
    research.py     the research graph (contract 1.1.0, additive): metric registry, observations with
                    comparison context, rankings, time series, entity profiles, event research, market
                    history, capability manifest, search index, and the atomic explorer/ publication
    packet.py       the deterministic AI-ready handicap packet and the research-tray resolver
    protocols/      the versioned Edge Finder handicap protocol (core + per-sport extensions)
"""

from __future__ import annotations

SCHEMA_VERSION = "edge_finder.app.v1"
CONTRACT_VERSION = "1.1.1"

#: The canonical sport vocabulary. Nothing else may reach the UI.
SPORTS = ("MLB", "CFB", "NFL", "NBA", "NHL", "SOCCER", "TENNIS")

#: Every app-facing file kind, and the schema that validates it.
KINDS = (
    "manifest", "events", "markets", "model_prices", "recommendations", "theses", "wagers",
    "settlements", "runs", "health", "board", "event_detail", "performance",
    "router_health", "recent_deliveries", "sports_registry",
    # research graph (contract 1.1.0, additive): see research.py / packet.py
    "explorer_index", "capability_manifest", "metric_registry", "entity_profile", "event_research",
    "ranking", "time_series", "market_history", "search_index",
    "handicap_packet", "handicap_protocol", "research_tray",
)

#: The research-graph capability vocabulary. A sport answers each with VERIFIED / PARTIAL / RESEARCH /
#: UNAVAILABLE in its capability manifest; the UI never offers a capability the manifest does not grant.
CAPABILITIES = (
    "team_profiles", "player_profiles", "event_research", "team_metrics", "player_metrics",
    "team_game_logs", "player_game_logs", "historical_results", "opponents", "opponent_adjustment",
    "schedule_strength", "recent_form_windows", "usage", "lineups", "injuries", "matchup_metrics",
    "projection_distributions", "raw_projections", "market_prices", "market_price_history",
    "advanced_stats", "situational_splits", "player_props", "team_props", "game_markets",
    "play_by_play", "weather", "venue_effects", "calibration", "historical_accuracy", "clv",
    "wager_history", "rankings", "time_series", "comparisons", "search",
)

#: Research quality statuses, in confidence order.
QUALITY_STATUSES = ("VERIFIED", "PARTIAL", "RESEARCH", "UNAVAILABLE", "UNKNOWN")

__all__ = ["SCHEMA_VERSION", "CONTRACT_VERSION", "SPORTS", "KINDS", "CAPABILITIES", "QUALITY_STATUSES"]
