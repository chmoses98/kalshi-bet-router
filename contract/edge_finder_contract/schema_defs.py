"""The contract schemas, as Python so they can share definitions. ``python -m
edge_finder_contract.schema_defs`` regenerates ``schemas/*.schema.json``; a test pins that the
committed files equal the generator's output, so the JSON on disk is never edited by hand and
never drifts from the vocabulary below.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import CAPABILITIES, KINDS, QUALITY_STATUSES, SCHEMA_VERSION, SPORTS

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"

# ------------------------------------------------------------------ primitives

def s(desc: str = "", **kw) -> dict:
    out = {"type": "string"}
    if desc:
        out["description"] = desc
    out.update(kw)
    return out


def ns(desc: str = "", **kw) -> dict:
    out = {"type": ["string", "null"]}
    if desc:
        out["description"] = desc
    out.update(kw)
    return out


def num(desc: str = "", **kw) -> dict:
    out = {"type": "number"}
    if desc:
        out["description"] = desc
    out.update(kw)
    return out


def nnum(desc: str = "", **kw) -> dict:
    out = {"type": ["number", "null"]}
    if desc:
        out["description"] = desc
    out.update(kw)
    return out


def nint(desc: str = "") -> dict:
    return {"type": ["integer", "null"], "description": desc}


def integer(desc: str = "", **kw) -> dict:
    out = {"type": "integer"}
    if desc:
        out["description"] = desc
    out.update(kw)
    return out


def boolean(desc: str = "") -> dict:
    return {"type": "boolean", "description": desc}


def enum(values, desc: str = "", nullable: bool = False) -> dict:
    out = {"enum": list(values) + ([None] if nullable else [])}
    out["type"] = ["string", "null"] if nullable else "string"
    if desc:
        out["description"] = desc
    return out


def ts(desc: str = "") -> dict:
    return {"type": "string", "format": "date-time", "description": desc or "ISO-8601 UTC, Z suffix"}


def nts(desc: str = "") -> dict:
    return {"type": ["string", "null"], "format": "date-time", "description": desc or "ISO-8601 UTC, Z suffix, or null"}


def prob(desc: str = "") -> dict:
    return {"type": "number", "minimum": 0, "maximum": 1, "description": desc}


def nprob(desc: str = "") -> dict:
    return {"type": ["number", "null"], "minimum": 0, "maximum": 1, "description": desc}


def arr(items: dict, desc: str = "") -> dict:
    out = {"type": "array", "items": items}
    if desc:
        out["description"] = desc
    return out


def strings(desc: str = "") -> dict:
    return arr({"type": "string"}, desc)


def obj(properties: dict, required=None, *, extra: bool = False, desc: str = "") -> dict:
    out = {"type": "object", "properties": properties,
           "required": sorted(required) if required is not None else sorted(properties),
           "additionalProperties": extra}
    if desc:
        out["description"] = desc
    return out


def free_object(desc: str = "") -> dict:
    return {"type": "object", "description": desc, "additionalProperties": True}


def ref(name: str) -> dict:
    return {"$ref": f"#/$defs/{name}"}


SPORT = enum(SPORTS, "canonical sport")
ID = {"type": "string", "pattern": r"^[a-z]{2,4}_[A-Za-z0-9._-]{4,}$", "description": "a contract id"}
NID = {"type": ["string", "null"], "pattern": r"^[a-z]{2,4}_[A-Za-z0-9._-]{4,}$"}
SOURCE_IDS = {"type": "object", "additionalProperties": {"type": ["string", "integer", "null"]},
              "description": "provider id namespace -> provider id (kalshi, espn, nflverse, mlb_game_pk ...)"}
EXTENSIONS = {"type": "object", "additionalProperties": True,
              "description": "sport-specific detail the common fields do not carry; never required by the UI"}
SELECTION = enum(["YES", "NO"], "the Kalshi contract side")
FRESHNESS = enum(["FRESH", "AGING", "STALE", "UNKNOWN"])
AUTHORITY = enum(["RESEARCH_ONLY", "SHADOW", "LIMITED", "TRUSTED", "MANUAL", "ASSISTED"],
                 "who may act on this: a model promotion level, or MANUAL/ASSISTED when a person decides")
OVERALL = enum(["HEALTHY", "DEGRADED", "STALE", "UNAVAILABLE", "RESEARCH_ONLY"])
COMPONENT_STATUS = enum(["OK", "DEGRADED", "STALE", "UNAVAILABLE", "NOT_APPLICABLE", "UNKNOWN"])

# ------------------------------------------------------------------ objects

PARTICIPANT = obj({
    "participant_id": ID,
    "display_name": s(),
    "short_name": ns("abbreviation or surname"),
    "participant_type": enum(["TEAM", "PLAYER", "PAIR"]),
    "source_ids": SOURCE_IDS,
    "metadata": free_object("conference, country, ranking, seed, jersey ... never required"),
})

EVENT = obj({
    "event_id": ID,
    "sport": SPORT,
    "league": ns("display league / tour, e.g. NFL, Premier League, ATP"),
    "season": ns("season label as the source states it, e.g. 2026, 2026-27"),
    "competition": ns("competition / tournament / week label where distinct from league"),
    "home_participant": NID,
    "away_participant": NID,
    "participants": arr(PARTICIPANT, "every participant; the durable abstraction (tennis has no home/away)"),
    "start_time_utc": ts("scheduled start, UTC"),
    "start_time_local": ns("ISO-8601 with the venue's offset when the source knows it"),
    "start_time_source": ns("which provider supplied the start time"),
    "start_time_confidence": enum(["SCHEDULED", "VERIFIED", "ESTIMATED", "PLACEHOLDER", "UNKNOWN"],
                                  "PLACEHOLDER = a Kalshi nominal time, not a schedule", nullable=True),
    "effective_start_time_utc": nts("the best current estimate of the actual start, when it differs"),
    "status": enum(["SCHEDULED", "LIVE", "FINAL", "POSTPONED", "CANCELLED", "UNKNOWN"]),
    "venue": ns(),
    "broadcast": ns(),
    "source_ids": SOURCE_IDS,
    "schedule_updated_at": nts("when the schedule source was last read"),
    "last_updated_at": ts("when this record was built"),
    "extensions": EXTENSIONS,
})

MARKET = obj({
    "market_id": ID,
    "kalshi_ticker": s(),
    "kalshi_event_ticker": ns(),
    "kalshi_series_ticker": ns(),
    "event_id": NID,
    "sport": SPORT,
    "market_family": s("the source repository's family label, normalised to lower snake_case"),
    "market_type": ns("finer type where the family is broad"),
    "period": ns("FULL_GAME, FIRST_HALF, F5, SET_1 ... as the source labels it"),
    "participant_id": NID,
    "player_id": NID,
    "side": enum(["HOME", "AWAY", "DRAW", "OVER", "UNDER", "PARTICIPANT", "TIE", "OTHER"],
                 "what YES refers to, when the family has sides", nullable=True),
    "line": nnum("spread / total / handicap line"),
    "threshold": nnum("prop threshold (YES iff stat >= / > threshold as yes_description states)"),
    "yes_description": s("what YES means, in words"),
    "no_description": ns(),
    "market_probability": nprob("mid of the YES book in [0,1]; null when the book is empty"),
    "yes_bid": nprob(), "yes_ask": nprob(), "no_bid": nprob(), "no_ask": nprob(),
    "last_price": nprob(),
    "volume": nnum(), "open_interest": nnum(),
    "market_status": enum(["OPEN", "CLOSED", "SETTLED", "UNOPENED", "UNKNOWN"]),
    "close_time_utc": nts(),
    "captured_at": nts("when the quote was observed; null only for a stub of a market no longer on the board"),
    "source": s("which capture produced the quote"),
    "raw_market_reference": ns("where the raw record lives in the source repository"),
    "extensions": EXTENSIONS,
})

MODEL_PRICE = obj({
    "model_price_id": ID,
    "event_id": NID,
    "market_id": ID,
    "run_id": ID,
    "model_version": ns(),
    "fair_probability": prob("P(YES) in [0,1]"),
    "lower_bound": nprob(), "upper_bound": nprob(),
    "uncertainty": nnum("standard error or interval half-width when the model computes one; never invented"),
    "market_probability": nprob("the market probability the model was compared against"),
    "edge": nnum("fair_probability - market_probability on the YES side, when both exist"),
    "projection_value": nnum(), "projection_unit": ns(),
    "generated_at": ts(),
    "inputs_as_of": nts("the newest input the model saw"),
    "freshness_status": FRESHNESS,
    "data_quality_status": enum(["OK", "DEGRADED", "CANNOT_TRUST_INPUTS", "UNSUPPORTED", "UNKNOWN"]),
    "support_status": ns("the source repository's support / gate label, verbatim"),
    "extensions": EXTENSIONS,
})

THESIS = obj({
    "thesis_id": ID,
    "event_id": ID,
    "run_id": ID,
    "summary": ns("null when the repository produces no prose; never LLM-fabricated"),
    "primary_game_script": ns(),
    "supporting_factors": strings(), "opposing_factors": strings(), "key_dependencies": strings(),
    "context_notes": obj({"injuries": ns(), "lineups": ns(), "weather": ns(), "usage": ns(), "other": ns()}),
    "confidence_label": ns(),
    "evidence": free_object("structured model evidence when no prose exists"),
    "generated_at": ts(),
})

RECOMMENDATION = obj({
    "recommendation_id": ID,
    "event_id": ID,
    "market_id": ID,
    "sport": SPORT,
    "run_id": ID,
    "selection": SELECTION,
    "market_description": s(),
    "current_probability": nprob("market probability when generated"),
    "current_price": nprob("executable price of the selection when generated"),
    "fair_probability": nprob("P(selection) per the model"),
    "edge": nnum(),
    "bet_up_to_probability": nprob(), "bet_up_to_price": nprob(),
    "confidence": ns(),
    "stake_units": nnum(), "stake_dollars": nnum(), "bankroll_basis": nnum(),
    "thesis_id": NID,
    "status": enum(["RECOMMENDED", "WATCH", "PASS", "RESEARCH_CANDIDATE", "EXPIRED", "NOT_PLAYABLE"]),
    "reason_not_playable": ns(),
    "created_at": ts(),
    "expires_at": nts(),
    "data_freshness": FRESHNESS,
    "lineup_status": ns(), "injury_flags": strings(),
    "research_only": boolean("true unless a promoted model or a human decision backs it"),
    "authority": AUTHORITY,
    "source_repo": s(),
    "source_ids": SOURCE_IDS,
    "extensions": EXTENSIONS,
})

LINKAGE = obj({
    "wager_placed_at": nts(), "model_inputs_as_of": nts(), "market_captured_at": nts(),
    "recommendation_generated_at": nts(),
}, desc="temporal proof of the link: every linked record predates the wager")

WAGER = obj({
    "wager_id": ID,
    "source_bet_key": ns("the router's deterministic order identity"),
    "kalshi_order_id": ns(), "kalshi_fill_ids": strings(),
    "event_id": NID,
    "market_id": ID,
    "kalshi_ticker": s(),
    "sport": SPORT,
    "selection": SELECTION,
    "side": enum(["BUY", "SELL"], nullable=True),
    "contracts": num(minimum=0),
    "stake": num("cash at risk including fees, dollars", minimum=0),
    "average_price": prob("quantity-weighted fill price, dollars per contract"),
    "fees": nnum("exchange fees, dollars; null when unknown"),
    "placed_at": ts(),
    "source": enum(["KALSHI_ROUTER", "MANUAL", "LEGACY_IMPORT", "OTHER"]),
    "router_ingested_at": nts(),
    "destination_repo": s(),
    "model_run_id": NID, "model_price_id": NID, "recommendation_id": NID,
    "linkage": LINKAGE,
    "settlement_status": enum(["PENDING", "SETTLED", "VOID", "UNKNOWN"]),
    "settlement_id": NID,
    "payout": nnum(), "profit_loss": nnum(),
    "source_ids": SOURCE_IDS,
    "extensions": EXTENSIONS,
})

SETTLEMENT = obj({
    "settlement_id": ID,
    "wager_id": ID,
    "market_id": ID,
    "result": enum(["WON", "LOST", "PUSH", "VOID", "SCALAR", "UNKNOWN"]),
    "winning_side": enum(["YES", "NO"], nullable=True),
    "settlement_value": nprob("dollars per YES contract paid by the exchange, when known"),
    "settled_at": ts(),
    "gross_payout": nnum(), "fees": nnum(), "net_pnl": nnum(),
    "source": s(),
    "source_ids": SOURCE_IDS,
    "verification_status": enum(["EXCHANGE_CONFIRMED", "MODEL_DERIVED", "UNVERIFIED", "REFUSED"]),
    "refusals": strings("why money fields are absent"),
    "extensions": EXTENSIONS,
})

RUN = obj({
    "run_id": ID,
    "sport": SPORT,
    "repo": s(),
    "commit_sha": ns(),
    "workflow_run_id": ns(),
    "model_version": ns(),
    "started_at": nts(), "completed_at": ts(),
    "status": enum(["SUCCESS", "PARTIAL", "FAILED"]),
    "scope": s("what this run covered, e.g. slate 2026-10-02"),
    "events_requested": nint(), "events_processed": integer(), "markets_discovered": integer(),
    "markets_priced": integer(), "recommendations_created": integer(),
    "data_sources": strings(),
    "input_freshness": {"type": "object", "additionalProperties": {"type": ["string", "null"], "format": "date-time"}},
    "warnings": strings(), "errors": strings(),
    "source_ids": SOURCE_IDS,
})


def envelope(kind: str, item_schema: dict | None, extra_props: dict | None = None,
             *, sport_required: bool = True) -> dict:
    props = {
        "schema_version": {"const": SCHEMA_VERSION},
        "kind": {"const": kind},
        "sport": SPORT if sport_required else enum(list(SPORTS) + ["ALL"]),
        "run_id": ID,
        "generated_at": ts(),
    }
    if item_schema is not None:
        props["count"] = integer(minimum=0)
        props["items"] = arr(item_schema)
    if extra_props:
        props.update(extra_props)
    return obj(props)


def with_defs(schema: dict, **defs: dict) -> dict:
    schema = dict(schema)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://edge-finder.app/schemas/{SCHEMA_VERSION}/{schema.get('title', 'x')}"
    if defs:
        schema["$defs"] = defs
    return schema


FILE_ENTRY = obj({
    "path": s("relative to the app root"), "kind": enum(KINDS), "sha256": s(), "bytes": integer(minimum=0),
    "count": nint("items in the file, when it is a collection"),
})

MANIFEST = envelope("manifest", None, {
    "commit_sha": ns(),
    "model_version": ns(),
    "status": enum(["SUCCESS", "PARTIAL"]),
    "source_repo": s(), "source_branch": s(),
    "files": {"type": "object", "additionalProperties": FILE_ENTRY},
    "counts": {"type": "object", "additionalProperties": integer(minimum=0)},
    "freshness": {"type": "object", "additionalProperties": obj({"as_of": nts(), "status": FRESHNESS})},
    "warnings": strings(),
})

COMPONENT = obj({"status": COMPONENT_STATUS, "as_of": nts(), "age_seconds": nnum(), "detail": ns()})

HEALTH = envelope("health", None, {
    "overall_status": OVERALL,
    "model_status": COMPONENT_STATUS,
    "market_data_status": COMPONENT_STATUS,
    "router_status": COMPONENT_STATUS,
    "settlement_status": COMPONENT_STATUS,
    "freshness_status": FRESHNESS,
    "bet_authority": AUTHORITY,
    "last_successful_run": nts(),
    "last_export_attempt": ts(),
    "payload_run_id": NID,
    "last_market_capture": nts(),
    "last_model_generated": nts(),
    "next_scheduled_run": nts(),
    "data_age_seconds": nnum(),
    "thresholds": {"type": "object", "additionalProperties": obj({"fresh_after_seconds": integer(), "stale_after_seconds": integer()})},
    "components": {"type": "object", "additionalProperties": COMPONENT},
    "warnings": strings(), "errors": strings(),
    "commit_sha": ns(),
})

COMPACT_PARTICIPANT = obj({"participant_id": ID, "display_name": s(), "short_name": ns(),
                           "participant_type": enum(["TEAM", "PLAYER", "PAIR"])})
COMPACT_REC = obj({"recommendation_id": ID, "market_id": ID, "selection": SELECTION, "market_description": s(),
                   "fair_probability": nprob(), "current_price": nprob(), "edge": nnum(), "status": s(),
                   "authority": AUTHORITY, "research_only": boolean()})
BOARD_EVENT = obj({
    "event_id": ID, "league": ns(), "competition": ns(), "start_time_utc": ts(), "status": s(),
    "home_participant": NID, "away_participant": NID, "participants": arr(COMPACT_PARTICIPANT),
    "data_freshness": FRESHNESS, "market_captured_at": nts(), "model_generated_at": nts(),
    "markets_available": integer(minimum=0), "markets_priced": integer(minimum=0),
    "recommendations_count": integer(minimum=0),
    "top_recommendations": arr(COMPACT_REC), "wagers_count": integer(minimum=0),
    "health_flags": strings(), "detail_path": s("app-root-relative path of the event_detail file"),
})
BOARD = envelope("board", BOARD_EVENT, {"overall_status": OVERALL, "bet_authority": AUTHORITY})

EVENT_DETAIL = envelope("event_detail", None, {
    "event": EVENT,
    "markets": arr(MARKET), "model_prices": arr(MODEL_PRICE), "recommendations": arr(RECOMMENDATION),
    "theses": arr(THESIS), "wagers": arr(WAGER), "settlements": arr(SETTLEMENT),
    "context": free_object("injuries / lineups / weather / usage, sport-specific"),
    "price_history": arr(obj({"market_id": ID, "captured_at": ts(), "yes_bid": nprob(), "yes_ask": nprob(), "last_price": nprob()})),
    "data_freshness": FRESHNESS,
})

TOTALS = obj({
    "wagers": integer(minimum=0), "settled": integer(minimum=0), "pending": integer(minimum=0),
    "won": integer(minimum=0), "lost": integer(minimum=0), "push": integer(minimum=0),
    "void": integer(minimum=0), "scalar": integer(minimum=0), "unknown": integer(minimum=0),
    "stake": nnum(), "gross_payout": nnum(), "fees": nnum(), "net_pnl": nnum(), "roi": nnum(),
    "open_exposure": nnum(),
    "settled_with_economics": integer("settlements whose money fields are established", minimum=0),
})
PERFORMANCE = envelope("performance", None, {
    "as_of": ts(),
    "totals": TOTALS,
    "by_market_family": {"type": "object", "additionalProperties": TOTALS},
    "by_month": {"type": "object", "additionalProperties": TOTALS},
    "by_source": {"type": "object", "additionalProperties": TOTALS},
    "recent_results": arr(obj({"wager_id": ID, "settlement_id": ID, "market_id": ID, "kalshi_ticker": s(),
                               "selection": SELECTION, "result": s(), "net_pnl": nnum(), "settled_at": ts()})),
    "pending_wagers": arr(obj({"wager_id": ID, "market_id": ID, "kalshi_ticker": s(), "selection": SELECTION,
                               "stake": num(), "placed_at": ts()})),
    "recommended_vs_wagered": obj({"recommendations": integer(minimum=0), "wagers_linked_to_recommendation": integer(minimum=0),
                                   "wagers_unlinked": integer(minimum=0)}),
    "clv": obj({"available": boolean(), "wagers_with_clv": integer(minimum=0), "mean_clv": nnum()}),
    "bankroll": obj({"available": boolean(), "basis": ns(), "history": arr(obj({"as_of": ts(), "balance": num()}))}),
    "data_completeness": {"type": "object", "additionalProperties": {"type": ["integer", "number", "string", "boolean", "null"]}},
    "notes": strings(),
})

SPORT_ROUTE = obj({
    "routable": boolean("the router has a classification AND a destination profile for this sport"),
    "classification": enum(["SUPPORTED", "UNSUPPORTED"]),
    "destination_repo": ns(), "ledger_branch": ns(), "auto_merge": {"type": ["boolean", "null"]},
    "eligible": nint("eligible wagers in the last delivery run"), "delivered": nint(), "failed": nint(),
    "status": enum(["DELIVERED", "NO_OP", "FAILED", "REFUSED", "NOT_ROUTABLE", "DRY_RUN", "UNKNOWN"]),
    "last_error_type": ns(),
})
RUN_REF = obj({"run_id": ns(), "url": ns(), "started_at": nts(), "concluded_at": nts(),
               "conclusion": ns(), "health_state": ns()})
ROUTER_HEALTH = envelope("router_health", None, {
    "overall_status": OVERALL,
    "router_health_state": enum(["healthy_no_op", "delivered", "not_routable", "deferred", "blocked", "unknown"]),
    "last_poll_at": nts(), "poll_age_seconds": nnum(),
    "last_delivery_run": RUN_REF, "last_settlement_run": RUN_REF,
    "bets_discovered": nint(), "delivered": nint(), "failed": nint(), "blocked": nint(), "deferred": nint(),
    "by_sport": {"type": "object", "additionalProperties": SPORT_ROUTE},
    "thresholds": {"type": "object", "additionalProperties": obj({"fresh_after_seconds": integer(), "stale_after_seconds": integer()})},
    "warnings": strings(), "errors": strings(), "commit_sha": ns(),
}, sport_required=False)

DELIVERY = obj({
    "run_id": s(), "run_url": ns(), "started_at": nts(), "sport": SPORT, "destination": ns(),
    "status": enum(["DELIVERED", "MERGED", "FAILED", "REFUSED", "DRY_RUN", "NO_OP", "SETTLED", "PARTIAL"]),
    "rows": nint(), "attempt": nint(), "error_type": ns(), "error_message": ns("never a wager, price or ticker"),
    "wager_ids": strings("opaque source keys of refused rows, when the receipt names them"),
    "first_failed_at": nts(), "last_attempt_at": nts(),
    "retry_status": enum(["WILL_RETRY", "NEEDS_ATTENTION", "RESOLVED", "NOT_APPLICABLE"], nullable=True),
})
RECENT_DELIVERIES = envelope("recent_deliveries", DELIVERY, sport_required=False)

SPORT_LOCATION = obj({
    "repo": s(), "branch": s(), "app_root": s("path of the app directory in that branch"),
    "raw_base_url": s("https://raw.githubusercontent.com/<repo>/<branch>/<app_root>"),
    "status": enum(["ACTIVE", "PLANNED"]),
    "notes": strings(),
})
SPORTS_REGISTRY = envelope("sports_registry", None, {
    "sports": {"type": "object", "additionalProperties": SPORT_LOCATION},
    "router": SPORT_LOCATION,
}, sport_required=False)


# ------------------------------------------------------------------ research graph (contract 1.1.0, additive)
# Everything below is a NEW kind. No existing object above changes. The vocabulary mirrors the app
# contract: the same ids (prt_/evt_/mkt_kalshi_), the same FRESHNESS states, the same UTC rule.

QUALITY_STATUS = enum(QUALITY_STATUSES, "VERIFIED = production-generated on a cadence, tested, history present; "
                      "PARTIAL = real but limited; RESEARCH = non-production or experimental; "
                      "UNAVAILABLE = nothing supports it; UNKNOWN = not established")
RESEARCH_ENTITY_TYPE = enum(["TEAM", "PLAYER", "EVENT", "MARKET", "MATCHUP"])
WINDOW_KIND = enum(["SEASON", "LAST_N", "DATE_RANGE", "GAME", "RUN", "CUSTOM"])
X_AXIS = enum(["GAME", "WEEK", "DATE", "RUN", "CAPTURE"])
STAT_TYPE = enum(["RATE", "COUNT", "PERCENT", "PROBABILITY", "RATING", "INDEX", "DURATION", "CURRENCY", "SCORE", "OTHER"])
HOME_AWAY = enum(["HOME", "AWAY", "NEUTRAL"], nullable=True)
LINK_REL = enum(["TEAM", "PLAYER", "EVENT", "OPPONENT", "METRIC", "RANKING", "SERIES", "MARKET", "MARKET_HISTORY",
                 "EVENT_RESEARCH", "CAPABILITIES", "SEARCH", "INDEX"])
METRIC_ID = {"type": "string", "pattern": r"^met_[a-z]+\.[a-z0-9_]+$", "description": "met_<sport>.<slug>"}
NMETRIC_ID = {"type": ["string", "null"], "pattern": r"^met_[a-z]+\.[a-z0-9_]+$"}
DATE = {"type": "string", "format": "date", "description": "YYYY-MM-DD"}
NDATE = {"type": ["string", "null"], "format": "date"}

QUALITY = obj({
    "status": QUALITY_STATUS,
    "source": s("the producing dataset or provider, e.g. nflverse pbp, kalshi captures, internal simulation"),
    "source_version": ns(), "methodology_version": ns(),
    "production": boolean("produced by a scheduled production path, not a notebook or one-off"),
    "generated_at": ts(), "data_as_of": nts("the newest underlying observation"),
    "coverage": ns("human-readable span, e.g. 2026 weeks 1-4"),
    "sample_size": nint(), "missingness": nnum("fraction of expected observations that are absent, 0..1"),
    "limitations": strings("known caveats, verbatim from the audit; never empty for PARTIAL/RESEARCH"),
}, desc="where a research object came from and how far to trust it")

LINK = obj({
    "rel": LINK_REL, "target_kind": enum(KINDS), "target_id": ns("the contract id of the target, when it has one"),
    "label": s(), "path": ns("app-root-relative path of the target document; null only when the target is embedded"),
}, desc="a navigable edge; every path must exist in explorer/index.json")

WINDOW = obj({
    "kind": WINDOW_KIND, "n": nint("LAST_N size"), "start": nts(), "end": nts(),
    "label": s("SEASON, L3, L5, 2026-W04, run 2026-10-02T14:40Z ..."),
})
SPLIT = obj({"dimension": s("home_away, handedness, strength_state, surface ..."), "value": s()})
NSPLIT = {"anyOf": [SPLIT, {"type": "null"}]}

OBS_CONTEXT = obj({
    "rank": nint("1 = best per higher_is_better"), "universe_size": nint(), "percentile": nnum("0..100, 100 = best"),
    "ranking_id": NID, "universe_label": ns(),
    "league_average": nnum(), "league_median": nnum(), "best_value": nnum(), "worst_value": nnum(),
    "best_entity_id": NID, "worst_entity_id": NID, "higher_is_better": {"type": ["boolean", "null"]},
}, desc="the comparison context that makes a number meaningful; never a bare percentile")

OBSERVATION = obj({
    "observation_id": ID, "metric_id": METRIC_ID, "sport": SPORT,
    "entity_id": ID, "entity_type": RESEARCH_ENTITY_TYPE,
    "value": nnum("the raw value; null when the source has none for this window"),
    "adjusted_value": nnum("opponent/schedule-adjusted value when the source computes one; never invented"),
    "display_value": ns(), "unit": ns(),
    "window": WINDOW, "split": NSPLIT,
    "sample_size": nint("games, plays, PA, minutes ... in the metric's sample unit"),
    "as_of": ts(), "season": ns(), "event_id": NID, "opponent_id": NID,
    "context": {"anyOf": [OBS_CONTEXT, {"type": "null"}]},
    "source": s(), "quality_status": QUALITY_STATUS,
    "extensions": EXTENSIONS,
})

SUPPORTS = obj({
    "rank": boolean(), "percentile": boolean(), "time_series": boolean(), "windows": boolean(), "splits": boolean(),
    "opponent_adjustment": boolean(), "schedule_adjustment": boolean(), "home_away": boolean(), "game_state": boolean(),
})
METRIC = obj({
    "metric_id": METRIC_ID, "sport": SPORT, "name": s(), "short_name": s(), "description": s(),
    "entity_type": RESEARCH_ENTITY_TYPE, "category": s(), "subcategory": ns(), "unit": ns(),
    "stat_type": STAT_TYPE, "higher_is_better": {"type": ["boolean", "null"]},
    "comparison_universe": ns("e.g. NFL teams, 2026 season"),
    "supports": SUPPORTS, "windows": strings("window labels this metric is published for"),
    "splits": strings("split dimensions this metric is published for"),
    "source": s(), "source_version": ns(), "methodology_version": ns(),
    "quality": QUALITY, "historical_start": NDATE, "update_frequency": ns("per capture, daily, weekly, per run ..."),
    "freshness": FRESHNESS, "known_limitations": strings(), "related_metrics": arr(METRIC_ID),
    "extensions": EXTENSIONS,
})
METRIC_REGISTRY = envelope("metric_registry", METRIC)

RANKING_ENTRY = obj({
    "rank": integer(minimum=1), "entity_id": ID, "display_name": s(), "short_name": ns(),
    "value": nnum(), "adjusted_value": nnum(), "sample_size": nint(), "percentile": nnum(), "path": ns(),
})
RANKING = envelope("ranking", None, {
    "ranking_id": ID, "metric_id": METRIC_ID,
    "universe": obj({"label": s(), "entity_type": RESEARCH_ENTITY_TYPE, "season": ns(), "size": integer(minimum=0), "filter": ns()}),
    "window": WINDOW, "split": NSPLIT, "as_of": ts(), "higher_is_better": {"type": ["boolean", "null"]},
    "summary": obj({"mean": nnum(), "median": nnum(), "min": nnum(), "max": nnum(), "stdev": nnum(),
                    "best_entity_id": NID, "worst_entity_id": NID, "sample_size_min": nint(), "sample_size_max": nint()}),
    "entries": arr(RANKING_ENTRY, "sorted by rank ascending; rank 1 is best per higher_is_better"),
    "quality": QUALITY, "links": arr(LINK),
})

POINT = obj({
    "x": s("the x label: game id, week, date, run or capture label"), "t": ts(),
    "event_id": NID, "opponent_id": NID, "value": nnum(), "adjusted_value": nnum(), "rolling_value": nnum(),
    "sample_size": nint(), "run_id": NID, "source": ns(), "quality_status": QUALITY_STATUS, "path": ns(),
})
TIME_SERIES = envelope("time_series", None, {
    "series_id": ID, "metric_id": METRIC_ID, "entity_id": ID, "entity_type": RESEARCH_ENTITY_TYPE,
    "x_axis": X_AXIS, "split": NSPLIT, "unit": ns(), "as_of": ts(), "rolling_window": nint(),
    "points": arr(POINT, "sorted by t ascending, then x"),
    "quality": QUALITY, "links": arr(LINK),
})

GAME_REF = obj({
    "event_id": ID, "start_time_utc": ts(), "opponent_id": NID, "opponent_name": ns(), "home_away": HOME_AWAY,
    "status": s(), "result": {"anyOf": [obj({"for": nnum(), "against": nnum(), "outcome": enum(["W", "L", "T"], nullable=True)}), {"type": "null"}]},
    "competition": ns(), "path": ns(),
})
PLAYER_REF = obj({"participant_id": ID, "display_name": s(), "role": ns("position / lineup slot / line"), "path": ns()})
TEAM_REF = obj({"participant_id": ID, "display_name": s(), "short_name": ns(), "path": ns()})
OPPONENT_REF = obj({"participant_id": ID, "display_name": s(), "event_ids": arr(ID), "path": ns()})
MARKET_REF = obj({
    "market_id": ID, "kalshi_ticker": s(), "event_id": NID, "market_family": s(), "yes_description": s(),
    "market_probability": nprob(), "yes_bid": nprob(), "yes_ask": nprob(), "captured_at": nts(),
    "participant_id": NID, "player_id": NID, "period": ns(), "line": nnum(), "threshold": nnum(),
})
PROJECTION_REF = obj({
    "model_price_id": NID, "market_id": NID, "event_id": NID, "metric_id": NMETRIC_ID,
    "fair_probability": nprob(), "market_probability": nprob(), "edge": nnum(),
    "projection_value": nnum(), "projection_unit": ns(), "lower_bound": nnum(), "upper_bound": nnum(),
    "generated_at": ts(), "run_id": NID, "model_version": ns(), "research_only": boolean(),
    "authority": AUTHORITY, "quality_status": QUALITY_STATUS,
})
SERIES_REF = obj({"series_id": ID, "metric_id": METRIC_ID, "x_axis": X_AXIS, "split": NSPLIT, "path": s()})
RANKING_REF = obj({"ranking_id": ID, "metric_id": METRIC_ID, "window_label": s(), "split": NSPLIT, "path": s()})
AVAILABILITY = obj({"status": s("ACTIVE, OUT, QUESTIONABLE, IR, PROBABLE, DAY_TO_DAY, UNKNOWN ..."), "detail": ns(),
                    "as_of": nts(), "source": s(), "event_id": NID})

ENTITY_PROFILE = envelope("entity_profile", None, {
    "entity": PARTICIPANT, "entity_type": enum(["TEAM", "PLAYER"]), "season": ns(), "league": ns(),
    "team": {"anyOf": [TEAM_REF, {"type": "null"}]},
    "metrics": arr(OBSERVATION, "current values with comparison context"),
    "splits": {"type": "object", "additionalProperties": arr(OBSERVATION)},
    "series": arr(SERIES_REF), "rankings": arr(RANKING_REF),
    "games": arr(GAME_REF), "players": arr(PLAYER_REF), "opponents": arr(OPPONENT_REF),
    "markets": arr(MARKET_REF), "projections": arr(PROJECTION_REF), "availability": arr(AVAILABILITY),
    "links": arr(LINK), "quality": QUALITY, "extensions": EXTENSIONS,
})

MATCHUP_ROW = obj({"metric_id": METRIC_ID, "name": s(),
                   "home": {"anyOf": [OBSERVATION, {"type": "null"}]}, "away": {"anyOf": [OBSERVATION, {"type": "null"}]},
                   "note": ns()})
DISTRIBUTION = obj({
    "market_id": NID, "metric_id": NMETRIC_ID, "entity_id": NID, "label": s(),
    "quantiles": {"type": "object", "additionalProperties": num(), "description": "p05, p25, p50, p75, p95 ... as the source stores them"},
    "mean": nnum(), "stdev": nnum(), "samples": nint(), "run_id": NID, "generated_at": ts(),
    "source": s(), "quality_status": QUALITY_STATUS,
})
EVENT_RESEARCH = envelope("event_research", None, {
    "event": EVENT,
    "participants": arr(obj({"participant_id": ID, "display_name": s(), "home_away": HOME_AWAY, "path": ns()})),
    "matchup": arr(MATCHUP_ROW), "players": arr(obj({"participant_id": ID, "display_name": s(), "team_id": NID, "role": ns(), "path": ns()})),
    "projections": arr(PROJECTION_REF), "distributions": arr(DISTRIBUTION),
    "markets": arr(MARKET_REF), "market_history_path": ns(),
    "context": obj({"injuries": arr(AVAILABILITY), "lineups": arr(free_object()), "weather": {"anyOf": [free_object(), {"type": "null"}]},
                    "venue": {"anyOf": [free_object(), {"type": "null"}]}, "notes": strings()}),
    "wagers": arr(ID), "links": arr(LINK), "quality": QUALITY, "extensions": EXTENSIONS,
})

PRICE_POINT = obj({"captured_at": ts(), "yes_bid": nprob(), "yes_ask": nprob(), "last_price": nprob(),
                   "volume": nnum(), "open_interest": nnum(), "source": ns()})
MARKET_HISTORY = envelope("market_history", None, {
    "event_id": ID, "as_of": ts(),
    "series": arr(obj({"market_id": ID, "kalshi_ticker": s(), "points": arr(PRICE_POINT, "sorted by captured_at")})),
    "quality": QUALITY, "links": arr(LINK),
})

CAPABILITY = obj({
    "capability": enum(CAPABILITIES), "status": QUALITY_STATUS, "entity_types": arr(RESEARCH_ENTITY_TYPE),
    "summary": s(), "reasons": strings("why this status, from the audit"), "limitations": strings(),
    "evidence": strings("app-root-relative paths that prove the capability; required for VERIFIED/PARTIAL"),
    "coverage": ns(), "since": NDATE, "metrics": arr(METRIC_ID), "windows": strings(), "splits": strings(),
})
CAPABILITY_MANIFEST = envelope("capability_manifest", CAPABILITY, {
    "split_dimensions": arr(obj({"dimension": s(), "values": strings(), "status": QUALITY_STATUS})),
    "windows": arr(WINDOW),
    "audit_date": DATE, "notes": strings(),
})

SEARCH_ENTRY = obj({
    "id": s(), "kind": enum(["TEAM", "PLAYER", "EVENT", "METRIC", "RANKING", "SERIES"]),
    "label": s(), "secondary": ns("position, team, competition ..."), "aliases": strings(),
    "tokens": strings("lowercase, deduplicated, sorted"), "path": s(), "sport": SPORT,
    "context": obj({"team": ns(), "position": ns(), "league": ns(), "season": ns()}),
})
SEARCH_INDEX = envelope("search_index", SEARCH_ENTRY)

EXPLORER_FILE = obj({"kind": enum(KINDS), "sha256": s(), "bytes": integer(minimum=0), "entity_id": ns()})
EXPLORER_INDEX = envelope("explorer_index", None, {
    "commit_sha": ns(), "as_of": nts(), "base_manifest_run_id": NID,
    "counts": {"type": "object", "additionalProperties": integer(minimum=0)},
    "files": {"type": "object", "additionalProperties": EXPLORER_FILE, "description": "explorer-relative path -> entry"},
    "capabilities_path": s(), "metrics_path": s(), "search_index_path": s(),
    "teams": arr(TEAM_REF), "players_by_team": {"type": "object", "additionalProperties": arr(ID)},
    "events": arr(obj({"event_id": ID, "start_time_utc": ts(), "home_participant": NID, "away_participant": NID,
                       "participants": arr(ID), "status": s(), "path": s()})),
    "windows": arr(WINDOW), "quality": QUALITY, "warnings": strings(),
})

PROTOCOL_STEP = obj({"id": s(), "instruction": s()})
HANDICAP_PROTOCOL = obj({
    "schema_version": {"const": SCHEMA_VERSION}, "kind": {"const": "handicap_protocol"},
    "protocol_id": s("edge_finder.handicap.core.v1, edge_finder.handicap.nfl.v1 ..."), "version": s(),
    "extends": ns("the protocol this one extends"), "sport": enum(list(SPORTS) + ["ALL"]),
    "title": s(), "principles": strings(), "steps": arr(PROTOCOL_STEP), "outputs_required": strings(),
    "sport_notes": strings(), "evidence_weights": obj({"VERIFIED": s(), "PARTIAL": s(), "RESEARCH": s(), "UNKNOWN": s()}),
    "forbidden": strings(),
})

TRAY_ITEM = obj({
    "item_id": s(), "ref_kind": enum(["TEAM", "PLAYER", "EVENT", "METRIC", "RANKING", "SERIES", "CHART_POINT", "MARKET", "PROJECTION"]),
    "sport": SPORT, "id": s("the contract id of the referenced object"),
    "extra": {"anyOf": [obj({"series_id": ns(), "x": ns(), "metric_id": NMETRIC_ID, "market_id": NID, "event_id": NID}), {"type": "null"}]},
    "note": ns(), "added_at": ts(),
})
RESEARCH_TRAY = obj({
    "schema_version": {"const": SCHEMA_VERSION}, "kind": {"const": "research_tray"},
    "tray_version": s(), "items": arr(TRAY_ITEM), "updated_at": ts(),
})

PACKET_MARKET = obj({
    "market_id": ID, "kalshi_ticker": s(), "event_id": NID, "market_family": s(), "yes_description": s(),
    "yes_bid": nprob(), "yes_ask": nprob(), "mid": nprob(), "last_price": nprob(), "captured_at": nts(),
    "freshness": FRESHNESS, "market_status": s(), "participant_id": NID, "player_id": NID,
})
PACKET_MODEL = obj({
    "market_id": ID, "fair_probability": nprob(), "market_probability": nprob(), "edge": nnum(),
    "projection_value": nnum(), "projection_unit": ns(), "model_version": ns(), "generated_at": ts(),
    "research_only": boolean(), "authority": AUTHORITY, "data_quality_status": s(), "freshness": FRESHNESS,
})
PACKET_OBS = obj({
    "metric_id": METRIC_ID, "name": s(), "value": nnum(), "adjusted_value": nnum(), "display_value": ns(), "unit": ns(),
    "window": s(), "split": ns(), "rank": nint(), "universe_size": nint(), "league_average": nnum(),
    "as_of": ts(), "quality_status": QUALITY_STATUS, "source": s(),
})
PACKET_EVIDENCE = obj({
    "entity_id": ID, "entity_type": RESEARCH_ENTITY_TYPE, "label": s(), "team": ns(), "role": ns(),
    "observations": arr(PACKET_OBS), "availability": arr(AVAILABILITY), "recent": arr(obj({
        "x": s(), "t": ts(), "metric_id": METRIC_ID, "value": nnum(), "opponent": ns(), "event_id": NID})),
})
PACKET_EVENT = obj({
    "event_id": ID, "label": s(), "start_time_utc": ts(), "status": s(), "home_participant": NID, "away_participant": NID,
    "venue": ns(), "context_notes": strings(), "theses": arr(obj({"summary": ns(), "supporting_factors": strings(), "opposing_factors": strings(), "research_only": boolean()})),
})
HANDICAP_PACKET = obj({
    "schema_version": {"const": SCHEMA_VERSION}, "kind": {"const": "handicap_packet"},
    "packet_id": ID, "packet_version": s(),
    "protocol": obj({"protocol_id": s(), "version": s(), "extends": ns(), "principles": strings(), "steps": arr(PROTOCOL_STEP),
                     "outputs_required": strings(), "forbidden": strings(), "evidence_weights": obj({"VERIFIED": s(), "PARTIAL": s(), "RESEARCH": s(), "UNKNOWN": s()})}),
    "scope": obj({"kind": enum(["GAME", "SLATE", "CUSTOM"]), "event_ids": arr(ID), "window_start": nts(), "window_end": nts(),
                  "label": s()}),
    "sports": arr(SPORT), "generated_at": ts(), "data_as_of": nts(),
    "warning": s(), "user_focus": arr(obj({"item_id": s(), "ref_kind": s(), "id": s(), "label": ns(), "resolved": boolean(), "note": ns()})),
    "events": arr(PACKET_EVENT), "evidence": arr(PACKET_EVIDENCE),
    "markets": arr(PACKET_MARKET), "model_evidence": arr(PACKET_MODEL),
    "repo_recommendations": arr(obj({"market_id": ID, "selection": SELECTION, "status": s(), "fair_probability": nprob(),
                                     "bet_up_to_price": nprob(), "authority": AUTHORITY, "research_only": boolean(), "created_at": ts()})),
    "quality": obj({"sources": strings(), "market_freshness": FRESHNESS, "model_freshness": FRESHNESS,
                    "research_only_items": strings("metric / model ids whose status is RESEARCH"), "missing": strings(),
                    "capabilities": {"type": "object", "additionalProperties": QUALITY_STATUS}}),
    "budget": obj({"max_chars": integer(minimum=0), "chars": integer(minimum=0), "truncated": strings()}),
})

COLLECTIONS = {
    "events": EVENT, "markets": MARKET, "model_prices": MODEL_PRICE, "recommendations": RECOMMENDATION,
    "theses": THESIS, "wagers": WAGER, "settlements": SETTLEMENT, "runs": RUN,
}

SINGLETONS = {
    "manifest": MANIFEST, "health": HEALTH, "board": BOARD, "event_detail": EVENT_DETAIL,
    "performance": PERFORMANCE, "router_health": ROUTER_HEALTH, "recent_deliveries": RECENT_DELIVERIES,
    "sports_registry": SPORTS_REGISTRY,
    # research graph
    "explorer_index": EXPLORER_INDEX, "capability_manifest": CAPABILITY_MANIFEST, "metric_registry": METRIC_REGISTRY,
    "entity_profile": ENTITY_PROFILE, "event_research": EVENT_RESEARCH, "ranking": RANKING,
    "time_series": TIME_SERIES, "market_history": MARKET_HISTORY, "search_index": SEARCH_INDEX,
    "handicap_packet": HANDICAP_PACKET, "handicap_protocol": HANDICAP_PROTOCOL, "research_tray": RESEARCH_TRAY,
}

OBJECTS = {"participant": PARTICIPANT, "event": EVENT, "market": MARKET, "model_price": MODEL_PRICE,
           "thesis": THESIS, "recommendation": RECOMMENDATION, "wager": WAGER, "settlement": SETTLEMENT,
           "run": RUN,
           # research graph objects
           "quality": QUALITY, "link": LINK, "observation": OBSERVATION, "metric": METRIC,
           "capability": CAPABILITY, "search_entry": SEARCH_ENTRY, "tray_item": TRAY_ITEM}


def all_schemas() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for kind, item in COLLECTIONS.items():
        schema = envelope(kind, item)
        schema["title"] = kind
        out[kind] = with_defs(schema)
    for kind, schema in SINGLETONS.items():
        schema = dict(schema)
        schema["title"] = kind
        out[kind] = with_defs(schema)
    for name, schema in OBJECTS.items():
        schema = dict(schema)
        schema["title"] = name
        out[name] = with_defs(schema)
    return out


def render(schema: dict) -> str:
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def write_all(directory: Path = SCHEMA_DIR) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for kind, schema in all_schemas().items():
        path = directory / f"{kind}.schema.json"
        path.write_text(render(schema), encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    for p in write_all():
        print(p)
