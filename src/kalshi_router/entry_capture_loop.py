"""The prospective collector loop (RESEARCH ONLY, GET-only): fast public top-of-book sampling + order join, aggregates out.

Every ``interval`` seconds it samples ``GET /markets?series_ticker=S&status=open`` for each watched series (public, no account data) into an
in-memory ring, stamping each snapshot with the time the RESPONSE was received (the latest the book state could have been observed, so a
snapshot can only look older, never newer, than it is). Each pass it also reads the account's most recent executed orders and fills (GET)
and joins every NEW order created after this collector started to its entry quote under ``entry_capture.join_order``. Orders created before
the collector started are counted as ``historical_not_capturable`` and never joined to a later book.
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Iterable

from . import entry_capture as EC
from .models import Action, normalize_fill


def _iso(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


class Collector:
    def __init__(self, client, series: Iterable[str], state: dict | None = None, *, now: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], None] = time.sleep, order_limit: int = 100):
        self.client, self.series = client, sorted(set(series))
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.sleep, self.order_limit = sleep, order_limit
        st = state or {}
        # The older collector inadvertently used the YES-axis price when
        # evaluating bought NO contracts. Prior aggregated entry economics
        # cannot be side-corrected because individual rows are intentionally
        # not persisted. Preserve their count as a LEGACY/UNVERIFIED cohort,
        # but never mix them into corrected purchased-leg calculations.
        self.metrics_version = "purchased_leg_v2"
        old = st.get("agg") or {}
        is_legacy = bool(old) and st.get("metrics_version") != self.metrics_version
        self.legacy_unreconciled = st.get("legacy_unreconciled")
        self.ignored_orders = set(st.get("ignored_orders", []))
        if is_legacy:
            self.legacy_unreconciled = {
                "orders": len(old.get("orders_seen", [])),
                "fresh_entry_quotes": sum(int(c.get("ENTRY_FRESH", 0)) for c in old.get("cells", {}).values()),
                "reason": "pre-v2 purchased-side price axis; aggregates withheld from corrected metrics",
            }
            self.ignored_orders.update(old.get("orders_seen", []))
        self.agg = EC.Aggregator(None if is_legacy else st.get("agg"))
        self.ring = EC.Ring()
        self.health = st.get("health") or {"passes": 0, "snapshots": 0, "series_errors": 0, "order_errors": 0, "max_gap_seconds": 0.0,
                                           "historical_not_capturable": 0, "first_pass_at": None, "last_pass_at": None}
        self.historical_seen = set(st.get("historical_seen", []))
        self.started_at = self.now()
        self._last_pass = None

    # ---------------------------------------------------------------- sampling
    def sample_markets(self) -> int:
        n = 0
        for s in self.series:
            try:
                rows = list(self.client.iter_markets(s, max_pages=3))
            except Exception:
                self.health["series_errors"] += 1
                continue
            observed = self.now()                      # after the response: the latest possible observation instant
            for m in rows:
                t = m.get("ticker")
                if not isinstance(t, str):
                    continue
                self.ring.add(t, EC.Snap(observed, EC._dec(m.get("yes_bid_dollars")), EC._dec(m.get("yes_ask_dollars")),
                                         EC._dec(m.get("yes_bid_size_fp")), EC._dec(m.get("yes_ask_size_fp"))))
                n += 1
        self.health["snapshots"] += n
        return n

    # ------------------------------------------------------------------ orders
    def process_orders(self) -> dict:
        out = {"new": 0, "historical": 0}
        try:
            orders = list(self.client.iter_orders(self.order_limit, page_limit=min(100, self.order_limit), status="executed"))
            fills = list(self.client.iter_fills(max_fills=self.order_limit * 3, page_limit=100))
        except Exception:
            self.health["order_errors"] += 1
            return out
        by_order = defaultdict(list)
        for raw in fills:
            try:
                f = normalize_fill(raw)
            except Exception:
                continue
            if f.order_id:
                by_order[f.order_id].append(f)
        for o in orders:
            oid, ticker, placed = o.get("order_id"), o.get("ticker"), _iso(o.get("created_time"))
            if not (isinstance(oid, str) and isinstance(ticker, str) and placed):
                continue
            od = EC.digest("order", oid)
            if self.agg.has(od) or od in self.historical_seen or od in self.ignored_orders:
                continue
            fl = by_order.get(oid)
            if not fl:
                continue                                  # fills not visible yet: retry next pass (nothing recorded)
            if placed < self.started_at:                  # placed before this collector existed: cannot have a prospective snapshot
                self.historical_seen.add(od)
                self.health["historical_not_capturable"] += 1
                out["historical"] += 1
                continue
            # "Purchase price vs entry ASK" is defined only for buys. Do
            # not compare sell proceeds with an ask or silently pool mixed
            # outcome sides. Mark such orders excluded once, never as quotes.
            if any(f.ticker != ticker or f.outcome_side != fl[0].outcome_side for f in fl):
                self.ignored_orders.add(od)
                self.health["mixed_contract_orders_excluded"] = self.health.get("mixed_contract_orders_excluded", 0) + 1
                continue
            if any(f.legacy_action is not Action.BUY for f in fl):
                self.ignored_orders.add(od)
                self.health["non_buy_orders_excluded"] = self.health.get("non_buy_orders_excluded", 0) + 1
                continue
            qty = sum((f.count for f in fl if f.count is not None), Decimal(0))
            if qty <= 0 or any(f.leg_price_dollars is None or f.count is None for f in fl):
                continue
            # price_dollars is Kalshi's YES-axis coordinate even for bought
            # NO; leg_price_dollars is the fee-exclusive price actually paid
            # for the outcome side. The book join works on the latter axis.
            vwap = sum((f.leg_price_dollars * f.count for f in fl), Decimal(0)) / qty
            side = fl[0].outcome_side.value if hasattr(fl[0].outcome_side, "value") else str(fl[0].outcome_side)
            row = EC.join_order(self.ring, ticker=ticker, outcome_side=side, placed_at=placed, vwap=vwap, qty=qty)
            self.agg.add(od, EC.series_of(ticker), EC.digest("game", EC.event_of(ticker)), row)
            out["new"] += 1
        return out

    # -------------------------------------------------------------------- loop
    def one_pass(self) -> dict:
        t0 = self.now()
        n = self.sample_markets()
        o = self.process_orders()
        if self._last_pass is not None:
            self.health["max_gap_seconds"] = max(self.health["max_gap_seconds"], (t0 - self._last_pass).total_seconds())
        self._last_pass = t0
        self.health["passes"] += 1
        self.health["first_pass_at"] = self.health["first_pass_at"] or t0.isoformat(timespec="seconds")
        self.health["last_pass_at"] = t0.isoformat(timespec="seconds")
        return {"snapshots": n, **o}

    def run(self, minutes: float, interval: float, on_pass: Callable[[dict], None] | None = None) -> None:
        end = self.now().timestamp() + minutes * 60
        while self.now().timestamp() < end:
            t = self.now().timestamp()
            res = self.one_pass()
            if on_pass:
                on_pass(res)
            self.sleep(max(0.0, interval - (self.now().timestamp() - t)))

    def state(self) -> dict:
        pub = self.agg.public()
        return {"agg": self.agg.s, "health": self.health, "historical_seen": sorted(self.historical_seen),
                "metrics_version": self.metrics_version, "ignored_orders": sorted(self.ignored_orders),
                "legacy_unreconciled": self.legacy_unreconciled}

    def public(self) -> dict:
        d = self.agg.public()
        d["sampler"] = {**self.health, "series_watched": len(self.series), "tickers_in_ring": self.ring.tickers()}
        d["metrics_version"] = self.metrics_version
        d["legacy_unreconciled"] = self.legacy_unreconciled
        return d


def load_state(path: str) -> dict:
    return json.load(open(path)) if os.path.exists(path) else {}


def save_atomic(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, sort_keys=True, separators=(",", ":"))
    os.replace(tmp, path)
