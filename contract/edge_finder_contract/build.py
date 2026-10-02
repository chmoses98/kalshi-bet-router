"""Constructors that emit EVERY field of each object, null where the source has nothing.

An adapter calls these rather than assembling dicts, so no sport can omit a key, misspell one,
or leak an internal spelling. Each constructor validates its output against the object schema and
raises :class:`SchemaError` with the offending path, which is the error an adapter author wants.

Probabilities and prices are in [0, 1] (dollars per contract == probability). Callers converting
from cents or percentages do so with :func:`from_cents` / :func:`from_percent`, explicitly.
"""

from __future__ import annotations

from typing import Any

from . import SCHEMA_VERSION, ids
from .timeutil import now_utc, to_iso, to_iso_or_none
from .validate import validate


def from_cents(value: object) -> float | None:
    if value is None or value == "":
        return None
    return round(float(value) / 100.0, 6)


def from_percent(value: object) -> float | None:
    if value is None or value == "":
        return None
    return round(float(value) / 100.0, 6)


def as_prob(value: object) -> float | None:
    """A dollar-string, float or Decimal already in [0,1]; None stays None."""
    if value is None or value == "":
        return None
    return round(float(value), 6)


def _num(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def _strs(values: Any) -> list[str]:
    if not values:
        return []
    return [str(v) for v in values if v is not None]


def _ids(source_ids: dict | None) -> dict:
    out: dict = {}
    for k, v in (source_ids or {}).items():
        if v is None:
            out[str(k)] = None
        elif isinstance(v, bool):
            out[str(k)] = str(v)
        elif isinstance(v, int):
            out[str(k)] = v
        else:
            out[str(k)] = str(v)
    return out


def normalise_family(value: object) -> str:
    text = str(value or "unknown").strip().lower().replace("-", "_").replace(" ", "_")
    return text or "unknown"


def participant(*, sport: str, participant_type: str, source: str, source_id: object, display_name: str,
                short_name: object = None, source_ids: dict | None = None, metadata: dict | None = None) -> dict:
    out = {
        "participant_id": ids.participant_id(sport, participant_type, source, source_id),
        "display_name": str(display_name),
        "short_name": _str(short_name),
        "participant_type": participant_type,
        "source_ids": _ids({source: source_id, **(source_ids or {})}),
        "metadata": dict(metadata or {}),
    }
    validate(out, "participant")
    return out


def event(*, sport: str, source: str, source_id: object, start_time_utc: object, participants: list[dict],
          home_participant: object = None, away_participant: object = None, league: object = None,
          season: object = None, competition: object = None, status: str = "SCHEDULED",
          start_time_local: object = None, start_time_source: object = None,
          start_time_confidence: object = None, effective_start_time_utc: object = None,
          venue: object = None, broadcast: object = None, source_ids: dict | None = None,
          schedule_updated_at: object = None, last_updated_at: object = None,
          extensions: dict | None = None) -> dict:
    sport = ids.normalize_sport(sport)
    out = {
        "event_id": ids.event_id(sport, source, source_id),
        "sport": sport,
        "league": _str(league),
        "season": _str(season),
        "competition": _str(competition),
        "home_participant": _str(home_participant),
        "away_participant": _str(away_participant),
        "participants": list(participants),
        "start_time_utc": to_iso(start_time_utc),
        "start_time_local": _str(start_time_local),
        "start_time_source": _str(start_time_source),
        "start_time_confidence": _str(start_time_confidence),
        "effective_start_time_utc": to_iso_or_none(effective_start_time_utc),
        "status": status,
        "venue": _str(venue),
        "broadcast": _str(broadcast),
        "source_ids": _ids({source: source_id, **(source_ids or {})}),
        "schedule_updated_at": to_iso_or_none(schedule_updated_at),
        "last_updated_at": to_iso(last_updated_at or now_utc()),
        "extensions": dict(extensions or {}),
    }
    validate(out, "event")
    return out


def market(*, sport: str, kalshi_ticker: str, market_family: object, yes_description: str, source: str,
           event_id: object = None, kalshi_event_ticker: object = None, kalshi_series_ticker: object = None,
           market_type: object = None, period: object = None, participant_id: object = None,
           player_id: object = None, side: object = None, line: object = None, threshold: object = None,
           no_description: object = None, yes_bid: object = None, yes_ask: object = None,
           no_bid: object = None, no_ask: object = None, last_price: object = None, volume: object = None,
           open_interest: object = None, market_status: str = "OPEN", close_time_utc: object = None,
           captured_at: object = None, raw_market_reference: object = None,
           extensions: dict | None = None, market_probability: object = None) -> dict:
    yb, ya = as_prob(yes_bid), as_prob(yes_ask)
    if market_probability is None and yb is not None and ya is not None:
        market_probability = round((yb + ya) / 2.0, 6)
    ticker = str(kalshi_ticker).strip().upper()
    out = {
        "market_id": ids.market_id(ticker),
        "kalshi_ticker": ticker,
        "kalshi_event_ticker": _str(kalshi_event_ticker) or (ticker.rsplit("-", 1)[0] if ticker.count("-") >= 2 else None),
        "kalshi_series_ticker": _str(kalshi_series_ticker) or (ticker.split("-", 1)[0] if "-" in ticker else None),
        "event_id": _str(event_id),
        "sport": ids.normalize_sport(sport),
        "market_family": normalise_family(market_family),
        "market_type": _str(market_type),
        "period": _str(period),
        "participant_id": _str(participant_id),
        "player_id": _str(player_id),
        "side": _str(side),
        "line": _num(line),
        "threshold": _num(threshold),
        "yes_description": str(yes_description),
        "no_description": _str(no_description),
        "market_probability": as_prob(market_probability),
        "yes_bid": yb, "yes_ask": ya, "no_bid": as_prob(no_bid), "no_ask": as_prob(no_ask),
        "last_price": as_prob(last_price),
        "volume": _num(volume), "open_interest": _num(open_interest),
        "market_status": market_status,
        "close_time_utc": to_iso_or_none(close_time_utc),
        "captured_at": to_iso_or_none(captured_at),
        "source": str(source),
        "raw_market_reference": _str(raw_market_reference),
        "extensions": dict(extensions or {}),
    }
    validate(out, "market")
    return out


def market_stub(*, sport: str, kalshi_ticker: str, market_family: object = "unknown",
                yes_description: object = None, event_id: object = None, source: str = "ledger",
                market_status: str = "SETTLED") -> dict:
    """A market no longer on the board, referenced by a wager. Prices null, identity intact."""
    return market(sport=sport, kalshi_ticker=kalshi_ticker, market_family=market_family,
                  yes_description=str(yes_description or f"YES on {kalshi_ticker}"), source=source,
                  event_id=event_id, market_status=market_status)


def model_price(*, run_id: str, market_id: str, fair_probability: object, generated_at: object,
                event_id: object = None, model_version: object = None, lower_bound: object = None,
                upper_bound: object = None, uncertainty: object = None, market_probability: object = None,
                edge: object = None, projection_value: object = None, projection_unit: object = None,
                inputs_as_of: object = None, freshness_status: str = "UNKNOWN",
                data_quality_status: str = "UNKNOWN", support_status: object = None,
                extensions: dict | None = None) -> dict:
    fair = as_prob(fair_probability)
    mp = as_prob(market_probability)
    if edge is None and fair is not None and mp is not None:
        edge = round(fair - mp, 6)
    out = {
        "model_price_id": ids.model_price_id(run_id, market_id, model_version),
        "event_id": _str(event_id),
        "market_id": market_id,
        "run_id": run_id,
        "model_version": _str(model_version),
        "fair_probability": fair,
        "lower_bound": as_prob(lower_bound), "upper_bound": as_prob(upper_bound),
        "uncertainty": _num(uncertainty),
        "market_probability": mp,
        "edge": _num(edge),
        "projection_value": _num(projection_value), "projection_unit": _str(projection_unit),
        "generated_at": to_iso(generated_at),
        "inputs_as_of": to_iso_or_none(inputs_as_of),
        "freshness_status": freshness_status,
        "data_quality_status": data_quality_status,
        "support_status": _str(support_status),
        "extensions": dict(extensions or {}),
    }
    validate(out, "model_price")
    return out


def thesis(*, sport: str, run_id: str, event_id: str, generated_at: object, summary: object = None,
           primary_game_script: object = None, supporting_factors=None, opposing_factors=None,
           key_dependencies=None, context_notes: dict | None = None, confidence_label: object = None,
           evidence: dict | None = None, scope: object = "event") -> dict:
    notes = {k: _str((context_notes or {}).get(k)) for k in ("injuries", "lineups", "weather", "usage", "other")}
    out = {
        "thesis_id": ids.thesis_id(sport, run_id, event_id, scope),
        "event_id": event_id, "run_id": run_id,
        "summary": _str(summary), "primary_game_script": _str(primary_game_script),
        "supporting_factors": _strs(supporting_factors), "opposing_factors": _strs(opposing_factors),
        "key_dependencies": _strs(key_dependencies),
        "context_notes": notes,
        "confidence_label": _str(confidence_label),
        "evidence": dict(evidence or {}),
        "generated_at": to_iso(generated_at),
    }
    validate(out, "thesis")
    return out


def recommendation(*, sport: str, source_repo: str, event_id: str, market_id: str, run_id: str,
                   selection: str, market_description: str, created_at: object, status: str,
                   authority: str, research_only: bool, native_id: object = None,
                   current_probability: object = None, current_price: object = None,
                   fair_probability: object = None, edge: object = None,
                   bet_up_to_probability: object = None, bet_up_to_price: object = None,
                   confidence: object = None, stake_units: object = None, stake_dollars: object = None,
                   bankroll_basis: object = None, thesis_id: object = None,
                   reason_not_playable: object = None, expires_at: object = None,
                   data_freshness: str = "UNKNOWN", lineup_status: object = None, injury_flags=None,
                   source_ids: dict | None = None, extensions: dict | None = None) -> dict:
    sport = ids.normalize_sport(sport)
    out = {
        "recommendation_id": ids.recommendation_id(sport, source_repo, native_id, run_id=run_id,
                                                   market_id_value=market_id, selection=selection),
        "event_id": event_id, "market_id": market_id, "sport": sport, "run_id": run_id,
        "selection": selection, "market_description": str(market_description),
        "current_probability": as_prob(current_probability), "current_price": as_prob(current_price),
        "fair_probability": as_prob(fair_probability), "edge": _num(edge),
        "bet_up_to_probability": as_prob(bet_up_to_probability), "bet_up_to_price": as_prob(bet_up_to_price),
        "confidence": _str(confidence),
        "stake_units": _num(stake_units), "stake_dollars": _num(stake_dollars), "bankroll_basis": _num(bankroll_basis),
        "thesis_id": _str(thesis_id),
        "status": status, "reason_not_playable": _str(reason_not_playable),
        "created_at": to_iso(created_at), "expires_at": to_iso_or_none(expires_at),
        "data_freshness": data_freshness,
        "lineup_status": _str(lineup_status), "injury_flags": _strs(injury_flags),
        "research_only": bool(research_only), "authority": authority,
        "source_repo": str(source_repo),
        "source_ids": _ids(source_ids), "extensions": dict(extensions or {}),
    }
    validate(out, "recommendation")
    return out


def wager(*, sport: str, kalshi_ticker: str, selection: str, contracts: object, stake: object,
          average_price: object, placed_at: object, source: str, destination_repo: str,
          source_bet_key: object = None, native_id: object = None, kalshi_order_id: object = None,
          kalshi_fill_ids=None, event_id: object = None, side: object = None, fees: object = None,
          router_ingested_at: object = None, model_run_id: object = None, model_price_id: object = None,
          recommendation_id: object = None, linkage: dict | None = None,
          settlement_status: str = "PENDING", settlement_id: object = None, payout: object = None,
          profit_loss: object = None, source_ids: dict | None = None, extensions: dict | None = None) -> dict:
    sport = ids.normalize_sport(sport)
    ticker = str(kalshi_ticker).strip().upper()
    link = linkage or {}
    out = {
        "wager_id": ids.wager_id(sport, source_bet_key, source_repo=destination_repo, native_id=native_id),
        "source_bet_key": _str(source_bet_key),
        "kalshi_order_id": _str(kalshi_order_id), "kalshi_fill_ids": _strs(kalshi_fill_ids),
        "event_id": _str(event_id),
        "market_id": ids.market_id(ticker), "kalshi_ticker": ticker, "sport": sport,
        "selection": selection, "side": _str(side),
        "contracts": float(contracts), "stake": float(stake), "average_price": as_prob(average_price),
        "fees": _num(fees),
        "placed_at": to_iso(placed_at), "source": source,
        "router_ingested_at": to_iso_or_none(router_ingested_at),
        "destination_repo": str(destination_repo),
        "model_run_id": _str(model_run_id), "model_price_id": _str(model_price_id),
        "recommendation_id": _str(recommendation_id),
        "linkage": {
            "wager_placed_at": to_iso_or_none(link.get("wager_placed_at")),
            "model_inputs_as_of": to_iso_or_none(link.get("model_inputs_as_of")),
            "market_captured_at": to_iso_or_none(link.get("market_captured_at")),
            "recommendation_generated_at": to_iso_or_none(link.get("recommendation_generated_at")),
        },
        "settlement_status": settlement_status,
        "settlement_id": _str(settlement_id),
        "payout": _num(payout), "profit_loss": _num(profit_loss),
        "source_ids": _ids(source_ids), "extensions": dict(extensions or {}),
    }
    validate(out, "wager")
    return out


def settlement(*, wager_id: str, market_id: str, result: str, settled_at: object, source: str,
               verification_status: str, winning_side: object = None, settlement_value: object = None,
               gross_payout: object = None, fees: object = None, net_pnl: object = None,
               refusals=None, source_ids: dict | None = None, extensions: dict | None = None) -> dict:
    out = {
        "settlement_id": ids.settlement_id(wager_id),
        "wager_id": wager_id, "market_id": market_id,
        "result": result, "winning_side": _str(winning_side),
        "settlement_value": as_prob(settlement_value),
        "settled_at": to_iso(settled_at),
        "gross_payout": _num(gross_payout), "fees": _num(fees), "net_pnl": _num(net_pnl),
        "source": str(source), "source_ids": _ids(source_ids),
        "verification_status": verification_status,
        "refusals": _strs(refusals), "extensions": dict(extensions or {}),
    }
    validate(out, "settlement")
    return out


def run(*, sport: str, repo: str, completed_at: object, scope: str, status: str = "SUCCESS",
        native_run_id: object = None, commit_sha: object = None, workflow_run_id: object = None,
        model_version: object = None, started_at: object = None, events_requested: object = None,
        events_processed: int = 0, markets_discovered: int = 0, markets_priced: int = 0,
        recommendations_created: int = 0, data_sources=None, input_freshness: dict | None = None,
        warnings=None, errors=None, source_ids: dict | None = None) -> dict:
    sport = ids.normalize_sport(sport)
    out = {
        "run_id": ids.run_id(sport, repo, native_run_id, generated_at=to_iso(completed_at)),
        "sport": sport, "repo": str(repo),
        "commit_sha": _str(commit_sha), "workflow_run_id": _str(workflow_run_id),
        "model_version": _str(model_version),
        "started_at": to_iso_or_none(started_at), "completed_at": to_iso(completed_at),
        "status": status, "scope": str(scope),
        "events_requested": None if events_requested is None else int(events_requested),
        "events_processed": int(events_processed), "markets_discovered": int(markets_discovered),
        "markets_priced": int(markets_priced), "recommendations_created": int(recommendations_created),
        "data_sources": _strs(data_sources),
        "input_freshness": {str(k): to_iso_or_none(v) for k, v in (input_freshness or {}).items()},
        "warnings": _strs(warnings), "errors": _strs(errors),
        "source_ids": _ids({"native_run_id": native_run_id, **(source_ids or {})}),
    }
    validate(out, "run")
    return out


def collection(kind: str, sport: str, run_id: str, generated_at: object, items: list[dict]) -> dict:
    out = {
        "schema_version": SCHEMA_VERSION, "kind": kind, "sport": ids.normalize_sport(sport),
        "run_id": run_id, "generated_at": to_iso(generated_at), "count": len(items), "items": list(items),
    }
    validate(out, kind)
    return out
