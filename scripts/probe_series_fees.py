"""Bounded, read-only probe: what fee schedule does Kalshi PUBLISH for each series we trade, and do our own fills agree?

For each series seen in a bounded sample of recent fills it reads the PUBLIC catalogue entry (``GET /series/{ticker}``:
``fee_type``, ``fee_multiplier`` and any other fee-named field) and compares it with the exchange-reported fees of our own
fills in that series (implied rate = fee / (count * p * (1 - p))).

Output: per series ticker (public catalogue name) the published fee fields and counters. Never prints a fill/order id, price,
quantity, fee amount, timestamp or credential. Maker fees are reported as OBSERVED or UNOBSERVED; an unobserved maker rate is
never filled in with an assumed one.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from decimal import Decimal

from kalshi_router.auth import KalshiSigner
from kalshi_router.client import KalshiReadOnlyClient, WalkStats
from kalshi_router.config import AuditConfig, base_url_from_env, read_credentials
from kalshi_router.errors import SchemaError
from kalshi_router.models import normalize_fill
from kalshi_router.safety import safe_schema_name

SCHEMA = "sift-series-fee-probe/1"
MAX_FILLS, MAX_SERIES = 200, 60
_FEE_KEYS = ("fee_type", "fee_multiplier")


def series_of(ticker: str) -> str:
    return ticker.split("-", 1)[0]


def published(series_obj: dict) -> dict:
    """Public fee fields only: fixed names plus any other key whose (safe) name contains 'fee'; values must be scalars."""
    out = {}
    for k, v in series_obj.items():
        name = safe_schema_name(k)
        if name and "fee" in name and isinstance(v, (str, int, float, bool)) and len(str(v)) <= 40:
            out[name] = v
    return out


def implied(rows) -> dict:
    taker, maker, unknown, rates_t, rates_m = 0, 0, 0, [], []
    for f in rows:
        if f.fee_dollars is None or f.count in (None, 0) or f.price_dollars is None or not (0 < f.price_dollars < 1):
            continue
        rate = float(f.fee_dollars / (f.count * f.price_dollars * (1 - f.price_dollars)))
        if f.is_taker is True:
            taker += 1; rates_t.append(rate)
        elif f.is_taker is False:
            maker += 1; rates_m.append(rate)
        else:
            unknown += 1
    med = lambda xs: None if not xs else round(statistics.median(xs), 5)        # noqa: E731
    return {"taker_fills": taker, "maker_fills": maker, "taker_unknown": unknown,
            "implied_taker_rate_median": med(rates_t), "implied_maker_rate_median": med(rates_m),
            "maker_rate": "OBSERVED" if maker else "UNOBSERVED"}


def probe(client: KalshiReadOnlyClient, *, sample: int) -> dict:
    if not isinstance(sample, int) or isinstance(sample, bool) or not 1 <= sample <= MAX_FILLS:
        raise ValueError("sample must be between 1 and 200")
    stats = WalkStats()
    by: dict[str, list] = defaultdict(list)
    for raw in client.iter_fills(max_fills=sample, page_limit=min(100, sample), stats=stats):
        try:
            f = normalize_fill(raw)
        except (SchemaError, ValueError, TypeError):
            continue
        by[series_of(f.ticker)].append(f)
    out, unreadable = {}, 0
    for s in sorted(by, key=lambda k: -len(by[k]))[:MAX_SERIES]:
        try:
            pub = published(client.get_series(s))
        except Exception:
            pub, unreadable = {"series_catalogue": "UNREADABLE"}, unreadable + 1
        out[s] = {"fills": len(by[s]), "published": pub, **implied(by[s])}
    return {"schema": SCHEMA, "requested_fills": sample, "pages_read": stats.pages, "series_seen": len(by),
            "series_reported": len(out), "series_unreadable": unreadable, "requests_issued": client.request_count,
            "by_series": out,
            "qualification": "Published fee fields are the exchange's own catalogue; observed rates come from a bounded sample."}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=200)
    a = ap.parse_args(argv)
    if not 1 <= a.sample <= MAX_FILLS:
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
        print(f"SERIES_FEE_PROBE_FAILED:{type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
