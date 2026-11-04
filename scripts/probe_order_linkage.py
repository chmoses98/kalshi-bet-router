"""Bounded, GET-only probe: what does Kalshi's order LIST expose for linking an order to its origin?

Counters only. Never prints or writes an order id, client_order_id value, ticker, price, quantity, fee, timestamp,
credential, or any field VALUE. Field NAMES are printed only when they pass ``safe_schema_name`` (lowercase schema
identifiers) and are capped. The join counters are computed in memory over the two samples and only their sizes escape.

This is an availability probe. A matching ticker and time is NEVER evidence of attribution, and nothing here maps any
order to a recommendation.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from typing import Any, Iterable

from kalshi_router.auth import KalshiSigner
from kalshi_router.client import KalshiReadOnlyClient, WalkStats, ORDER_STATUSES
from kalshi_router.config import AuditConfig, base_url_from_env, read_credentials
from kalshi_router.safety import safe_schema_name

SCHEMA = "sift-order-linkage-probe/1"
MAX_SAMPLE = 200
MAX_FIELD_NAMES = 80
SIFT_PREFIX = "sift:"      # the token shape sift_clv.linkage.client_order_token() emits; only its PREFIX is counted

KEYS = (
    "orders_sampled", "order_id_present", "client_order_id_present", "client_order_id_distinct",
    "client_order_id_sift_prefixed", "order_group_id_present", "ticker_present", "created_time_present",
    "status_executed", "status_resting", "status_canceled", "status_other",
    "taker_fill_count_present", "maker_fill_count_present", "taker_fees_present", "maker_fees_present",
    "fills_sampled", "fills_with_order_id", "fills_joined_to_sampled_order",
    "joined_orders_with_client_order_id", "joined_orders_sift_prefixed",
)


def _text(row: dict, key: str) -> str | None:
    v = row.get(key)
    return v.strip() if isinstance(v, str) and v.strip() else None


def _has(row: dict, *names: str) -> bool:
    return any(row.get(n) is not None for n in names)


def summarize(orders: Iterable[Any], fills: Iterable[Any]) -> dict:
    c: Counter[str] = Counter({k: 0 for k in KEYS})
    names: Counter[str] = Counter()
    order_index: dict[str, dict] = {}
    client_ids: set[str] = set()
    for row in orders:
        if not isinstance(row, dict):
            continue
        c["orders_sampled"] += 1
        for k in row:
            safe = safe_schema_name(k)
            if safe:
                names[safe] += 1
        oid, coid = _text(row, "order_id"), _text(row, "client_order_id")
        if oid:
            c["order_id_present"] += 1
            order_index[oid] = row
        if coid:
            c["client_order_id_present"] += 1
            client_ids.add(coid)
            if coid.startswith(SIFT_PREFIX):
                c["client_order_id_sift_prefixed"] += 1
        if _text(row, "order_group_id"):
            c["order_group_id_present"] += 1
        if _text(row, "ticker"):
            c["ticker_present"] += 1
        if _has(row, "created_time", "created_ts"):
            c["created_time_present"] += 1
        st = row.get("status")
        key = f"status_{st}" if st in ORDER_STATUSES else "status_other"
        c[key] += 1
        for field, out in (("taker_fill_count", "taker_fill_count_present"), ("maker_fill_count", "maker_fill_count_present")):
            if _has(row, field, field + "_fp"):
                c[out] += 1
        if _has(row, "taker_fees", "taker_fees_dollars"):
            c["taker_fees_present"] += 1
        if _has(row, "maker_fees", "maker_fees_dollars"):
            c["maker_fees_present"] += 1
    c["client_order_id_distinct"] = len(client_ids)
    for row in fills:
        if not isinstance(row, dict):
            continue
        c["fills_sampled"] += 1
        oid = _text(row, "order_id")
        if not oid:
            continue
        c["fills_with_order_id"] += 1
        match = order_index.get(oid)
        if match is not None:
            c["fills_joined_to_sampled_order"] += 1
            coid = _text(match, "client_order_id")
            if coid:
                c["joined_orders_with_client_order_id"] += 1
                if coid.startswith(SIFT_PREFIX):
                    c["joined_orders_sift_prefixed"] += 1
    return {"counts": dict(c), "order_field_names": dict(names.most_common(MAX_FIELD_NAMES))}


def probe(client: KalshiReadOnlyClient, *, sample: int, status: str | None = None) -> dict:
    if not isinstance(sample, int) or isinstance(sample, bool) or not 1 <= sample <= MAX_SAMPLE:
        raise ValueError("sample must be between 1 and 200")
    ostats, fstats = WalkStats(), WalkStats()
    orders = list(client.iter_orders(sample, page_limit=min(100, sample), stats=ostats, status=status))
    fills = list(client.iter_fills(max_fills=sample, page_limit=min(100, sample), stats=fstats))
    out = summarize(orders, fills)
    return {"schema": SCHEMA, "requested_sample": sample, "status_filter": status or "all",
            "order_pages_read": ostats.pages, "fill_pages_read": fstats.pages,
            "requests_issued": client.request_count, **out,
            "qualification": ("Availability counters over a bounded sample. Not complete history. A join to a sampled order "
                              "shows only that an order id exists; it attributes nothing to a recommendation.")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="GET-only order-linkage availability probe")
    ap.add_argument("--sample", type=int, default=100)
    ap.add_argument("--status", choices=sorted(ORDER_STATUSES))
    a = ap.parse_args(argv)
    if not 1 <= a.sample <= MAX_SAMPLE:
        ap.error("--sample must be between 1 and 200")
    key_id, private_key = read_credentials()
    client = KalshiReadOnlyClient(signer=KalshiSigner(key_id, private_key),
                                  config=AuditConfig(base_url=base_url_from_env(), max_fills=a.sample))
    print(json.dumps(probe(client, sample=a.sample, status=a.status), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:                     # private endpoint errors can echo identifiers: never print them
        print(f"ORDER_LINKAGE_PROBE_FAILED:{type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
