"""Run the prospective entry-market collector (GET-only). Aggregates only reach stdout/state; see kalshi_router.entry_capture.

    entry_capture.py --state state.json --public stats.json --minutes 330 --interval 20 [--series-from-fills N | --series A,B,...]
"""
from __future__ import annotations

import argparse
import json
import sys

from kalshi_router.auth import KalshiSigner
from kalshi_router.client import KalshiReadOnlyClient
from kalshi_router.config import AuditConfig, base_url_from_env, read_credentials
from kalshi_router.entry_capture_loop import Collector, load_state, save_atomic
from kalshi_router.models import normalize_fill


def series_from_fills(client, n):
    out = set()
    for raw in client.iter_fills(max_fills=n, page_limit=min(100, n)):
        try:
            out.add(normalize_fill(raw).ticker.split("-", 1)[0])
        except Exception:
            continue
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True); ap.add_argument("--public", required=True)
    ap.add_argument("--minutes", type=float, default=5.0); ap.add_argument("--interval", type=float, default=20.0)
    ap.add_argument("--series", default=""); ap.add_argument("--series-from-fills", type=int, default=200)
    ap.add_argument("--extra-series", default="KXNFLGAME,KXNFLSPREAD,KXNFLTOTAL,KXNFLTEAMTOTAL,KXNBAGAME,KXNBASPREAD,KXNBATOTAL,KXNCAAFGAME,KXNCAAFSPREAD,KXNCAAFTOTAL,KXNHLGAME")
    ap.add_argument("--max-series", type=int, default=60)
    a = ap.parse_args(argv)
    if not 0 < a.minutes <= 340 or a.interval > 600:
        ap.error("minutes must be in (0, 340] and interval at most 600 s (values under 20 s are raised to 20 s)")
    key_id, private_key = read_credentials()
    client = KalshiReadOnlyClient(signer=KalshiSigner(key_id, private_key), config=AuditConfig(base_url=base_url_from_env(), max_fills=300))
    series = set(filter(None, a.series.split(","))) if a.series else set()
    series |= set(filter(None, a.extra_series.split(",")))
    if a.series_from_fills:
        series |= series_from_fills(client, a.series_from_fills)
    series = sorted(series)[: a.max_series]
    col = Collector(client, series, load_state(a.state))
    pub_every = {"n": 0}

    def on_pass(res):
        pub_every["n"] += 1
        if pub_every["n"] % 6 == 0:
            save_atomic(a.state, col.state()); save_atomic(a.public, col.public())
    col.run(a.minutes, max(20.0, a.interval), on_pass)
    save_atomic(a.state, col.state()); save_atomic(a.public, col.public())
    p = col.public()
    print(json.dumps({"series_watched": len(series), "totals": p["totals"], "sampler": p["sampler"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ENTRY_CAPTURE_FAILED:{type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
