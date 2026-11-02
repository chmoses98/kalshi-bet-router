"""Execution evidence for one routed wager: WHICH orders and fills made it, and WHEN -- without publishing their ids.

THE GAP THIS CLOSES
-------------------
Every downstream ledger row (CFB 308 of 308, NBA 47 of 47) carries no order id, no fill ids and no fill window, so a
losing bet cannot be tied back to the exchange's own order record, and a recommendation cannot be matched to the
fill that executed it. The router is the only component that SEES those ids, in ``OrderExecution``.

THE UNIT IS THE ORDER. A production wager is one submitted order: ``source_bet_key`` is ``kalshi:v1:`` plus a digest
of (subaccount, ticker, order id) -- see ``production.production_source_key`` -- so the evidence row is keyed by that
same key and describes that order's fills.

WHY DIGESTS, NOT IDS
--------------------
This repository is public and the README's security boundary lists order ids, fill ids and trade ids as private. The
downstream ledgers are public too. A raw id must therefore never leave the router's memory. What does cross is a
domain-separated SHA-256 digest of each id: the owner, who holds the ids in their Kalshi account, can recompute the
digest of any order and match it to a ledger row; nobody else can invert a digest of a high-entropy id.

WHAT IS NOT CAPTURED (and is flagged, never inferred)
-----------------------------------------------------
* ``FILL_IDS_MISSING`` -- an order whose execution carries no fill ids.
* ``ACTION_NOT_ESTABLISHED`` -- the buy/sell verb is absent or mixed (the exchange deprecated it on 2026-05-14).
* ``TAKER_NOT_ESTABLISHED`` -- maker/taker is mixed or unreported across the order's fills.
* Per-fill timestamps: only the order's first and last execution times are recorded.
* A recommendation id. ``routed_ledger.PROVENANCE_FIELDS`` FORBIDS recommendation/model fields in a routed row by
  design ("no model is involved and none has authority"). The link to a recommendation is made on the edge-finder
  side, by market + side + time, from these digests' timestamps; the router never asserts it.
* A closing price. The client exposes no candlestick/orderbook-history endpoint, and a settled market's own
  ``last_price`` is the 0/1 resolution, not a pre-close quote. See docs/POSTMORTEM_2026-11-02.md.

NOT WIRED INTO DELIVERY
-----------------------
The contract's ``WAGER_INPUT_FIELDS`` is a closed list and every destination's vendored importer REFUSES an unknown
field. So this module produces a SIDECAR row, keyed by ``source_bet_key``, and changes no wager row, no identity and
no delivery. Landing it in a ledger needs the contract change documented in docs/POSTMORTEM_2026-11-02.md first.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .accounting.execution import OrderExecution
from .production import ProductionWager, production_source_key

SCHEMA_VERSION = "router_execution_evidence.v1"
FILL_IDS_MISSING = "FILL_IDS_MISSING"
ACTION_NOT_ESTABLISHED = "ACTION_NOT_ESTABLISHED"
TAKER_NOT_ESTABLISHED = "TAKER_NOT_ESTABLISHED"
DIGEST_HEX_CHARS = 32  # 128 bits: ample for a collision-free match against the owner's own ids


def id_digest(kind: str, value: str) -> str:
    """Domain-separated digest. ``kind`` stops an order id and a fill id that happen to be equal from colliding."""
    if kind not in ("order", "fill"):
        raise ValueError(f"unknown id kind {kind!r}")
    material = f"kalshi-router.execution-evidence.v1|{kind}|{value}".encode()
    return hashlib.sha256(material).hexdigest()[:DIGEST_HEX_CHARS]


def _num(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


@dataclass(frozen=True)
class ExecutionEvidence:
    """Digests, times and flags only. Safe to commit; still never log per-wager."""

    source_bet_key: str
    order_id_digest: str
    fill_id_digests: tuple[str, ...]
    fill_count: int
    first_fill_ts: int | float
    last_fill_ts: int | float
    execution_action: str | None
    all_taker: bool | None
    flags: tuple[str, ...]

    def to_sidecar_row(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "source_bet_key": self.source_bet_key,
            "order_id_digest": self.order_id_digest,
            "fill_id_digests": list(self.fill_id_digests),
            "fill_count": self.fill_count,
            "first_fill_ts": self.first_fill_ts,
            "last_fill_ts": self.last_fill_ts,
            "execution_action": self.execution_action,
            "all_taker": self.all_taker,
            "flags": list(self.flags),
        }


def build_execution_evidence(
    order: OrderExecution, wager: ProductionWager | None = None
) -> ExecutionEvidence:
    """The evidence for one order. If ``wager`` is given its ``source_key`` must be this order's own key -- a
    mismatch raises rather than attaching one order's fills to another order's wager."""
    key = production_source_key(order.subaccount_number, order.ticker, order.order_id)
    if wager is not None and wager.source_key != key:
        raise ValueError("the wager does not belong to this order: refusing to attach its evidence")
    flags: list[str] = []
    if not order.fill_ids:
        flags.append(FILL_IDS_MISSING)
    action = order.legacy_action.value.upper() if order.legacy_action is not None else None
    if action is None:
        flags.append(ACTION_NOT_ESTABLISHED)
    if order.all_taker is None:
        flags.append(TAKER_NOT_ESTABLISHED)
    return ExecutionEvidence(
        source_bet_key=key,
        order_id_digest=id_digest("order", order.order_id),
        fill_id_digests=tuple(sorted(id_digest("fill", f) for f in set(order.fill_ids))),
        fill_count=order.fill_count,
        first_fill_ts=_num(order.first_execution_time),
        last_fill_ts=_num(order.last_execution_time),
        execution_action=action,
        all_taker=order.all_taker,
        flags=tuple(flags),
    )
