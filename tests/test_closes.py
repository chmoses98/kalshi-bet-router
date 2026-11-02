"""Candlestick parsing, close labelling, and the read-only client method. Fixtures reproduce the DOCUMENTED shape."""

from __future__ import annotations

from decimal import Decimal

import pytest

from kalshi_router import closes
from kalshi_router.client import KalshiReadOnlyClient, READ_ONLY_PATH_PREFIXES, _assert_read_only
from kalshi_router.errors import SchemaError

CUTOFF = 1_790_000_000


def candle(end, bid=None, ask=None, trade=None, vol="10", dollars=True):
    def ohlc(v):
        if v is None:
            return None
        return {"close_dollars": v} if dollars else {"close": int(round(float(v) * 100))}
    row = {"end_period_ts": end, "yes_bid": ohlc(bid), "yes_ask": ohlc(ask), "price": ohlc(trade)}
    if vol is not None:
        row["volume_fp"] = vol
    return row


def parse(rows):
    return closes.parse_candlesticks({"candlesticks": rows}, "t")


# --------------------------------------------------------------------------------------------- parsing

def test_dollar_strings_parse_exactly_and_legacy_cents_still_parse():
    a = parse([candle(CUTOFF - 60, "0.4800", "0.5200", "0.5000")])[0]
    assert (a.yes_bid_close, a.yes_ask_close, a.trade_close) == (Decimal("0.4800"), Decimal("0.5200"), Decimal("0.5000"))
    b = parse([candle(CUTOFF - 60, "0.48", "0.52", None, dollars=False)])[0]
    assert (b.yes_bid_close, b.yes_ask_close, b.trade_close) == (Decimal("0.48"), Decimal("0.52"), None)


@pytest.mark.parametrize("payload", [
    {}, {"candlesticks": "x"}, {"candlesticks": [1]},
    {"candlesticks": [{"end_period_ts": "123"}]}, {"candlesticks": [{"end_period_ts": True}]},
    {"candlesticks": [{"end_period_ts": 5, "yes_bid": {"close_dollars": "1.50"}}]},
    {"candlesticks": [{"end_period_ts": 5, "yes_bid": {"close": 4.5}}]},
    {"candlesticks": [{"end_period_ts": 5, "yes_bid": "0.4"}]},
    {"candlesticks": [{"end_period_ts": 5}, {"end_period_ts": 5}]},
])
def test_malformed_responses_are_refused_not_coerced(payload):
    with pytest.raises(SchemaError):
        closes.parse_candlesticks(payload, "t")


def test_candles_come_back_sorted():
    got = parse([candle(CUTOFF - 60, "0.4", "0.5"), candle(CUTOFF - 180, "0.4", "0.5")])
    assert [c.end_ts for c in got] == [CUTOFF - 180, CUTOFF - 60]


# ------------------------------------------------------------------------------------------- labelling

def resolve(rows, *, cutoff=CUTOFF, period=1, **kw):
    return closes.resolve_close(parse(rows), cutoff_ts=cutoff, period_minutes=period,
                                source_endpoint="series_market_candlesticks", **kw)


def test_a_final_minute_quote_is_a_true_close():
    got = resolve([candle(CUTOFF - 60, "0.48", "0.52", "0.50")])
    assert got.quality == closes.TRUE_CLOSE and got.yes_mid == Decimal("0.50")
    assert got.minutes_before_cutoff == 1 and got.flags == ()


def test_an_early_quote_is_a_proxy_and_never_a_close():
    got = resolve([candle(CUTOFF - 3 * 3600, "0.48", "0.52")])
    assert got.quality == closes.EARLIER_QUOTE_PROXY
    assert got.minutes_before_cutoff == 180 and "TOO_EARLY_FOR_CLOSE" in got.flags


def test_an_hourly_candle_near_the_start_is_still_labelled_by_its_age_and_period():
    got = resolve([candle(CUTOFF - 300, "0.48", "0.52")], period=60)
    assert got.quality == closes.TRUE_CLOSE  # 60 minutes is the longest period that may qualify
    daily = resolve([candle(CUTOFF - 300, "0.48", "0.52")], period=1440)
    assert daily.quality == closes.EARLIER_QUOTE_PROXY and "PERIOD_TOO_LONG_FOR_CLOSE" in daily.flags


def test_candles_after_the_cutoff_are_never_used():
    got = resolve([candle(CUTOFF + 60, "0.90", "0.95"), candle(CUTOFF - 600, "0.48", "0.52")])
    assert got.yes_bid == Decimal("0.48") and got.as_of_ts == CUTOFF - 600


