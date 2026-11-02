"""Immutable per-contract reconciliation record (issue #116, deliverable 1).

One record per wager, assembled from evidence that already exists elsewhere and NEVER from inference:

  identity        source_bet_key, market ticker, side
  recommendation  recommendation reference / time, only if the owner-supplied record carries one
  model snapshot  model run id, model inputs as-of
  execution       fill ids, order id, contracts, average price, fees (and whether the fees are estimates)
  ontology        a ComboDecoding when the ticker is a multivariate combo
  settlement      result, gross, net, economics version, amendment ids
  close           closing quote for the side bought, and CLV against the fill

Anything absent becomes a ``MISSING_*`` flag and a ``None`` -- not a guess.  ``record_hash`` is the SHA-256 of the
canonical JSON of everything except the hash itself, so a later change to any input produces a *different* record
rather than silently rewriting this one; callers append, they never update.

Money identity (docs: CFB SETTLEMENT_ECONOMICS, router economics v2): ``stake`` already CONTAINS the entry fee, so
``net = gross - stake``.  Fees are reported but must never be subtracted a second time; ``economics_check`` flags a
settlement whose net disagrees with that identity instead of repairing it.

Output holds tickers and prices (private for real fills): treat as a wager row, never print to a public log.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from .combo_decode import ComboDecoding

SCHEMA_VERSION = "contract_reconciliation.v1"
#: Cents of tolerance when comparing dollar figures stated to 4 decimals.
MONEY_TOLERANCE = 0.0051


def _first(row: Mapping[str, Any] | None, *names: str) -> Any:
    if not row:
        return None
    for name in names:
        value = row.get(name)
        if value not in (None, "", []):
            return value
    return None


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


@dataclass(frozen=True)
class ContractReconciliation:
    schema_version: str
    source_bet_key: str | None
    market_ticker: str | None
    side: str | None
    recommendation_id: str | None
    recommendation_generated_at: str | None
    model_run_id: str | None
    model_inputs_as_of: str | None
    market_captured_at: str | None
    placed_at: str | None
    order_id: str | None
    fill_ids: tuple[str, ...]
    contracts: float | None
    execution_price: float | None
    stake: float | None
    fees: float | None
    fees_are_estimated: bool | None
    ontology_status: str | None
    ontology_sports: tuple[str, ...]
    ontology_legs: tuple[dict[str, Any], ...]
    settlement_result: str | None
    gross_return: float | None
    net_pnl: float | None
    economics_version: str | None
    amendment_ids: tuple[str, ...]
    close_price_for_side: float | None
    clv: float | None
    economics_check: str
    missing: tuple[str, ...]
    record_hash: str

    def to_json(self) -> dict[str, Any]:
        out = asdict(self)
        for key in ("fill_ids", "ontology_sports", "ontology_legs", "amendment_ids", "missing"):
            out[key] = list(out[key])
        return out


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def economics_check(stake: float | None, gross: float | None, net: float | None) -> str:
    """``OK`` when net == gross - stake; otherwise a named reason.  Never repairs a figure."""
    if stake is None or gross is None or net is None:
        return "UNESTABLISHED"
    if abs((gross - stake) - net) <= MONEY_TOLERANCE:
        return "OK"
    return "NET_DISAGREES_WITH_GROSS_MINUS_STAKE"


def build_reconciliation(
    wager: Mapping[str, Any],
    settlement: Mapping[str, Any] | None = None,
    *,
    amendments: Sequence[Mapping[str, Any]] = (),
    combo: ComboDecoding | None = None,
    close_price_for_side: float | None = None,
) -> ContractReconciliation:
    """Assemble one record.  ``wager`` may use the router/NBA keys (``execution_price``, ``fees_paid``,
    ``market_ticker``) or the CFB app keys (``average_price``, ``fees``, ``kalshi_ticker``)."""
    linkage = wager.get("linkage") if isinstance(wager.get("linkage"), Mapping) else {}
    ext = wager.get("extensions") if isinstance(wager.get("extensions"), Mapping) else {}

    price = _num(_first(wager, "average_price", "execution_price"))
    stake = _num(wager.get("stake"))
    fees = _num(_first(wager, "fees", "fees_paid"))
    fill_ids = tuple(str(f) for f in (wager.get("kalshi_fill_ids") or ()))
    est = _first(wager, "fees_are_estimated")
    if est is None:
        est = ext.get("fees_are_estimated") if isinstance(ext, Mapping) else None

    # The settlement's own money; the latest amendment (by economics_version order) supersedes the filed row.
    gross = _num(_first(settlement, "gross_payout", "gross_return"))
    net = _num(_first(settlement, "net_pnl", "net_profit_loss"))
    version = _first(settlement, "economics_version") or (
        (settlement or {}).get("extensions", {}) or {}).get("economics_version")
    amendment_ids = tuple(sorted(str(a.get("amendment_id")) for a in amendments if a.get("amendment_id")))
    if amendments:
        top = sorted(amendments, key=lambda a: str(a.get("economics_version") or ""))[-1]
        gross = _num(top.get("gross_return")) if _num(top.get("gross_return")) is not None else gross
        net = _num(top.get("net_profit_loss")) if _num(top.get("net_profit_loss")) is not None else net
        version = top.get("economics_version") or version

    clv = None
    if close_price_for_side is not None and price is not None:
        clv = round(close_price_for_side - price, 6)

    values: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source_bet_key": _first(wager, "source_bet_key") or (wager.get("source_ids") or {}).get("source_bet_key"),
        "market_ticker": _first(wager, "kalshi_ticker", "market_ticker"),
        "side": _first(wager, "selection", "side"),
        "recommendation_id": _first(wager, "recommendation_id"),
        "recommendation_generated_at": _first(linkage, "recommendation_generated_at"),
        "model_run_id": _first(wager, "model_run_id"),
        "model_inputs_as_of": _first(linkage, "model_inputs_as_of"),
        "market_captured_at": _first(linkage, "market_captured_at"),
        "placed_at": _first(wager, "placed_at", "executed_at") or _first(linkage, "wager_placed_at"),
        "order_id": _first(wager, "kalshi_order_id", "order_id"),
        "fill_ids": fill_ids,
        "contracts": _num(wager.get("contracts")),
        "execution_price": price,
        "stake": stake,
        "fees": fees,
        "fees_are_estimated": est if isinstance(est, bool) else None,
        "ontology_status": combo.status.value if combo else None,
        "ontology_sports": tuple(combo.sports) if combo else (),
        "ontology_legs": tuple(
            {"market_ticker": l.market_ticker, "required_side": l.required_side, "sport": l.sport.value,
             "note": l.note} for l in (combo.legs if combo else ())),
        "settlement_result": _first(settlement, "result"),
        "gross_return": gross,
        "net_pnl": net,
        "economics_version": version,
        "amendment_ids": amendment_ids,
        "close_price_for_side": close_price_for_side,
        "clv": clv,
    }
    values["economics_check"] = economics_check(stake, gross, net) if settlement is not None else "NOT_SETTLED"

    missing = []
    for flag, key in (
        ("MISSING_RECOMMENDATION_ID", "recommendation_id"),
        ("MISSING_MODEL_RUN_ID", "model_run_id"),
        ("MISSING_MODEL_INPUTS_AS_OF", "model_inputs_as_of"),
        ("MISSING_ORDER_ID", "order_id"),
        ("MISSING_EXECUTION_PRICE", "execution_price"),
        ("MISSING_FEES", "fees"),
    ):
        if values[key] is None:
            missing.append(flag)
    if not fill_ids:
        missing.append("MISSING_FILL_IDS")
    if settlement is None:
        missing.append("MISSING_SETTLEMENT")
    if close_price_for_side is None:
        missing.append("MISSING_CLOSE_QUOTE")
    if combo is not None and not combo.decoded:
        missing.append("UNDECODED_COMBO")
    values["missing"] = tuple(sorted(missing))

    digest = hashlib.sha256(_canonical({k: (list(v) if isinstance(v, tuple) else v)
                                        for k, v in values.items()}).encode()).hexdigest()
    return ContractReconciliation(record_hash=digest, **values)


def summarize(records: Sequence[ContractReconciliation]) -> dict[str, Any]:
    """Counts only (public-safe): how many records carry each MISSING_* flag, plus economics-check tallies."""
    flags: dict[str, int] = {}
    checks: dict[str, int] = {}
    for rec in records:
        for flag in rec.missing:
            flags[flag] = flags.get(flag, 0) + 1
        checks[rec.economics_check] = checks.get(rec.economics_check, 0) + 1
    return {"schema_version": SCHEMA_VERSION, "records": len(records),
            "missing_flag_counts": dict(sorted(flags.items())), "economics_check_counts": dict(sorted(checks.items()))}
