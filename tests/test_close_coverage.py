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
    def __init__(self, markets, milestones=None, milestone_error=False):
        self.markets = markets  # ticker -> dict(strike, candles_fn)
        self.calls = []
        self.milestones = milestones or []
        self.milestone_error = milestone_error
        self.milestone_params = []

    def get_milestones(self, params):
        from kalshi_router.errors import KalshiRouterError
        self.milestone_params.append(dict(params))
        if self.milestone_error:
            raise KalshiRouterError("boom")
        return {"milestones": self.milestones, "cursor": ""}

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
                             "missing": 1, "no_cutoff": 1, "errors": 0, "true_close_wide_spread": 1, "truncated": 0,
                             "cutoff_from_strike_date": 4, "cutoff_from_milestone_start": 0, "cutoff_conflict": 0,
                             "milestone_sweep_requests": 1, "milestone_sweep_budget_exhausted": 0,
                             "milestone_sweep_failed": 0}
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


# ---- exchange-stated game start from the public milestone (EXCHANGE_MILESTONE_START) ----------------------------

def milestone(events, start=ISO, status="scheduled", **extra):
    m = {"id": "m", "type": "football_game", "start_date": start, "details": {"status": status},
         "primary_event_tickers": list(events), "related_event_tickers": []}
    m.update(extra)
    return m


def near_close():
    return lambda p, h: [candle(CUT - 60, "0.48", "0.52")] if p == 1 else []


def run(markets, milestones, **kw):
    c = Fake(markets, milestones)
    return run_close_coverage(c, max_fills=10, **kw), c


def test_milestone_start_gives_a_cutoff_when_the_event_states_none():
    markets = {"KXNCAAFGAME-A-1": {"strike": None, "candles": near_close()}}
    cov, c = run(markets, [milestone(["EV-KXNCAAFGAME-A-1"])])
    assert (cov.true_close, cov.no_cutoff, cov.cutoff_from_milestone_start, cov.cutoff_from_strike_date) == (1, 0, 1, 0)
    # the sweep is the PUBLIC football-game list: the account's ticker is never sent
    assert c.milestone_params == [{"type": "football_game", "limit": 200}]


def test_milestone_cutoff_is_labelled_on_the_close_row():
    from kalshi_router.closes import fetch_and_resolve
    c = Fake({"KXNCAAFGAME-A-1": {"strike": None, "candles": near_close()}})
    got = fetch_and_resolve(c, series_ticker="KXNCAAFGAME", ticker="KXNCAAFGAME-A-1", cutoff_ts=CUT,
                            cutoff_source="EXCHANGE_MILESTONE_START")
    assert got.quality == "TRUE_CLOSE" and got.to_close_body()["cutoff_source"] == "EXCHANGE_MILESTONE_START"


def test_true_close_semantics_are_unchanged_for_a_milestone_cutoff():
    # a candle 4h before the stated start is a proxy, exactly as with a strike_date cutoff
    markets = {"KXNCAAFGAME-A-1": {"strike": None,
                                   "candles": lambda p, h: [candle(CUT - 4 * 3600, "0.4", "0.44")] if p == 60 else []}}
    cov, _ = run(markets, [milestone(["EV-KXNCAAFGAME-A-1"])])
    assert (cov.true_close, cov.earlier_quote_proxy, cov.cutoff_from_milestone_start) == (0, 1, 1)


def test_unmatched_event_is_no_cutoff_and_makes_no_candle_request():
    markets = {"KXNCAAFGAME-A-1": {"strike": None, "candles": near_close()}}
    cov, c = run(markets, [milestone(["EV-SOMETHING-ELSE"])])
    assert (cov.no_cutoff, cov.true_close) == (1, 0) and c.calls == []


