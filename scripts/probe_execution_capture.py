"""Bounded read-only probe for the *availability* of execution evidence.

Never prints or writes raw fills, tickers, account identifiers, prices, quantities,
fees, timestamps, credentials, schema field values, or individual order metadata.
Only known aggregate counters may reach GitHub Actions logs.

This is an availability probe, NOT evidence that a SIFT recommendation caused
a wager, nor proof that the Exchange's orders API exposes client_order_id.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from typing import Iterable, Any

from kalshi_router.auth import KalshiSigner
from kalshi_router.client import KalshiReadOnlyClient, WalkStats
from kalshi_router.config import AuditConfig, base_url_from_env, read_credentials
from kalshi_router.errors import SchemaError
from kalshi_router.models import normalize_fill

SCHEMA = "sift-execution-availability-probe/1"
MAX_SAMPLE = 200

# The output is a fixed vocabulary of counters, never a dynamic payload key.
KEYS = (
    "sample_rows", "normalized", "rejected", "order_id_present",
    "fill_id_present", "trade_id_present", "client_order_id_present",
    "explicit_taker", "explicit_maker", "taker_unknown",
    "exchange_fee_present", "exchange_fee_missing", "exchange_fee_zero",
    "raw_fill_price_present", "fill_timestamp_present",
    "entry_yes_bid_and_ask_present",
)


def summarize_fills(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter({key: 0 for key in KEYS})
    for row in rows:
        counts["sample_rows"] += 1
        if not isinstance(row, dict):
            counts["rejected"] += 1
            continue
        # Presence is not evidence that a field contains a valid association.
        for field, key in (
            ("order_id", "order_id_present"),
            ("fill_id", "fill_id_present"),
            ("trade_id", "trade_id_present"),
            ("client_order_id", "client_order_id_present"),
        ):
            if isinstance(row.get(field), str) and bool(row[field].strip()):
                counts[key] += 1
        if (row.get("yes_bid_dollars") is not None and
                row.get("yes_ask_dollars") is not None):
            counts["entry_yes_bid_and_ask_present"] += 1
        if isinstance(row.get("created_time"), str) or (
                isinstance(row.get("ts"), int) and not isinstance(row["ts"], bool)):
            counts["fill_timestamp_present"] += 1
        try:
            fill = normalize_fill(row)
        except (SchemaError, ValueError, TypeError):
            counts["rejected"] += 1
            continue
        counts["normalized"] += 1
        if fill.is_taker is True:
            counts["explicit_taker"] += 1
        elif fill.is_taker is False:
            counts["explicit_maker"] += 1
        else:
            counts["taker_unknown"] += 1
        if fill.fee_dollars is None:
            counts["exchange_fee_missing"] += 1
        else:
            counts["exchange_fee_present"] += 1
            if fill.fee_dollars == 0:
                counts["exchange_fee_zero"] += 1
        if fill.price_dollars is not None:
            counts["raw_fill_price_present"] += 1
    return dict(counts)


def probe(client: KalshiReadOnlyClient, *, sample: int, historical: bool = False) -> dict:
    if not isinstance(sample, int) or isinstance(sample, bool) or not 1 <= sample <= MAX_SAMPLE:
        raise ValueError("sample must be between 1 and 200")
    stats = WalkStats()
    # Only /portfolio/fills or /historical/fills, both GET-allowlisted.
    it = client.iter_historical_fills if historical else client.iter_fills
    counters = summarize_fills(it(max_fills=sample, page_limit=min(100, sample), stats=stats))
    return {
        "schema": SCHEMA,
        "source": "historical_fills" if historical else "recent_fills",
        "requested_max_fills": sample,
        "pages_read": stats.pages,
        "truncated": stats.truncated,
        "exhausted": stats.exhausted,
        "counts": counters,
        "qualification": (
            "Availability counts only. No order API, recommendation mapping, "
            "entry-time book, or maker fee schedule verified."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only aggregate execution availability probe")
    parser.add_argument("--max-fills", type=int, default=100)
    parser.add_argument("--historical", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.max_fills <= MAX_SAMPLE:
        parser.error("--max-fills must be between 1 and 200")
    key_id, private_key = read_credentials()
    client = KalshiReadOnlyClient(
        signer=KalshiSigner(key_id, private_key),
        config=AuditConfig(base_url=base_url_from_env(), max_fills=args.max_fills),
    )
    result = probe(client, sample=args.max_fills, historical=args.historical)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Errors from private portfolio endpoints may include identifiers.
        # Never echo exception messages, response bodies, or credentials.
        print(f"EXECUTION_PROBE_FAILED:{type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
