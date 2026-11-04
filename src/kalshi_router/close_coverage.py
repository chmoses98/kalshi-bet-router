"""How many of the account's traded markets have a TRUE close, a proxy, or none -- as COUNTS ONLY.

Read-only: it walks the account's fills for the set of markets, then asks the public market/event/candlestick routes
for each one. It writes nothing, uploads nothing, and prints no ticker, price, time or id: the report has no field that can
hold a string (a test asserts it), the same structural rule as ``aggregate.AuditReport``.

The scheduled start (the cutoff) comes from exactly two EXCHANGE-STATED sources, tried in this order, and from nowhere
else:

``EXCHANGE_STRIKE_DATE``      the EVENT's ``strike_date``.
``EXCHANGE_MILESTONE_START``  the ``start_date`` of the public football-game milestone that names the market's event
                              (``primary_event_tickers`` / ``related_event_tickers``), only for a milestone whose
                              ``details.status`` is a scheduled/started/finished one (never ``time-tbd``/``created``)
                              and whose date is a plausible timezone-qualified instant.

Recorded evidence behind the choice (aggregate, from captured catalogs): no captured CFB event carries a
``strike_date`` (0 of 906), market ``occurrence_datetime`` / ``expected_expiration_time`` are the NOMINAL END (about
kickoff + 3h, varying by game), and ``close_time`` is days later -- none of those is a start and none is used. The
milestone ``start_date`` equalled the repository schedule kickoff in 115 of 116 name-matched games.

A market with no stated start, two disagreeing stated starts, or an event named by an untrusted milestone is counted
``no_cutoff`` (or ``cutoff_conflict``) and gets no close: a cutoff is never inferred from a ticker, a date in a
ticker, a nominal expiration or a settlement time. TRUE_CLOSE semantics are those of ``closes.resolve_close``, unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any

from .closes import EARLIER_QUOTE_PROXY, MISSING, TRUE_CLOSE, fetch_and_resolve
from .errors import KalshiRouterError
from .milestones import MilestoneStartIndex, build_milestone_start_index

EXCHANGE_STRIKE_DATE = "EXCHANGE_STRIKE_DATE"
EXCHANGE_MILESTONE_START = "EXCHANGE_MILESTONE_START"


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
    #: cutoff provenance of the markets that got one (these sum to the markets that were resolved + errors after cutoff)
    cutoff_from_strike_date: int = 0
    cutoff_from_milestone_start: int = 0
    #: strike_date and milestone start both stated and different -> no cutoff
    cutoff_conflict: int = 0
    #: milestone sweep health (0/1 flags and a request count)
    milestone_sweep_requests: int = 0
    milestone_sweep_budget_exhausted: int = 0
    milestone_sweep_failed: int = 0

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


def run_close_coverage(client: Any, *, max_fills: int, prefixes: tuple[str, ...] = (), max_markets: int = 400,
                       use_milestone_start: bool = True) -> CloseCoverage:
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
    starts: MilestoneStartIndex | None = None
    for ticker in considered:
        out.markets_considered += 1
        try:
            market = client.get_market(ticker)
            series = market.get("series_ticker") or ticker.split("-")[0]
            event_ticker = market.get("event_ticker")
            cutoff, source = None, None
            if isinstance(event_ticker, str):
                cutoff = _epoch(client.get_event(event_ticker).get("strike_date"))
                if cutoff is not None:
                    source = EXCHANGE_STRIKE_DATE
                if use_milestone_start:
                    if starts is None:  # one public sweep, only if some market needs it or can be cross-checked
                        starts = build_milestone_start_index(client)
                        out.milestone_sweep_requests = starts.requests_issued
                        out.milestone_sweep_budget_exhausted = int(starts.budget_exhausted)
                        out.milestone_sweep_failed = int(starts.fetch_failed)
                    stated = starts.start_for_event(event_ticker)
                    if cutoff is None and stated is not None:
                        cutoff, source = stated, EXCHANGE_MILESTONE_START
                    elif cutoff is not None and stated is not None and stated != cutoff:
                        out.cutoff_conflict += 1
                        out.no_cutoff += 1
                        continue
            if cutoff is None:
                out.no_cutoff += 1
                continue
            if source == EXCHANGE_STRIKE_DATE:
                out.cutoff_from_strike_date += 1
            else:
                out.cutoff_from_milestone_start += 1
            res = fetch_and_resolve(client, series_ticker=series, ticker=ticker, cutoff_ts=cutoff,
                                    cutoff_source=source)
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
