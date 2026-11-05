from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import pytest
from kalshi_router import entry_capture as EC
from kalshi_router.client import PUBLIC_EXACT_PATHS, _assert_read_only
from kalshi_router.entry_capture_loop import Collector
from kalshi_router.errors import SchemaError
from tests.synthetic import make_fill

T0 = datetime(2026, 11, 4, 20, 0, 0, tzinfo=timezone.utc)
TK = "KXNFLGAME-26NOV04AAABBB-AAA"


class Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t
    def tick(self, s): self.t += timedelta(seconds=s)


class FakeClient:
    def __init__(self, clock):
        self.clock, self.markets, self.orders, self.fills, self.request_count = clock, {}, [], [], 0
    def iter_markets(self, series, **kw):
        return iter(self.markets.get(series, []))
    def iter_orders(self, n, **kw):
        return iter(self.orders)
    def iter_fills(self, **kw):
        return iter(self.fills)


def mkt(bid, ask, bs="50", az="40", t=TK):
    return {"ticker": t, "yes_bid_dollars": bid, "yes_ask_dollars": ask, "yes_bid_size_fp": bs, "yes_ask_size_fp": az}


def order(oid, created, ticker=TK):
    return {"order_id": oid, "ticker": ticker, "created_time": created.isoformat().replace("+00:00", "Z"), "status": "executed"}


def setup():
    clock = Clock(T0); c = FakeClient(clock)
    col = Collector(c, ["KXNFLGAME"], now=clock, sleep=lambda s: None)
    return clock, c, col


def fill(oid, price="0.50", count=10):
    f = make_fill(1, ticker=TK, side="yes", order_id=oid, count=count)
    f["yes_price_dollars"] = price; f["no_price_dollars"] = str(D("1") - D(price)); f["count_fp"] = f"{count}.00"
    return f


def test_public_exact_path_is_markets_only():
    _assert_read_only("/markets")
    assert PUBLIC_EXACT_PATHS == {"/markets"}
    for bad in ("/marketsX", "/portfolio/orders/ORD", "/exchange/status"):
        with pytest.raises(SchemaError):
            _assert_read_only(bad)


def test_fresh_snapshot_before_placement_is_an_entry_quote():
    clock, c, col = setup()
    c.markets["KXNFLGAME"] = [mkt("0.48", "0.50")]
    col.one_pass()                               # snapshot at T0
    clock.tick(20)
    placed = T0 + timedelta(seconds=15)          # placed 15 s after the snapshot, before the next one
    c.orders = [order("ORD-1", placed)]; c.fills = [fill("ORD-1", "0.50")]
    clock.tick(0); res = col.one_pass()          # pass at T0+20: new snapshot is AFTER placement and must not be used
    assert res["new"] == 1
    t = col.agg.public()["totals"]
    assert t[EC.ENTRY_FRESH] == 1 and t["orders"] == 1
    cell = col.agg.public()["cells"]["KXNFLGAME"]
    assert cell["fresh_spread_sum"] == pytest.approx(2.0) and cell["fresh_paid_vs_ask_sum"] == pytest.approx(0.0)


def test_snapshot_after_placement_is_never_used():
    clock, c, col = setup()
    clock.tick(30)
    placed = T0 + timedelta(seconds=5)           # collector started at T0 ... but only snapshot is at T0+30
    c.markets["KXNFLGAME"] = [mkt("0.48", "0.50")]
    col.started_at = T0
    c.orders = [order("ORD-2", placed)]; c.fills = [fill("ORD-2")]
    col.one_pass()
    t = col.agg.public()["totals"]
    assert t[EC.SNAPSHOT_AFTER_ORDER] == 1 and t[EC.ENTRY_FRESH] == 0


def test_stale_snapshot_counts_as_coverage_but_not_entry_statistics():
    clock, c, col = setup()
    c.markets["KXNFLGAME"] = [mkt("0.48", "0.50")]
    col.one_pass()
    clock.tick(300)
    c.markets["KXNFLGAME"] = []                   # ticker stops being sampled (e.g. sampler gap)
    placed = clock.t - timedelta(seconds=1)
    c.orders = [order("ORD-3", placed)]; c.fills = [fill("ORD-3")]
    col.one_pass()
    p = col.agg.public()
    assert p["totals"][EC.STALE_NOT_ENTRY] == 1 and p["cells"]["KXNFLGAME"]["fresh_spread_sum"] == 0.0 and p["game_sums"] == {}


def test_orders_placed_before_the_collector_existed_are_never_joined():
    clock, c, col = setup()
    c.markets["KXNFLGAME"] = [mkt("0.48", "0.50")]
    c.orders = [order("ORD-OLD", T0 - timedelta(seconds=30))]; c.fills = [fill("ORD-OLD")]
    col.one_pass()
    assert col.agg.public()["totals"]["orders"] == 0 and col.health["historical_not_capturable"] == 1


def test_no_snapshot_and_no_double_count_and_privacy():
    clock, c, col = setup()
    c.orders = [order("ORD-5", T0 + timedelta(seconds=2))]; c.fills = [fill("ORD-5")]
    clock.tick(5); col.one_pass(); col.one_pass()
    p = col.public()
    assert p["totals"][EC.NO_SNAPSHOT] == 1 and p["totals"]["orders"] == 1
    import json
    blob = json.dumps(p)
    for secret in ("ORD-5", "KXNFLGAME-26", "SYNTHFILL", TK):
        assert secret not in blob


