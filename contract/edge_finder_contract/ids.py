"""Deterministic identities. Same inputs, same id, on every machine, forever.

Every id is ``<prefix>_<20 hex>`` where the digest is sha256 over a length-prefixed, domain
separated encoding of the inputs (length-prefixed so that no separator byte can be forged into a
collision). Identity is never a display name: it is the sport plus the strongest provider id the
source repository has, and the readable provider ids travel beside it in ``source_ids``.

The one exception is the market: a Kalshi ticker is already a stable, globally unique, filename
safe identifier, so ``market_id`` is ``mkt_kalshi_<TICKER>`` and needs no digest.
"""

from __future__ import annotations

import hashlib
import re

from . import SCHEMA_VERSION, SPORTS

_DOMAIN = "edge_finder.app.v1/ids"
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]*$")

SPORT_ALIASES = {
    "mlb": "MLB", "baseball": "MLB",
    "cfb": "CFB", "ncaaf": "CFB", "college football": "CFB", "ncaa football": "CFB",
    "nfl": "NFL", "pro football": "NFL",
    "nba": "NBA", "basketball": "NBA", "pro basketball": "NBA",
    "nhl": "NHL", "hockey": "NHL", "pro hockey": "NHL",
    "soccer": "SOCCER", "football (soccer)": "SOCCER", "futbol": "SOCCER",
    "tennis": "TENNIS",
}


def normalize_sport(value: object) -> str:
    """Map any spelling the repositories use onto the canonical enum, or raise."""
    if isinstance(value, str):
        key = value.strip()
        if key.upper() in SPORTS:
            return key.upper()
        if key.lower() in SPORT_ALIASES:
            return SPORT_ALIASES[key.lower()]
    raise ValueError(f"unknown sport {value!r}; canonical values are {SPORTS}")


def digest(*parts: object, length: int = 20) -> str:
    material = _DOMAIN + "|" + SCHEMA_VERSION + "|"
    for part in parts:
        text = "" if part is None else str(part)
        material += f"{len(text.encode('utf-8'))}:{text}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:length]


def make_id(prefix: str, *parts: object) -> str:
    return f"{prefix}_{digest(*parts)}"


def event_id(sport: str, source: str, source_id: object) -> str:
    """``source`` names the provider namespace (``mlb_game_pk``, ``espn_event_id``, ``nflverse_game_id``,
    ``nhl_game_id``, ``kalshi_milestone_id``, ``fixture_id``, ``kalshi_event_ticker`` ...)."""
    if not source or source_id in (None, ""):
        raise ValueError("an event id needs a source namespace and a source id")
    return make_id("evt", "event", normalize_sport(sport), source, source_id)


def participant_id(sport: str, participant_type: str, source: str, source_id: object) -> str:
    if participant_type not in ("TEAM", "PLAYER", "PAIR"):
        raise ValueError(f"participant_type must be TEAM, PLAYER or PAIR, not {participant_type!r}")
    if not source or source_id in (None, ""):
        raise ValueError("a participant id needs a source namespace and a source id")
    return make_id("prt", "participant", normalize_sport(sport), participant_type, source, source_id)


def market_id(kalshi_ticker: str) -> str:
    ticker = (kalshi_ticker or "").strip().upper()
    if not _TICKER_RE.match(ticker):
        raise ValueError(f"not a Kalshi ticker: {kalshi_ticker!r}")
    return f"mkt_kalshi_{ticker}"


def ticker_from_market_id(market_id_value: str) -> str:
    if not market_id_value.startswith("mkt_kalshi_"):
        raise ValueError(f"not a kalshi market id: {market_id_value!r}")
    return market_id_value[len("mkt_kalshi_"):]


def model_price_id(run_id: str, market_id_value: str, model_version: object = None) -> str:
    return make_id("mp", "model_price", run_id, market_id_value, model_version)