def test_untrusted_milestones_never_give_a_cutoff():
    for kw in ({"status": "time-tbd"}, {"status": "created"}, {"status": None}, {"start": "2000-01-01T00:00:00Z"},
               {"start": "0001-01-01T00:00:00Z"}, {"start": "2026-09-21"}, {"start": "2026-09-21T14:13:20"},
               {"start": None}, {"start": "garbage"}):
        m = milestone(["EV-KXNCAAFGAME-A-1"], **kw)
        if kw.get("status", "x") is None:
            del m["details"]["status"]
        cov, c = run({"KXNCAAFGAME-A-1": {"strike": None, "candles": near_close()}}, [m])
        assert (cov.no_cutoff, cov.true_close) == (1, 0), kw
        assert c.calls == [], kw


def test_event_named_by_a_trusted_and_an_untrusted_milestone_fails_closed_in_either_order():
    ev = ["EV-KXNCAAFGAME-A-1"]
    for order in ([milestone(ev), milestone(ev, status="time-tbd")], [milestone(ev, status="time-tbd"), milestone(ev)]):
        cov, _ = run({"KXNCAAFGAME-A-1": {"strike": None, "candles": near_close()}}, order)
        assert (cov.no_cutoff, cov.true_close) == (1, 0)


def test_two_milestones_that_disagree_on_start_fail_closed():
    ev = ["EV-KXNCAAFGAME-A-1"]
    cov, _ = run({"KXNCAAFGAME-A-1": {"strike": None, "candles": near_close()}},
                 [milestone(ev), milestone(ev, start="2026-09-21T15:13:20Z")])
    assert (cov.no_cutoff, cov.true_close) == (1, 0)


def test_strike_date_disagreeing_with_milestone_start_is_a_conflict_not_a_guess():
    markets = {"KXNCAAFGAME-A-1": {"strike": "2026-09-21T15:13:20Z", "candles": near_close()}}
    cov, c = run(markets, [milestone(["EV-KXNCAAFGAME-A-1"])])
    assert (cov.cutoff_conflict, cov.no_cutoff, cov.true_close) == (1, 1, 0) and c.calls == []


def test_agreeing_strike_date_wins_and_is_labelled_strike_date():
    markets = {"KXNCAAFGAME-A-1": {"strike": ISO, "candles": near_close()}}
    cov, _ = run(markets, [milestone(["EV-KXNCAAFGAME-A-1"])])
    assert (cov.cutoff_from_strike_date, cov.cutoff_from_milestone_start, cov.true_close) == (1, 0, 1)


def test_milestone_sweep_failure_degrades_to_no_cutoff_and_is_reported():
    markets = {"KXNCAAFGAME-A-1": {"strike": None, "candles": near_close()}}
    c = Fake(markets, milestone_error=True)
    cov = run_close_coverage(c, max_fills=10)
    assert (cov.no_cutoff, cov.milestone_sweep_failed, cov.true_close) == (1, 1, 0)


def test_nominal_market_times_are_never_a_cutoff():
    """occurrence_datetime / expected_expiration_time / close_time are not a start; with no stated start -> no cutoff."""
    class F(Fake):
        def get_market(self, t):
            return {"series_ticker": "KXNCAAFGAME", "event_ticker": "EV-" + t, "occurrence_datetime": ISO,
                    "expected_expiration_time": ISO, "close_time": ISO, "open_time": ISO}
    c = F({"KXNCAAFGAME-A-1": {"strike": None, "candles": near_close()}})
    cov = run_close_coverage(c, max_fills=10)
    assert (cov.no_cutoff, cov.true_close) == (1, 0) and c.calls == []


def test_milestone_start_can_be_turned_off():
    markets = {"KXNCAAFGAME-A-1": {"strike": None, "candles": near_close()}}
    cov, c = run(markets, [milestone(["EV-KXNCAAFGAME-A-1"])], use_milestone_start=False)
    assert (cov.no_cutoff, cov.milestone_sweep_requests) == (1, 0) and c.milestone_params == []


def test_an_incomplete_sweep_trusts_no_start():
    from kalshi_router.milestones import MilestoneStartIndex

    for flag in ("budget_exhausted", "fetch_failed"):
        idx = MilestoneStartIndex()
        idx.record("EV-X", 1_800_000_000)
        assert idx.start_for_event("EV-X") == 1_800_000_000
        setattr(idx, flag, True)
        assert idx.start_for_event("EV-X") is None
