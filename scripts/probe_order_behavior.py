"""Bounded, GET-only, counters-only probe of HOW the account's real orders actually executed (order type, fill completeness,
maker/taker cost split, time from placement to last update), including orders that did NOT fill.

Why: simulated resting-order fill rates are not evidence. The account's own canceled/resting/executed orders are. A
canceled order with zero fill is a real missed fill; its cancel reason is unknown, so it is a bound, never an assumption.

Prints only fixed-vocabulary counters and bucket counts per (status, type). Never prints ids, tickers, prices, quantities, fee
amounts, costs or timestamps. Buckets are coarse so no single order is identifiable.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from kalshi_router.auth import KalshiSigner
from kalshi_router.client import KalshiReadOnlyClient, WalkStats, ORDER_STATUSES
from kalshi_router.config import AuditConfig, base_url_from_env, read_credentials

SCHEMA = "sift-order-behavior-probe/1"
MAX_SAMPLE = 200
TYPES = ("limit", "market")
BUCKETS = (("lt_5s", 5), ("lt_60s", 60), ("lt_10m", 600), ("lt_1h", 3600), ("ge_1h", None))


def _dec(v: Any) -> Decimal | None:
    try:
        return Decimal(str(v)) if v is not None and str(v).strip() != "" else None
    except InvalidOperation:
        return None


def _ts(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def bucket(seconds: float) -> str:
    for name, hi in BUCKETS:
        if hi is None or seconds < hi:
            return name
    return BUCKETS[-1][0]


def summarize(orders: Iterable[Any]) -> dict:
    cells: dict[str, Counter] = {}
    for row in orders:
        if not isinstance(row, dict):
            continue
        status = row.get("status") if row.get("status") in ORDER_STATUSES else "other"
        otype = row.get("type") if row.get("type") in TYPES else "other"
        c = cells.setdefault(f"{status}|{otype}", Counter())
        c["orders"] += 1
        init, filled = _dec(row.get("initial_count_fp")), _dec(row.get("fill_count_fp"))
        if init is None or filled is None or init <= 0:
            c["fill_unknown"] += 1
        elif filled == 0:
            c["zero_fill"] += 1
        elif filled >= init:
            c["fully_filled"] += 1
        else:
            c["partially_filled"] += 1
        maker, taker = _dec(row.get("maker_fill_cost_dollars")), _dec(row.get("taker_fill_cost_dollars"))
        if maker is not None and taker is not None:
            if maker > 0 and taker == 0:
                c["fills_all_maker"] += 1
            elif taker > 0 and maker == 0:
                c["fills_all_taker"] += 1
            elif maker > 0 and taker > 0:
                c["fills_mixed_maker_taker"] += 1
            else:
                c["no_fill_cost"] += 1
        else:
            c["maker_taker_unknown"] += 1
        a, b = _ts(row.get("created_time")), _ts(row.get("last_update_time"))
        if a is not None and b is not None and b >= a:
            c["age_" + bucket((b - a).total_seconds())] += 1
        else:
            c["age_unknown"] += 1
        mf, tf = _dec(row.get("maker_fees_dollars")), _dec(row.get("taker_fees_dollars"))
        if mf is not None and mf > 0:
            c["maker_fee_charged"] += 1
        if tf is not None and tf > 0:
            c["taker_fee_charged"] += 1
    return {k: dict(sorted(v.items())) for k, v in sorted(cells.items())}


def probe(client: KalshiReadOnlyClient, *, sample: int) -> dict:
    if not isinstance(sample, int) or isinstance(sample, bool) or not 1 <= sample <= MAX_SAMPLE:
        raise ValueError("sample must be between 1 and 200")
    stats = {}
    rows: list = []
    for status in sorted(ORDER_STATUSES):
        st = WalkStats()
        rows.extend(client.iter_orders(sample, page_limit=min(100, sample), stats=st, status=status))
        stats[status] = {"pages": st.pages, "truncated": st.truncated, "exhausted": st.exhausted}
    return {"schema": SCHEMA, "sample_per_status": sample, "walk": stats, "requests_issued": client.request_count,
            "cells": summarize(rows),
            "qualification": ("Bounded recent sample per status, not complete history. Canceled/resting counts bound the fill "
                              "probability of the account's own orders; cancel reasons are unknown.")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="GET-only order behaviour probe (counters only)")
    ap.add_argument("--sample", type=int, default=200)
    a = ap.parse_args(argv)
    if not 1 <= a.sample <= MAX_SAMPLE:
        ap.error("--sample must be between 1 and 200")
    key_id, private_key = read_credentials()
    client = KalshiReadOnlyClient(signer=KalshiSigner(key_id, private_key),
                                  config=AuditConfig(base_url=base_url_from_env(), max_fills=a.sample))
    print(json.dumps(probe(client, sample=a.sample), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ORDER_BEHAVIOR_PROBE_FAILED:{type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
