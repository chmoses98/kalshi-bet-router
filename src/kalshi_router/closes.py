"""Closing prices from Kalshi candlesticks, with an explicit TRUE_CLOSE / EARLIER_QUOTE_PROXY / MISSING label.

WHY THIS EXISTS
---------------
CLV and every "was the price good" question need the price at the close. Until now the only thing available was the
last catalog capture before kickoff -- sometimes 7 hours early -- and it was easy to read as a close. This module
resolves a close from the exchange's own candlesticks and says HOW CLOSE the answer is:

``TRUE_CLOSE``           the last candle ending at or before the scheduled start, no longer than ``max_gap_seconds``
                         before it, from a candle of at most 60 minutes.
``EARLIER_QUOTE_PROXY``  the last candle we could find before the cutoff, too early to call a close. Recorded with how
                         many minutes early it was. It is NEVER labelled a close.
``MISSING``              no usable candle (none, crossed quote, no cutoff supplied). The reason is recorded.

The cutoff (scheduled start) is an INPUT. It is never guessed: a caller that cannot supply it gets ``MISSING``.

RESPONSE SHAPE -- READ THIS
---------------------------
The parser follows Kalshi's published candlestick schema (``end_period_ts``; ``yes_bid`` / ``yes_ask`` / ``price``
objects of ``open``/``high``/``low``/``close``; the fixed-point era adds ``*_dollars`` decimal strings). It was written
without live access to the endpoint (the build container's egress policy denies the Kalshi host), so the fixtures are
reproductions of the DOCUMENTED shape, not captured payloads. Anything that does not match is REFUSED with a
``SchemaError`` rather than coerced: a shape drift must fail loudly, and the first aggregate run of
``resolve-closes.yml`` is the live verification.

Prices are exact ``Decimal`` until the final row, where they are emitted as JSON numbers in dollars. Raw payloads are
never logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .errors import SchemaError
from .fixedpoint import parse_fixed_point

TRUE_CLOSE = "TRUE_CLOSE"
EARLIER_QUOTE_PROXY = "EARLIER_QUOTE_PROXY"
MISSING = "MISSING"

DEFAULT_MAX_GAP_SECONDS = 15 * 60
DEFAULT_MAX_SPREAD = Decimal("0.10")
TRUE_CLOSE_MAX_PERIOD_MINUTES = 60
ALLOWED_PERIODS = (1, 60, 1440)
MAX_CANDLES = 5000

_LIVE = "/series/{series}/markets/{ticker}/candlesticks"
_HISTORICAL = "/historical/markets/{ticker}/candlesticks"


@dataclass(frozen=True)
class Candle:
    end_ts: int
    yes_bid_close: Decimal | None
    yes_ask_close: Decimal | None
    trade_close: Decimal | None
    volume: Decimal | None


def _px(obj: Any, name: str, op: str) -> Decimal | None:
    """The CLOSE of one OHLC object as dollars, preferring ``close_dollars`` (exact string) over legacy cents."""
    if obj is None:
        return None
    if not isinstance(obj, dict):
        raise SchemaError(f"{op}: {name} was not an object")
    dollars = parse_fixed_point(obj.get("close_dollars"), f"{name}.close_dollars")
    if dollars is not None:
        value = dollars.value
    else:
        cents = obj.get("close")
        if cents is None:
            return None
        if isinstance(cents, bool) or not isinstance(cents, int):
            raise SchemaError(f"{op}: {name}.close was not an integer number of cents")
        value = Decimal(cents) / Decimal(100)
    if not (Decimal(0) <= value <= Decimal(1)):
        raise SchemaError(f"{op}: {name} close outside [0, 1] dollars")
    return value


def parse_candlesticks(payload: dict[str, Any], operation: str) -> list[Candle]:
    """Strictly parse a candlestick response. Candles come back sorted by ``end_ts``; duplicates are refused."""
    rows = payload.get("candlesticks")
    if not isinstance(rows, list):
        raise SchemaError(f"{operation}: response had no 'candlesticks' list")
    if len(rows) > MAX_CANDLES:
        raise SchemaError(f"{operation}: response exceeded {MAX_CANDLES} candles")
    out: list[Candle] = []
    for row in rows:
        if not isinstance(row, dict):
            raise SchemaError(f"{operation}: a candle was not an object")
        ts = row.get("end_period_ts")
        if isinstance(ts, bool) or not isinstance(ts, int) or ts <= 0:
            raise SchemaError(f"{operation}: a candle had no integer end_period_ts")
        vol = parse_fixed_point(row.get("volume_fp") if row.get("volume_fp") is not None else row.get("volume"),
                                "volume")
        out.append(Candle(
            end_ts=ts,
            yes_bid_close=_px(row.get("yes_bid"), "yes_bid", operation),
            yes_ask_close=_px(row.get("yes_ask"), "yes_ask", operation),
            trade_close=_px(row.get("price"), "price", operation),
            volume=None if vol is None else vol.value,
        ))
    out.sort(key=lambda c: c.end_ts)
    if len({c.end_ts for c in out}) != len(out):
        raise SchemaError(f"{operation}: duplicate candle end_period_ts")
    return out


@dataclass(frozen=True)
class ResolvedClose:
    quality: str
    cutoff_ts: int | None
    period_minutes: int | None
    source_endpoint: str | None
    as_of_ts: int | None = None
    yes_bid: Decimal | None = None
    yes_ask: Decimal | None = None
    last_trade: Decimal | None = None
    minutes_before_cutoff: int | None = None
    flags: tuple[str, ...] = ()
    reason: str | None = None

    @property
    def yes_mid(self) -> Decimal | None:
        if self.yes_bid is not None and self.yes_ask is not None:
            return (self.yes_bid + self.yes_ask) / 2
        return None

    def to_close_body(self) -> dict[str, Any]:
        """The ``close`` object of a ``CLOSE`` evidence row (prices as JSON numbers, dollars)."""
        if self.quality == MISSING:
            return {"quality": MISSING, "reason": self.reason or "UNSPECIFIED"}

        def num(d: Decimal | None) -> float | None:
            return None if d is None else float(d)

        body: dict[str, Any] = {
            "quality": self.quality, "yes_bid": num(self.yes_bid), "yes_ask": num(self.yes_ask),
            "yes_mid": num(self.yes_mid), "last_trade": num(self.last_trade), "as_of_ts": self.as_of_ts,
            "cutoff_ts": self.cutoff_ts, "period_minutes": self.period_minutes,
            "minutes_before_cutoff": self.minutes_before_cutoff, "source_endpoint": self.source_endpoint,
            "flags": list(self.flags),
        }
        return {k: v for k, v in body.items() if v is not None or k in ("flags",)}


def missing(reason: str, cutoff_ts: int | None = None) -> ResolvedClose:
    return ResolvedClose(MISSING, cutoff_ts, None, None, reason=reason)


def resolve_close(
    candles: list[Candle],
    *,
    cutoff_ts: int | None,
    period_minutes: int,
    source_endpoint: str,
    max_gap_seconds: int = DEFAULT_MAX_GAP_SECONDS,
    max_spread: Decimal = DEFAULT_MAX_SPREAD,
) -> ResolvedClose:
    """Label the best candle at or before ``cutoff_ts``. Pure."""
    if cutoff_ts is None:
        return missing("NO_CUTOFF_SUPPLIED")
    if period_minutes not in ALLOWED_PERIODS:
        return missing("UNSUPPORTED_PERIOD", cutoff_ts)
    usable = [c for c in candles if c.end_ts <= cutoff_ts and (
        (c.yes_bid_close is not None and c.yes_ask_close is not None) or c.trade_close is not None)]
    if not usable:
        return missing("NO_CANDLE_AT_OR_BEFORE_CUTOFF", cutoff_ts)
    last = usable[-1]
    bid, ask = last.yes_bid_close, last.yes_ask_close
    flags: list[str] = []
    if bid is not None and ask is not None:
        if ask < bid:
            return missing("CROSSED_QUOTE", cutoff_ts)
        if ask - bid > max_spread:
            flags.append("WIDE_SPREAD")
    if last.volume is not None and last.volume == 0:
        flags.append("NO_VOLUME_IN_CANDLE")
    gap = cutoff_ts - last.end_ts
    near = gap <= max_gap_seconds and period_minutes <= TRUE_CLOSE_MAX_PERIOD_MINUTES
    if not near:
        flags.append("PERIOD_TOO_LONG_FOR_CLOSE" if period_minutes > TRUE_CLOSE_MAX_PERIOD_MINUTES
                     else "TOO_EARLY_FOR_CLOSE")
    return ResolvedClose(
        quality=TRUE_CLOSE if near else EARLIER_QUOTE_PROXY, cutoff_ts=cutoff_ts, period_minutes=period_minutes,
        source_endpoint=source_endpoint, as_of_ts=last.end_ts, yes_bid=bid, yes_ask=ask, last_trade=last.trade_close,
        minutes_before_cutoff=gap // 60, flags=tuple(flags),
    )


def endpoint_name(historical: bool) -> str:
    return "historical_candlesticks" if historical else "series_market_candlesticks"


def fetch_and_resolve(client: Any, *, series_ticker: str, ticker: str, cutoff_ts: int | None,
                      lookback_minutes: int = 120, wide_lookback_hours: int = 48,
                      allow_historical: bool = True, **resolve_kwargs: Any) -> ResolvedClose:
    """Two-tier fetch: 1-minute candles over the last ``lookback_minutes``, then 60-minute candles over
    ``wide_lookback_hours``; the live route first and the historical route only when allowed and the live one is
    empty. Bounded: a handful of requests per market, never a walk."""
    if cutoff_ts is None:
        return missing("NO_CUTOFF_SUPPLIED")
    tiers = ((1, lookback_minutes * 60), (60, wide_lookback_hours * 3600))
    best: ResolvedClose | None = None
    for period, span in tiers:
        for historical in ((False, True) if allow_historical else (False,)):
            candles = client.get_market_candlesticks(
                series_ticker=series_ticker, ticker=ticker, start_ts=cutoff_ts - span, end_ts=cutoff_ts,
                period_interval=period, historical=historical)
            got = resolve_close(candles, cutoff_ts=cutoff_ts, period_minutes=period,
                                source_endpoint=endpoint_name(historical), **resolve_kwargs)
            if got.quality == TRUE_CLOSE:
                return got
            if got.quality == EARLIER_QUOTE_PROXY and (best is None or got.as_of_ts > (best.as_of_ts or 0)):
                best = got
            if candles:
                break  # the live route answered; do not also ask the historical one
    return best or missing("NO_CANDLE_AT_OR_BEFORE_CUTOFF", cutoff_ts)
