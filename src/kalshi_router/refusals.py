"""What the refused post-cutover orders ARE, without saying what was traded.

WHY THIS EXISTS
---------------
From 2026-09-21 the production report said, every run:

    refused: ...
      sport unresolved: 14
    why those markets were unresolved (distinct MARKETS ...):
      competition absent: 9
    BLOCKED (cannot be recorded, and waiting will not help): 14

and nothing else. Fourteen real wagers, and the only thing anyone could learn
about them was a reason code. Whether they were MLB wagers the ledger was
missing, or wagers on a sport this system is designed never to route, could not
be answered from the report -- so a week of "BLOCKED" was indistinguishable
from a week of correct refusals.

"competition absent" with nothing from L4 is itself informative: every MLB
series Kalshi lists carries ``tags='Baseball'`` (measured by the series probe,
2026-09-15), which would have produced AMBIGUOUS_FAMILY rather than silence.
The shape that produces silence is a market whose own series names no sport at
all -- which is what a multivariate COMBO (parlay) market looks like: its
series is a collection, and the sports live on its legs.

So each refused market is profiled by what it structurally is:

  * whether the exchange presents it as a combo, and if so how its LEGS
    classify -- each leg through the same classifier, on the leg's own
    metadata, so "every leg is MLB" is Kalshi's evidence rather than ours;
  * the public catalogue descriptors of its series (category; and title and
    tags only when the category is Sports);
  * how many refused orders it carries, on which UTC dates.

*** WHAT IT DOES NOT PRINT ***
No market, event or series ticker. No price, size, fee, order id or fill id.
A non-Sports series is reported by category alone, because the title of, say,
an economics series would say more about the account than its sports titles
say beyond the destination ledgers the owner already publishes.

It changes no verdict and routes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

#: The field names Kalshi uses for a multivariate (combo) market. The legs
#: field is what matters; the collection ticker corroborates that the market is
#: one. Both are read from the MARKET object the resolver already fetched.
COMBO_LEGS_FIELD = "mve_selected_legs"
COMBO_COLLECTION_FIELD = "mve_collection_ticker"

SPORTS_CATEGORY = "sports"


def combo_legs(market: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The combo's legs as the exchange states them, or [] for a single market.

    A leg without a market ticker is dropped rather than guessed at; the caller
    compares the count it gets with the count the exchange stated.
    """
    if not isinstance(market, dict):
        return []
    raw = market.get(COMBO_LEGS_FIELD)
    if not isinstance(raw, list):
        return []
    return [leg for leg in raw if isinstance(leg, dict)]


def is_combo(market: dict[str, Any] | None) -> bool:
    if not isinstance(market, dict):
        return False
    if combo_legs(market):
        return True
    collection = market.get(COMBO_COLLECTION_FIELD)
    return isinstance(collection, str) and bool(collection.strip())


def leg_market_ticker(leg: dict[str, Any]) -> str | None:
    ticker = leg.get("market_ticker") or leg.get("ticker")
    return ticker if isinstance(ticker, str) and ticker.strip() else None