def test_no_candle_no_cutoff_and_a_crossed_quote_are_missing_with_a_reason():
    assert resolve([]).reason == "NO_CANDLE_AT_OR_BEFORE_CUTOFF"
    assert resolve([candle(CUTOFF + 5, "0.4", "0.5")]).quality == closes.MISSING
    assert resolve([candle(CUTOFF - 60, "0.4", "0.5")], cutoff=None).reason == "NO_CUTOFF_SUPPLIED"
    assert resolve([candle(CUTOFF - 60, "0.60", "0.40")]).reason == "CROSSED_QUOTE"
    assert resolve([candle(CUTOFF - 60, "0.4", "0.5")], period=7).reason == "UNSUPPORTED_PERIOD"


def test_a_wide_final_quote_is_still_the_close_but_flagged():
    got = resolve([candle(CUTOFF - 60, "0.02", "0.98")])
    assert got.quality == closes.TRUE_CLOSE and "WIDE_SPREAD" in got.flags


def test_a_last_trade_alone_can_be_the_close_without_inventing_a_quote():
    got = resolve([candle(CUTOFF - 60, None, None, "0.55")])
    assert got.quality == closes.TRUE_CLOSE and got.yes_bid is None and got.yes_mid is None
    assert got.last_trade == Decimal("0.55")


def test_close_body_is_valid_for_the_evidence_ledger():
    import importlib.util, sys
    from pathlib import Path
    mod = Path(__file__).resolve().parents[1] / "contract" / "standalone" / "execution_evidence_ledger.py"
    spec = importlib.util.spec_from_file_location("eel", mod)
    L = importlib.util.module_from_spec(spec)
    sys.modules["eel"] = L
    spec.loader.exec_module(L)
    for res in (resolve([candle(CUTOFF - 60, "0.48", "0.52")]),
                resolve([candle(CUTOFF - 3 * 3600, "0.48", "0.52")]),
                resolve([])):
        row = {"schema_version": L.SCHEMA_VERSION, "kind": "CLOSE", "producer": "router",
               "source_bet_key": "kalshi:v1:" + "ab" * 32, "market_ticker": "T-1", "side": "YES",
               "close": res.to_close_body()}
        assert L.validate_row(row) == [], res.quality


# ------------------------------------------------------------------------------------------- the client

class FakeClient(KalshiReadOnlyClient):
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def _get(self, path, operation, params=None):
        self.calls.append((path, dict(params or {})))
        out = self.responses(path, params)
        return out


def test_candlestick_paths_are_allowlisted_and_orders_still_are_not():
    _assert_read_only("/series/KXA/markets/KXA-1/candlesticks")
    _assert_read_only("/historical/markets/KXA-1/candlesticks")
    with pytest.raises(SchemaError):
        _assert_read_only("/portfolio/orders")
    assert not any("order" in p for p in READ_ONLY_PATH_PREFIXES)


def test_the_client_refuses_unbounded_or_nonsense_windows():
    c = FakeClient(lambda p, q: {"candlesticks": []})
    for kw in ({"period_interval": 5}, {"start_ts": CUTOFF, "end_ts": CUTOFF},
               {"start_ts": 1, "end_ts": 1 + 5000 * 60}):
        args = dict(series_ticker="S", ticker="S-1", start_ts=CUTOFF - 600, end_ts=CUTOFF, period_interval=1)
        args.update(kw)
        with pytest.raises(SchemaError):
            c.get_market_candlesticks(**args)
    assert c.calls == []  # nothing was requested


def test_fetch_prefers_a_true_close_from_the_live_route_and_stops():
    def respond(path, params):
        return {"candlesticks": [candle(CUTOFF - 60, "0.48", "0.52")]}
    c = FakeClient(respond)
    got = closes.fetch_and_resolve(c, series_ticker="S", ticker="S-1", cutoff_ts=CUTOFF)
    assert got.quality == closes.TRUE_CLOSE and got.source_endpoint == "series_market_candlesticks"
    assert len(c.calls) == 1 and c.calls[0][0] == "/series/S/markets/S-1/candlesticks"


def test_fetch_falls_back_to_hourly_then_historical_and_labels_the_best_it_found():
    def respond(path, params):
        if path.startswith("/historical/") and params["period_interval"] == 60:
            return {"candlesticks": [candle(CUTOFF - 4 * 3600, "0.40", "0.44")]}
        return {"candlesticks": []}
    c = FakeClient(respond)
    got = closes.fetch_and_resolve(c, series_ticker="S", ticker="S-1", cutoff_ts=CUTOFF)
    assert got.quality == closes.EARLIER_QUOTE_PROXY and got.source_endpoint == "historical_candlesticks"
    assert got.minutes_before_cutoff == 240 and got.period_minutes == 60
    assert len(c.calls) == 4  # bounded: live+historical at 1 minute, live+historical at 60 minutes


def test_fetch_without_a_cutoff_makes_no_request():
    c = FakeClient(lambda p, q: {"candlesticks": []})
    got = closes.fetch_and_resolve(c, series_ticker="S", ticker="S-1", cutoff_ts=None)
    assert got.quality == closes.MISSING and c.calls == []
