"""Aggregate-only close coverage: counts, no strings, no inferred cutoff, and a read-only workflow."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import yaml

from kalshi_router.close_coverage import CloseCoverage, run_close_coverage
from kalshi_router.cli import build_parser, config_from_args

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "resolve-closes.yml"
CUT = 1_790_000_000
ISO = "2026-09-21T14:13:20Z"  # == CUT


def candle(end, bid, ask):
    return {"end_period_ts": end, "yes_bid": {"close_dollars": bid}, "yes_ask": {"close_dollars": ask},
            "volume_fp": "5"}


class Fake:
    def __init__(self, markets):
        self.markets = markets  # ticker -> dict(strike, candles_fn)
        self.calls = []

    def iter_fills(self, max_fills=None, **_):
        for t in self.markets:
            yield {"ticker": t}
        yield {"ticker": next(iter(self.markets))}  # duplicate fill of the first market

    def get_market(self, t):
        return {"series_ticker": t.split("-")[0], "event_ticker": "EV-" + t}

    def get_event(self, e):
        strike = self.markets[e[3:]]["strike"]
        return {"strike_date": strike}

    def get_market_candlesticks(self, *, series_ticker, ticker, start_ts, end_ts, period_interval, historical=False):
        from kalshi_router.closes import parse_candlesticks
        self.calls.append((ticker, period_interval, historical))
        return parse_candlesticks({"candlesticks": self.markets[ticker]["candles"](period_interval, historical)}, "t")


def test_report_has_no_field_that_can_hold_a_string():
    for f in dataclasses.fields(CloseCoverage):
        assert f.type in (int, "int"), f.name


def test_counts_each_label_and_never_infers_a_cutoff():
    markets = {
        "KXNFLGAME-A-1": {"strike": ISO, "candles": lambda p, h: [candle(CUT - 60, "0.48", "0.52")] if p == 1 else []},
        "KXNFLGAME-B-1": {"strike": ISO, "candles": lambda p, h: [candle(CUT - 4 * 3600, "0.40", "0.44")] if p == 60 else []},
        "KXNFLGAME-C-1": {"strike": ISO, "candles": lambda p, h: []},
        "KXNFLGAME-D-1": {"strike": None, "candles": lambda p, h: [candle(CUT - 60, "0.4", "0.5")]},
        "KXNCAAFGAME-E-1": {"strike": ISO, "candles": lambda p, h: [candle(CUT - 60, "0.01", "0.99")]},
    }
    c = Fake(markets)
    cov = run_close_coverage(c, max_fills=100)
    assert cov.as_dict() == {"markets_seen": 5, "markets_considered": 5, "true_close": 2, "earlier_quote_proxy": 1,
                             "missing": 1, "no_cutoff": 1, "errors": 0, "true_close_wide_spread": 1, "truncated": 0}
    assert all(t != "KXNFLGAME-D-1" for t, *_ in c.calls)  # no cutoff -> not one candlestick request for it


def test_prefix_filter_and_market_bound():
    markets = {f"KXNFLGAME-{i}-1": {"strike": ISO, "candles": lambda p, h: []} for i in range(5)}
    markets["KXNCAAFGAME-X-1"] = {"strike": ISO, "candles": lambda p, h: []}
    cov = run_close_coverage(Fake(markets), max_fills=10, prefixes=("KXNFL",), max_markets=3)
    assert (cov.markets_seen, cov.markets_considered, cov.truncated) == (6, 3, 1)


def test_cli_parses_and_builds_a_config_like_every_other_subcommand():
    args = build_parser().parse_args(["closes", "--prefix", "KXNFL", "--prefix", "KXNCAAF"])
    assert args.prefix == ["KXNFL", "KXNCAAF"] and args.show_sensitive_details is False
    assert config_from_args(args).max_fills == 500


def test_workflow_is_read_only_main_only_and_prints_counts_only():
    text = WORKFLOW.read_text()
    code = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
    cfg = yaml.safe_load(text)
    on = cfg.get("on", cfg.get(True))
    assert set(on) == {"workflow_dispatch"}
    assert cfg["permissions"] == {"contents": "read"}
    for forbidden in ("upload-artifact", "actions/cache", "git push", "git commit", "gh api", "repository_dispatch",
                      "pull_request_target", "--show-sensitive-details", "tee ", ">>", "write"):
        assert forbidden not in code, forbidden
    assert "refs/heads/main" in code and "persist-credentials: false" in code
    assert not re.search(r"python[^\n]*>\s", code)
