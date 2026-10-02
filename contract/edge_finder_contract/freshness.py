"""Deterministic freshness. The UI never has to guess whether a Kalshi price is current.

A component is FRESH while younger than ``fresh_after``, AGING until ``stale_after``, STALE after
that, and UNKNOWN when it has no timestamp. The thresholds travel with the health object so a
consumer can recompute the state at read time with the same rule.
"""

from __future__ import annotations

from dataclasses import dataclass

from .timeutil import age_seconds

FRESH, AGING, STALE, UNKNOWN = "FRESH", "AGING", "STALE", "UNKNOWN"
STATES = (FRESH, AGING, STALE, UNKNOWN)


@dataclass(frozen=True)
class Thresholds:
    fresh_after_seconds: int
    stale_after_seconds: int

    def as_dict(self) -> dict:
        return {"fresh_after_seconds": self.fresh_after_seconds,
                "stale_after_seconds": self.stale_after_seconds}


#: Defaults per component. A sport may pass its own (its capture cadence is its own fact).
DEFAULT_THRESHOLDS: dict[str, Thresholds] = {
    "market_data": Thresholds(15 * 60, 60 * 60),
    "model": Thresholds(60 * 60, 6 * 60 * 60),
    "schedule": Thresholds(24 * 60 * 60, 72 * 60 * 60),
    "recommendations": Thresholds(60 * 60, 6 * 60 * 60),
    "export": Thresholds(30 * 60, 3 * 60 * 60),
    "router": Thresholds(30 * 60, 4 * 60 * 60),
    "settlement": Thresholds(24 * 60 * 60, 72 * 60 * 60),
}


def classify(age: float | None, thresholds: Thresholds) -> str:
    if age is None:
        return UNKNOWN
    if age < 0:
        # A timestamp from the future is a clock problem, not freshness. Treat as fresh but let
        # the caller warn; refusing would make a one-second skew look like an outage.
        return FRESH
    if age <= thresholds.fresh_after_seconds:
        return FRESH
    if age <= thresholds.stale_after_seconds:
        return AGING
    return STALE


def status_for(as_of: object, *, component: str = "market_data", now: object | None = None,
               thresholds: Thresholds | None = None) -> str:
    th = thresholds or DEFAULT_THRESHOLDS[component]
    return classify(age_seconds(as_of, now), th)


def worst(*states: str) -> str:
    """The least fresh of several states (UNKNOWN counts as worse than STALE for required data)."""
    order = {FRESH: 0, AGING: 1, STALE: 2, UNKNOWN: 3}
    if not states:
        return UNKNOWN
    return max(states, key=lambda s: order.get(s, 3))
