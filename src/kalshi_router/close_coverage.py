"""How many of the account's traded markets have a TRUE close, a proxy, or none -- as COUNTS ONLY.

Read-only: it walks the account's fills for the set of markets, then asks the public market/event/candlestick routes
for each one. It writes nothing, uploads nothing, and prints no ticker, price, time or id: the report has no field that can
hold a string (a test asserts it), the same structural rule as ``aggregate.AuditReport``.

The scheduled start (the cutoff) is taken from the EVENT's ``strike_date`` when the exchange states one, and from
nowhere else. A market whose event states none is counted ``no_cutoff`` and gets no close: a cutoff is never inferred
from a ticker, a date in a ticker, or a settlement time.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any

from .closes import EARLIER_QUOTE_PROXY, MISSING, TRUE_CLOSE, fetch_and_resolve
from .errors import KalshiRouterError


@dataclass
class CloseCoverage:
    markets_seen: int = 0
    markets_considered: int = 0
    true_close: int = 0
    earlier_quote_proxy: int = 0
    missing: int = 0
    no_cutoff: int = 0
    errors: int = 0
    true_close_wide_spread: int = 0
    truncated: int = 0

    def as_dict(self) -> dict[str, int]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def _epoch(value: Any) -> int | None:
    if not isinstance(value, str) or "T" not in value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(dt.timestamp()) if dt.tzinfo is not None else None


def run_close_coverage(client: Any, *, max_fills: int, prefixes: tuple[str, ...] = (), max_markets: int = 400) -> CloseCoverage:
    """Bounded: at most ``max_fills`` fills are read and at most ``max_markets`` markets resolved."""
    out = CloseCoverage()
    tickers: list[str] = []
    seen: set[str] = set()
    for fill in client.iter_fills(max_fills=max_fills):
        t = fill.get("ticker") or fill.get("market_ticker")
        if isinstance(t, str) and t not in seen:
            seen.add(t)
            tickers.append(t)
    out.markets_seen = len(tickers)
    considered = [t for t in tickers if not prefixes or any(t.startswith(p) for p in prefixes)]
    if len(considered) > max_markets:
        out.truncated = 1
        considered = considered[:max_markets]
    for ticker in considered:
        out.markets_considered += 1
        try:
            market = client.get_market(ticker)
            series = market.get("series_ticker") or ticker.split("-")[0]
            event_ticker = market.get("event_ticker")
            cutoff = None
            if isinstance(event_ticker, str):
                cutoff = _epoch(client.get_event(event_ticker).get("strike_date"))
            if cutoff is None:
                out.no_cutoff += 1
                continue
            res = fetch_and_resolve(client, series_ticker=series, ticker=ticker, cutoff_ts=cutoff)
        except KalshiRouterError:
            out.errors += 1
            continue
        if res.quality == TRUE_CLOSE:
            out.true_close += 1
            out.true_close_wide_spread += int("WIDE_SPREAD" in res.flags)
        elif res.quality == EARLIER_QUOTE_PROXY:
            out.earlier_quote_proxy += 1
        else:
            assert res.quality == MISSING
            out.missing += 1
    return out