def thesis_id(sport: str, run_id: str, event_id_value: str, scope: object = "event") -> str:
    return make_id("ths", "thesis", normalize_sport(sport), run_id, event_id_value, scope)


def recommendation_id(sport: str, source_repo: str, native_id: object = None, *,
                      run_id: object = None, market_id_value: object = None,
                      selection: object = None) -> str:
    """Prefer the source repository's own stable recommendation identity (``native_id``). When it has
    none, the (run, market, selection) triple is the identity."""
    if native_id not in (None, ""):
        return make_id("rec", "recommendation", normalize_sport(sport), source_repo, native_id)
    if not run_id or not market_id_value or not selection:
        raise ValueError("a recommendation id needs a native id or run_id + market_id + selection")
    return make_id("rec", "recommendation", normalize_sport(sport), source_repo, run_id,
                   market_id_value, selection)


def wager_id(sport: str, source_bet_key: object = None, *, source_repo: object = None,
             native_id: object = None) -> str:
    """The router's ``source_bet_key`` is the preferred identity (it is already deterministic per
    Kalshi order). A manually recorded wager without one uses the repository's own record id."""
    if source_bet_key not in (None, ""):
        return make_id("wgr", "wager", "source_bet_key", source_bet_key)
    if source_repo and native_id not in (None, ""):
        return make_id("wgr", "wager", normalize_sport(sport), source_repo, native_id)
    raise ValueError("a wager id needs a source_bet_key or a source_repo + native id")


def settlement_id(wager_id_value: str) -> str:
    """One wager settles once; the settlement identity is a function of the wager identity."""
    if not wager_id_value:
        raise ValueError("a settlement id needs a wager id")
    return make_id("stl", "settlement", wager_id_value)


def run_id(sport: str, repo: str, native_run_id: object = None, *, generated_at: object = None) -> str:
    if native_run_id in (None, "") and generated_at in (None, ""):
        raise ValueError("a run id needs the source run id or the generation timestamp")
    return make_id("run", "run", normalize_sport(sport), repo, native_run_id, generated_at)


# ------------------------------------------------------------------ research graph (contract 1.1.0)

_METRIC_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def metric_id(sport: str, slug: str) -> str:
    """``met_<sport>.<slug>``: readable and stable, like a market id is its ticker. The slug is the
    metric's methodology name in lower snake_case (``pass_epa_per_play``), never a display name."""
    slug = (slug or "").strip().lower()
    if not _METRIC_SLUG_RE.match(slug):
        raise ValueError(f"not a metric slug: {slug!r} (lower snake_case, starting with a letter)")
    return f"met_{normalize_sport(sport).lower()}.{slug}"


def observation_id(sport: str, metric_id_value: str, entity_id_value: str, window_label: str,
                   split_label: object = None, as_of: object = None) -> str:
    return make_id("obs", "observation", normalize_sport(sport), metric_id_value, entity_id_value,
                   window_label, split_label, as_of)


def ranking_id(sport: str, metric_id_value: str, universe_label: str, window_label: str,
               split_label: object = None) -> str:
    return make_id("rnk", "ranking", normalize_sport(sport), metric_id_value, universe_label, window_label, split_label)


def series_id(sport: str, metric_id_value: str, entity_id_value: str, x_axis: str, split_label: object = None) -> str:
    return make_id("ser", "series", normalize_sport(sport), metric_id_value, entity_id_value, x_axis, split_label)


def packet_id(protocol_id: str, scope_kind: str, *scope_parts: object, data_as_of: object = None) -> str:
    """Deterministic over the protocol, the scope and the data as-of: the same request against the
    same publication yields the same packet id (and, by construction, the same packet)."""
    return make_id("pkt", "packet", protocol_id, scope_kind, *sorted(str(p) for p in scope_parts), data_as_of)


def tray_item_id(ref_kind: str, id_value: str, extra: object = None) -> str:
    return make_id("try", "tray", ref_kind, id_value, extra)
