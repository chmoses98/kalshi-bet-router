"""Decode a multivariate (cross-category) combo into its COMPONENT LEGS, or say it cannot be decoded.

WHY (issue #116, deliverable 1: "cross-category contracts must be decoded into component legs or reported
separately").  ``classify_with_legs`` yields ONE sport for a combo and only when every leg agrees; a combo that
spans NBA + NFL, or has a leg the router cannot classify, comes back UNRESOLVED with a one-line reason.  That is the
right *routing* answer, but it throws away the component facts a reconciliation or a postmortem needs: which legs,
which sports, which sides.  This module keeps them.

RULES (no inference, ever)
  * Legs are read only from the exchange's own ``mve_selected_legs`` on the combo's market object.
  * Each leg's sport comes only from ``classify_market`` on that leg's own resolved metadata -- never from a ticker
    prefix, collection name, or title.
  * A leg whose metadata could not be resolved, or that is itself a combo, is reported as UNRESOLVED_LEG; the combo
    is then ``UNDECODED_COMBO`` (partial legs are still listed so nothing is hidden).
  * A combo with no stated legs is ``UNDECODED_COMBO`` (``legs_not_stated``).
  * A decoded combo whose legs span sports is ``DECODED_CROSS_CATEGORY``: it must be reported SEPARATELY and must
    not be defaulted into any single sport's ledger.  ``sport`` is ``None`` for it.

Pure: ``resolve_leg(ticker) -> MarketContext`` is injected (the same callable ``classify_with_legs`` takes).
Output carries market tickers, which are private for real fills; callers must treat it like a wager row and never
print it to a public log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .classify import MarketContext, classify_market
from .refusals import combo_legs, is_combo, leg_market_ticker
from .sports import Sport


class ComboStatus(str, Enum):
    NOT_A_COMBO = "NOT_A_COMBO"
    DECODED_SINGLE_SPORT = "DECODED_SINGLE_SPORT"
    DECODED_CROSS_CATEGORY = "DECODED_CROSS_CATEGORY"
    UNDECODED_COMBO = "UNDECODED_COMBO"


@dataclass(frozen=True)
class ComboLeg:
    market_ticker: str | None
    #: The side the combo requires of this leg, exactly as the exchange states it ("yes"/"no"), else None.
    required_side: str | None
    #: Classified sport, or ``Sport.UNRESOLVED`` when the leg could not be classified from its own metadata.
    sport: Sport
    event_ticker: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class ComboDecoding:
    status: ComboStatus
    legs: tuple[ComboLeg, ...] = ()
    #: Set only for DECODED_SINGLE_SPORT.  None for cross-category and undecoded combos, deliberately.
    sport: Sport | None = None
    sports: tuple[str, ...] = ()
    reason: str | None = None
    flags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def decoded(self) -> bool:
        return self.status in (ComboStatus.DECODED_SINGLE_SPORT, ComboStatus.DECODED_CROSS_CATEGORY)


def _side(leg: dict[str, Any]) -> str | None:
    side = leg.get("side")
    return side.strip().lower() if isinstance(side, str) and side.strip().lower() in ("yes", "no") else None


def decode_combo(context: MarketContext, resolve_leg: Callable[[str], MarketContext]) -> ComboDecoding:
    market = context.market
    if not is_combo(market):
        return ComboDecoding(ComboStatus.NOT_A_COMBO)
    raw = combo_legs(market)
    if not raw:
        return ComboDecoding(ComboStatus.UNDECODED_COMBO, reason="legs_not_stated", flags=("MISSING_COMBO_LEGS",))

    legs: list[ComboLeg] = []
    flags: list[str] = []
    for leg in raw:
        ticker = leg_market_ticker(leg)
        event = leg.get("event_ticker") if isinstance(leg.get("event_ticker"), str) else None
        side = _side(leg)
        if side is None:
            flags.append("MISSING_LEG_SIDE")
        if ticker is None:
            legs.append(ComboLeg(None, side, Sport.UNRESOLVED, event, "leg_has_no_market_ticker"))
            flags.append("LEG_WITHOUT_TICKER")
            continue
        try:
            leg_context = resolve_leg(ticker)
        except Exception as exc:  # a lookup failure is missing evidence, not a reason to crash a report
            legs.append(ComboLeg(ticker, side, Sport.UNRESOLVED, event, f"leg_lookup_failed:{type(exc).__name__}"))
            flags.append("LEG_LOOKUP_FAILED")
            continue
        if is_combo(leg_context.market):
            legs.append(ComboLeg(ticker, side, Sport.UNRESOLVED, event, "leg_is_itself_a_combo"))
            flags.append("NESTED_COMBO")
            continue
        verdict = classify_market(leg_context)
        if verdict.sport is Sport.UNRESOLVED:
            legs.append(ComboLeg(ticker, side, Sport.UNRESOLVED, event,
                                 f"leg_unresolved:{getattr(verdict.unresolved_reason, 'value', 'unknown')}"))
            flags.append("LEG_UNRESOLVED")
            continue
        legs.append(ComboLeg(ticker, side, verdict.sport, event))

    flags_t = tuple(sorted(set(flags)))
    if any(leg.sport is Sport.UNRESOLVED for leg in legs):
        return ComboDecoding(ComboStatus.UNDECODED_COMBO, tuple(legs), reason="a_leg_could_not_be_classified",
                             flags=flags_t)
    sports = tuple(sorted({leg.sport.value for leg in legs}))
    if len(sports) == 1:
        return ComboDecoding(ComboStatus.DECODED_SINGLE_SPORT, tuple(legs), sport=legs[0].sport, sports=sports,
                             flags=flags_t)
    return ComboDecoding(ComboStatus.DECODED_CROSS_CATEGORY, tuple(legs), sport=None, sports=sports,
                         reason="legs_span_sports_report_separately", flags=flags_t)