def utc_date(epoch_seconds: Decimal | int | float | None) -> str | None:
    if epoch_seconds is None:
        return None
    try:
        return datetime.fromtimestamp(float(epoch_seconds), tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _texts(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(v.strip() for v in value if isinstance(v, str) and v.strip())
    return ()


@dataclass
class RefusedMarketProfile:
    """One refused market, described by its shape. Holds no identifier."""

    verdict: str
    reason: str
    orders: int = 0
    order_dates: dict[str, int] = field(default_factory=dict)
    series_category: str | None = None
    #: Only populated when the category is Sports; see the module docstring.
    series_title: str | None = None
    series_tags: tuple[str, ...] = ()
    combo: bool = False
    legs_stated: int = 0
    #: Leg verdicts, by sport name, from the classifier run on each leg.
    leg_sports: dict[str, int] = field(default_factory=dict)
    #: UTC game dates of the legs whose date could be established.
    leg_game_dates: tuple[str, ...] = ()

    @property
    def all_legs_one_sport(self) -> str | None:
        if not self.combo or self.legs_stated < 2:
            return None
        if sum(self.leg_sports.values()) != self.legs_stated or len(self.leg_sports) != 1:
            return None
        return next(iter(self.leg_sports))


def describe_series(series: dict[str, Any] | None) -> tuple[str | None, str | None, tuple[str, ...]]:
    """(category, title, tags) -- title and tags only for a Sports series."""
    if not isinstance(series, dict):
        return None, None, ()
    category = next(iter(_texts(series.get("category"))), None)
    if category is None or category.lower() != SPORTS_CATEGORY:
        return category, None, ()
    title = next(iter(_texts(series.get("title"))), None)
    tags = _texts(series.get("tags")) + _texts(series.get("categories"))
    return category, title, tags


@dataclass
class RefusalProfile:
    """Every refused-for-sport market after the cutover, and the orders on it."""

    markets: list[RefusedMarketProfile] = field(default_factory=list)

    @property
    def orders(self) -> int:
        return sum(m.orders for m in self.markets)

    def orders_by_date(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for market in self.markets:
            for day, count in market.order_dates.items():
                out[day] = out.get(day, 0) + count
        return dict(sorted(out.items()))

    def combos_by_leg_sport(self) -> dict[str, int]:
        """Refused ORDERS on combos, keyed by the single sport all legs share
        (or 'mixed/unresolved')."""
        out: dict[str, int] = {}
        for market in self.markets:
            if not market.combo:
                continue
            key = market.all_legs_one_sport or "mixed/unresolved"
            out[key] = out.get(key, 0) + market.orders
        return dict(sorted(out.items()))

    def render(self) -> str:
        lines = [
            "refused-for-sport orders, profiled (public catalogue descriptors and counts only;",
            "no ticker, price, size or id):",
            f"  markets: {len(self.markets)}   orders: {self.orders}",
        ]
        if not self.markets:
            lines.append("  none")
            return "\n".join(lines)
        lines.append("  orders by first-fill UTC date:")
        for day, count in self.orders_by_date().items():
            lines.append(f"    {day}: {count}")
        combos = self.combos_by_leg_sport()
        combo_orders = sum(combos.values())
        lines.append(f"  orders on COMBO (multivariate) markets: {combo_orders}")
        for sport, count in combos.items():
            lines.append(f"    every leg classified {sport}: {count}" if sport != "mixed/unresolved"
                         else f"    legs mixed or unresolved: {count}")
        lines.append(f"  orders on single markets: {self.orders - combo_orders}")
        lines.append("  each market (unnamed):")
        for number, market in enumerate(
            sorted(self.markets, key=lambda m: (min(m.order_dates or {"": 0}), m.reason)), 1
        ):
            shape = (f"combo, {market.legs_stated} legs stated, leg verdicts "
                     + ", ".join(f"{k}={v}" for k, v in sorted(market.leg_sports.items()))
                     if market.combo else "single market")
            lines.append(
                f"    #{number}: {market.verdict} ({market.reason}); orders {market.orders} "
                f"on {', '.join(sorted(market.order_dates)) or '-'}; {shape}"
            )
            descriptor = f"category={market.series_category or '-'}"
            if market.series_title:
                descriptor += f"; title={market.series_title[:60]!r}"
            if market.series_tags:
                descriptor += f"; tags={','.join(market.series_tags)[:60]!r}"
            if market.leg_game_dates:
                descriptor += f"; leg game dates={','.join(sorted(set(market.leg_game_dates)))}"
            lines.append(f"        series {descriptor}")
        return "\n".join(lines)


def coverage_lines(wagers, refusals: RefusalProfile, sports) -> list[str]:
    """One machine-readable line per destination sport, for a watcher OUTSIDE
    this repository to compare with the destination's canonical ledger.

    ``newest_game_date`` is the newest game date among the sport's eligible
    orders -- what the destination's newest router-imported row must equal
    once everything eligible has merged. ``refused_provably`` counts refused
    orders whose every combo leg the classifier placed in this sport: wagers
    that ARE this sport's and that this router is not delivering. Dates and
    counts only.
    """
    lines = []
    provable = refusals.combos_by_leg_sport()
    for sport in sorted(sports):
        own = [w for w in wagers if w.sport == sport]
        newest_game = max((w.game_date for w in own), default="none")
        newest_fill = max(
            (utc_date(w.first_execution_time) or "" for w in own), default=""
        ) or "none"
        lines.append(
            f"ROUTER_COVERAGE sport={sport} eligible={len(own)} "
            f"newest_game_date={newest_game} newest_first_fill_utc={newest_fill} "
            f"refused_provably={provable.get(sport, 0)}"
        )
    return lines
