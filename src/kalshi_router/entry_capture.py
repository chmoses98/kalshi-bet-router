"""Prospective ENTRY-MARKET capture: what did the book look like at the instant an order was placed?  RESEARCH ONLY, GET-only.

THE RULE THIS MODULE EXISTS TO ENFORCE
  A snapshot is an ENTRY quote only if it was OBSERVED AT OR BEFORE the order's placement time and is FRESH (<= FRESH_SECONDS old).
  * A snapshot taken after placement is future information: SNAPSHOT_AFTER_ORDER, never used.
  * A snapshot minutes old is not the book at execution: STALE_NOT_ENTRY (kept only as a labelled coverage count, excluded from every
    entry-cost statistic).
  * No snapshot: NO_SNAPSHOT. Nothing is reconstructed, interpolated or borrowed from a later book.

PRIVACY: nothing per-order leaves memory. ``Aggregator.public()`` emits counters and per-GAME-digest sums (cents) grouped by series family;
no ticker, order/fill id, price level, quantity, dollar amount or timestamp. Order dedup state is a set of one-way order digests.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Optional

FRESH_SECONDS = 45          # sampler period is ~20 s; anything older than this is not "the book at execution"
NEAR_SECONDS = 900          # coverage label only; NEVER used for entry statistics
ENTRY_FRESH, STALE_NOT_ENTRY, SNAPSHOT_AFTER_ORDER, NO_SNAPSHOT, ONE_SIDED = (
    "ENTRY_FRESH", "STALE_NOT_ENTRY", "SNAPSHOT_AFTER_ORDER", "NO_SNAPSHOT", "SNAPSHOT_ONE_SIDED_OR_CROSSED")
STATUSES = (ENTRY_FRESH, STALE_NOT_ENTRY, SNAPSHOT_AFTER_ORDER, NO_SNAPSHOT, ONE_SIDED)
ZERO, ONE = Decimal(0), Decimal(1)


def digest(kind: str, value: str) -> str:
    return hashlib.sha256(f"sift-entry-capture.v1|{kind}|{value}".encode()).hexdigest()[:32]


def series_of(ticker: str) -> str:
    return ticker.split("-", 1)[0]


def event_of(ticker: str) -> str:
    parts = ticker.split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else ticker


def _dec(v) -> Optional[Decimal]:
    try:
        return Decimal(str(v)) if v is not None and str(v).strip() != "" else None
    except Exception:
        return None


@dataclass(frozen=True)
class Snap:
    observed_at: datetime
    yes_bid: Optional[Decimal]
    yes_ask: Optional[Decimal]
    yes_bid_size: Optional[Decimal] = None
    yes_ask_size: Optional[Decimal] = None


class Ring:
    """Per-ticker time-ordered snapshots, bounded in time. Only append what the exchange returned, stamped with OUR observation time."""

    def __init__(self, horizon_seconds: int = 1800):
        self.h, self.by = horizon_seconds, defaultdict(deque)

    def add(self, ticker: str, snap: Snap) -> None:
        q = self.by[ticker]
        q.append(snap)
        while q and (snap.observed_at - q[0].observed_at).total_seconds() > self.h:
            q.popleft()

    def tickers(self) -> int:
        return len(self.by)

    def latest_at_or_before(self, ticker: str, t: datetime):
        """(snapshot, reason). The latest snapshot observed <= t; reason explains a miss."""
        best, saw_after = None, False
        for s in self.by.get(ticker, ()):
            if s.observed_at <= t:
                best = s
            else:
                saw_after = True
        if best is not None:
            return best, ""
        return None, SNAPSHOT_AFTER_ORDER if saw_after else NO_SNAPSHOT


@dataclass(frozen=True)
class EntryRow:
    status: str
    age_seconds: Optional[float] = None
    spread_cents: Optional[float] = None
    paid_vs_ask_cents: Optional[float] = None       # fee-exclusive VWAP - entry ask on the held side (>0: paid above displayed best ask)
    paid_vs_mid_cents: Optional[float] = None
    entry_ask_size: Optional[float] = None
    qty: Optional[float] = None


def join_order(ring: Ring, *, ticker: str, outcome_side: str, placed_at: datetime, vwap: Decimal, qty: Decimal) -> EntryRow:
    """Join ONE order to its entry quote under the rule above. ``vwap`` is the fee-exclusive price of the traded outcome side."""
    snap, why = ring.latest_at_or_before(ticker, placed_at)
    if snap is None:
        return EntryRow(why)
    age = (placed_at - snap.observed_at).total_seconds()
    yb, ya = snap.yes_bid, snap.yes_ask
    if yb is None or ya is None or not (ZERO < yb < ONE and ZERO < ya < ONE) or ya < yb:
        return EntryRow(ONE_SIDED, age)
    side = str(outcome_side).lower()
    bid, ask = (yb, ya) if side == "yes" else (ONE - ya, ONE - yb)
    size = snap.yes_ask_size if side == "yes" else snap.yes_bid_size           # NO ask size = YES bid size
    status = ENTRY_FRESH if age <= FRESH_SECONDS else STALE_NOT_ENTRY
    mid = (bid + ask) / 2
    return EntryRow(status, age, float((ask - bid) * 100), float((vwap - ask) * 100), float((vwap - mid) * 100),
                    None if size is None else float(size), float(qty))


class Aggregator:
    """Cells keyed by series family. Entry statistics use ENTRY_FRESH rows ONLY; per-game sums allow game-clustered intervals."""

    def __init__(self, state: Optional[dict] = None):
        self.s = state or {"orders_seen": [], "cells": {}, "games": {}}
        self.seen = set(self.s["orders_seen"])

    def has(self, order_digest: str) -> bool:
        return order_digest in self.seen

    def add(self, order_digest: str, family: str, game_digest: str, row: EntryRow) -> None:
        if order_digest in self.seen:
            return
        self.seen.add(order_digest)
        c = self.s["cells"].setdefault(family, {k: 0 for k in STATUSES} | {"orders": 0, "fresh_spread_sum": 0.0, "fresh_paid_vs_ask_sum": 0.0, "fresh_paid_vs_mid_sum": 0.0})
        c["orders"] += 1
        c[row.status] += 1
        if row.status == ENTRY_FRESH:
            c["fresh_spread_sum"] += row.spread_cents
            c["fresh_paid_vs_ask_sum"] += row.paid_vs_ask_cents
            c["fresh_paid_vs_mid_sum"] += row.paid_vs_mid_cents
            g = self.s["games"].setdefault(family, {}).setdefault(game_digest, [0.0, 0])
            g[0] += row.paid_vs_ask_cents
            g[1] += 1

    def public(self) -> dict:
        self.s["orders_seen"] = sorted(self.seen)
        tot = {k: 0 for k in STATUSES} | {"orders": 0}
        for c in self.s["cells"].values():
            for k in tot:
                tot[k] += c[k]
        return {"schema": "sift-entry-capture-stats/1", "totals": tot, "cells": self.s["cells"], "game_sums": self.s["games"],
                "fresh_seconds": FRESH_SECONDS,
                "rule": "ENTRY_FRESH = same-contract snapshot observed at or before order placement and <= FRESH_SECONDS old; nothing else counts as an entry quote"}