def test_no_side_price_derivation_and_one_sided_book():
    r = EC.Ring(); r.add(TK, EC.Snap(T0, D("0.48"), D("0.50")))
    row = EC.join_order(r, ticker=TK, outcome_side="no", placed_at=T0 + timedelta(seconds=5), vwap=D("0.52"), qty=D(10))
    assert row.status == EC.ENTRY_FRESH and row.spread_cents == pytest.approx(2.0) and row.paid_vs_ask_cents == pytest.approx(0.0)
    r2 = EC.Ring(); r2.add(TK, EC.Snap(T0, None, D("0.50")))
    assert EC.join_order(r2, ticker=TK, outcome_side="yes", placed_at=T0 + timedelta(seconds=5), vwap=D("0.5"), qty=D(1)).status == EC.ONE_SIDED


def test_bought_no_uses_the_no_leg_price_not_the_yes_axis():
    """A 40c NO fill is not a 60c fill merely because the YES axis is 60c."""
    clock, c, col = setup()
    c.markets["KXNFLGAME"] = [mkt("0.60", "0.62")]
    col.one_pass()  # book at T0: NO bid 38c / NO ask 40c
    placed = T0 + timedelta(seconds=15)
    f = make_fill(71, ticker=TK, action="buy", side="no", order_id="ORD-NO")
    f["yes_price_dollars"] = "0.60"
    f["no_price_dollars"] = "0.40"
    c.orders = [order("ORD-NO", placed)]
    c.fills = [f]
    clock.tick(20)
    assert col.one_pass()["new"] == 1
    cell = col.agg.public()["cells"]["KXNFLGAME"]
    assert cell["ENTRY_FRESH"] == 1
    assert cell["fresh_paid_vs_ask_sum"] == pytest.approx(0.0)
    assert cell["fresh_paid_vs_mid_sum"] == pytest.approx(1.0)
    assert col.public()["metrics_version"] == "purchased_leg_v2"


def test_sell_does_not_get_treated_as_a_purchase_or_repeat_as_new():
    clock, c, col = setup()
    c.markets["KXNFLGAME"] = [mkt("0.48", "0.50")]
    col.one_pass()
    c.orders = [order("ORD-SELL", T0 + timedelta(seconds=10))]
    c.fills = [make_fill(72, ticker=TK, order_id="ORD-SELL", action="sell", side="yes")]
    clock.tick(20)
    assert col.one_pass()["new"] == 0
    assert col.public()["totals"]["orders"] == 0
    assert col.health["non_buy_orders_excluded"] == 1
    col.one_pass()
    assert col.health["non_buy_orders_excluded"] == 1
    assert col.public()["totals"]["ENTRY_FRESH"] == 0


def test_upgrade_separates_unverifiable_old_yes_axis_aggregates():
    """Old NO fills cannot be reconstructed from legacy aggregate-only state."""
    clock, c, col = setup()
    c.markets["KXNFLGAME"] = [mkt("0.48", "0.50")]
    col.one_pass()
    c.orders = [order("ORD-OLD", T0 + timedelta(seconds=10))]
    c.fills = [fill("ORD-OLD", "0.50")]
    clock.tick(20)
    col.one_pass()
    saved = col.state()
    # Simulate code before the purchased-side correction: no metrics_version.
    saved.pop("metrics_version")
    saved.pop("ignored_orders")
    saved.pop("legacy_unreconciled")
    reopened = Collector(c, ["KXNFLGAME"], state=saved, now=clock, sleep=lambda _: None)
    pub = reopened.public()
    assert pub["totals"]["orders"] == 0
    assert pub["legacy_unreconciled"]["orders"] == 1
    assert pub["legacy_unreconciled"]["fresh_entry_quotes"] == 1
    reopened.one_pass()
    assert reopened.agg.public()["totals"]["orders"] == 0
    assert reopened.state()["metrics_version"] == "purchased_leg_v2"
    # A second restart does not resurrect the unverifiable cohort.
    again = Collector(c, ["KXNFLGAME"], state=reopened.state(), now=clock, sleep=lambda _: None)
    assert again.public()["legacy_unreconciled"]["orders"] == 1
    assert again.public()["totals"]["orders"] == 0


def test_mixed_contract_fills_do_not_generate_price_comparisons():
    clock, c, col = setup()
    c.markets["KXNFLGAME"] = [mkt("0.48", "0.50")]
    col.one_pass()
    c.orders = [order("ORD-MIX", T0 + timedelta(seconds=15))]
    c.fills = [
        make_fill(73, ticker=TK, side="yes", order_id="ORD-MIX"),
        make_fill(74, ticker=TK, side="no", order_id="ORD-MIX"),
    ]
    clock.tick(20)
    assert col.one_pass()["new"] == 0
    assert col.health["mixed_contract_orders_excluded"] == 1
    assert col.agg.public()["totals"]["ENTRY_FRESH"] == 0
