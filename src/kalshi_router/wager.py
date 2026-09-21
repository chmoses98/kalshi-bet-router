"""Shadow canonical wagers: what a router WOULD send, built but never sent.

Phase G. This module turns a position episode into the exact row shape
``scripts/edgelab/import_bet_batch.py`` accepts in the MLB ledger, and then
does nothing with it. No write, no request, no file. The point is to find out
what would actually be rejected **before** anything can be rejected for real.

Why build the importer's row rather than the stored record
----------------------------------------------------------
``placed_bet.schema.json`` describes what the ledger STORES. Its ``betId``,
``validationStatus``, ``provenance`` and ``createdAt`` are produced by the
importer, not by its caller. A router that emitted those would be inventing
fields the destination owns. So the target here is the importer's INPUT
contract, read from the importer itself:

* ``gameDate`` and ``stake`` are required on every row;
* exactly one of ``entryPrice`` / ``entryOdds``;
* ``sourceBetKey`` per row and ``importBatchId`` on the payload are required
  whenever ``entryTimestamp`` is absent -- and are supplied here regardless,
  because they are what makes a re-run a no-op;
* ``marketTicker``, when given, skips the importer's own ticker resolution.

Everything else is optional, and optional is where a router should stay.

Refusal is the interesting output
---------------------------------
A wager is built only when every fact it needs is evidenced. Anything missing
produces a :class:`WagerRefusal` naming what was missing, never a row with a
plausible value in the gap. The counts of those refusals are the real result of
a shadow run: they say what routing would cost in accuracy today.

SENSITIVE: a :class:`ShadowWager` carries tickers, quantities, prices, fees and
payouts. It is an in-memory value. Only :class:`WagerDiagnostics` -- counts and
booleans -- is ever rendered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

from .accounting.position import Direction, PositionEpisode
from .classify import (
    Classification,
    EvidenceLevel,
    MarketContext,
    UnresolvedReason,
)
from .destinations import PROFILES
from .sports import Sport

ONE = Decimal(1)
ZERO = Decimal(0)

#: Sports whose ledger repository has an importer to route into.
#:
#: DERIVED from `destinations.PROFILES` rather than restated. When this was a
#: literal it could disagree with what production actually routed -- the
#: shadow path would refuse a sport the scheduled job was delivering, or the
#: reverse -- and neither disagreement is visible from either side. A profile
#: cannot be written without naming the repository, the ledger branch, the
#: importer and what it may touch, so a sport nobody has answered those for
#: still cannot appear here by accident.
SPORTS_WITH_AN_IMPORTER = frozenset(PROFILES)

#: Settlement results this contract knows how to express. A scalar settlement
#: pays somewhere strictly between the two, and MLB's ledger has WIN/LOSS/PUSH/
#: VOID and no partial. Tennis is known to produce scalars, so this is a live
#: hazard rather than a hypothetical one.
BINARY_RESULTS = frozenset({"yes", "no"})


class WagerRefusal(str, Enum):
    """Why an episode did not become a routable wager.

    Every value names a MISSING OR CONTRADICTED FACT, not a policy preference.
    An episode is refused because something could not be established, and the
    counter says which thing -- so the fix is always identifiable.
    """

    #: No stable identity: the opening is unprovable, or the position story is
    #: unearned. Either way there is no idempotent key to import against.
    IDENTITY_NOT_IMPORTABLE = "identity_not_importable"
    #: The episode is still open. A wager with no outcome is not a settled bet,
    #: and this contract has no state for one.
    NOT_CLOSED = "not_closed"
    #: The market was never classified -- outside the run's classification
    #: bound. Not the same as unresolved: nothing was attempted.
    MARKET_NOT_CLASSIFIED = "market_not_classified"
    #: Classification ran and could not name the sport. Routing on a guess is
    #: the one thing this system must never do.
    SPORT_UNRESOLVED = "sport_unresolved"
    #: The sport is known and its repository has no importer to route into.
    NO_DESTINATION_IMPORTER = "no_destination_importer"
    #: ``gameDate`` is required by the importer and could not be established
    #: from evidence. Deriving it from a UTC timestamp would be wrong for any
    #: evening game, roughly half the time, and silently.
    GAME_DATE_NOT_ESTABLISHED = "game_date_not_established"
    #: No cost basis, so no stake and no entry price.
    COST_BASIS_INCOMPLETE = "cost_basis_incomplete"
    #: A fee is missing, so the cash actually committed is unknown.
    FEES_INCOMPLETE = "fees_incomplete"
    #: A cross-zero reversal split one execution's fee across two episodes and
    #: Kalshi documents no allocation rule.
    FEE_ALLOCATION_AMBIGUOUS = "fee_allocation_ambiguous"
    #: Closed by trading rather than settlement, so there is no exchange-stated
    #: payout. The economics are knowable but this contract's settled-wager
    #: shape is not the right one, and inventing a result would be a guess.
    NO_SETTLEMENT_ECONOMICS = "no_settlement_economics"
    #: The settlement is not binary. MLB's ledger has no partial result.
    NON_BINARY_SETTLEMENT = "non_binary_settlement"


class GameDateSource(str, Enum):
    """Where a row's ``gameDate`` came from, or why there is none."""

    #: An explicit date field on the event or market object.
    EVENT_FIELD = "event_field"
    #: Parsed strictly from the event ticker's own date segment.
    EVENT_TICKER = "event_ticker"
    #: Nothing established it.
    NONE = "none"


#: Kalshi event tickers carry the contest's own date, e.g.
#: ``KXMLBGAME-26AUG03SFLAD``. That is the date as the exchange labels the
#: contest, which is what a game date means -- unlike a UTC close timestamp,
#: which puts a night game on the following day.
_EVENT_TICKER_DATE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})")
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
#: Explicit date-bearing fields, checked before any ticker parsing.
_EVENT_DATE_FIELDS = ("game_date", "scheduled_start_time", "strike_date")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def resolve_game_date(context: MarketContext | None) -> tuple[str | None, GameDateSource]:
    """Establish the contest's own date, or refuse.

    Evidence order, strongest first:

    1. an explicit date field on the event object;
    2. the date segment of the event ticker, parsed strictly.

    There is deliberately no third option. ``close_time`` is a UTC instant, and
    a game that ends at 03:00 UTC belongs to the previous local date -- so
    deriving the date from it would be wrong for most evening games, and wrong
    silently. An unparseable date is an :class:`WagerRefusal`, not a default.
    """
    if context is None:
        return None, GameDateSource.NONE

    for source in (context.event, context.market):
        if not isinstance(source, dict):
            continue
        for key in _EVENT_DATE_FIELDS:
            value = source.get(key)
            if not isinstance(value, str):
                continue
            match = _ISO_DATE.match(value.strip())
            if match:
                return match.group(0), GameDateSource.EVENT_FIELD

    ticker = _event_ticker(context)
    if ticker:
        match = _EVENT_TICKER_DATE.search(ticker)
        if match:
            year, month_name, day = match.groups()
            month = _MONTHS.get(month_name)
            if month is not None:
                # Two-digit years are read as 20xx. Kalshi has no pre-2000
                # contests, so there is no ambiguity to resolve.
                return f"20{year}-{month:02d}-{int(day):02d}", GameDateSource.EVENT_TICKER
    return None, GameDateSource.NONE


def _event_ticker(context: MarketContext) -> str | None:
    for source in (context.event, context.market):
        if isinstance(source, dict):
            value = source.get("event_ticker")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


@dataclass(frozen=True)
class ShadowWager:
    """One canonical wager, built and never sent.

    SENSITIVE: carries a ticker, a quantity, prices, fees and a payout. Never
    rendered; only counted.
    """

    source_bet_key: str
    market_ticker: str
    event_ticker: str | None
    sport: Sport
    game_date: str
    game_date_source: GameDateSource
    #: The contract actually held: YES or NO.
    side: str
    contracts: Decimal
    #: Price of the contract held, in dollars -- not the YES-axis coordinate.
    entry_price: Decimal
    contract_cost: Decimal
    total_fees: Decimal
    stake: Decimal
    gross_settlement_payout: Decimal
    net_profit_loss: Decimal
    result: str
    entry_timestamp: Decimal | None

    def to_import_row(self) -> dict[str, Any]:
        """The importer's row shape, with nothing invented.

        Money is emitted as ``float`` because that is what the destination
        schema declares; every value was computed in :class:`~decimal.Decimal`
        and converted once, here, at the boundary. No arithmetic happens after
        this point.

        Fields the IMPORTER owns -- ``betId``, ``validationStatus``,
        ``provenance``, ``createdAt`` -- are absent on purpose.
        """
        return {
            "sourceBetKey": self.source_bet_key,
            "gameDate": self.game_date,
            "marketTicker": self.market_ticker,
            "side": self.side,
            "stake": float(self.stake),
            "entryPrice": float(self.entry_price),
            "contracts": float(self.contracts),
            "contractCost": float(self.contract_cost),
            "totalFees": float(self.total_fees),
            "actualCashConsumed": float(self.stake),
            "grossSettlementPayout": float(self.gross_settlement_payout),
            "netProfitLoss": float(self.net_profit_loss),
            "result": self.result,
            "status": "settled",
            "executionStatus": "HELD_TO_SETTLEMENT",
            # The fees and the payout are the exchange's own, read from its
            # fills and settlement rows. Nothing here is reconstructed from a
            # fee schedule, which is what the lower tiers of these enums mean.
            "feeStatus": "ACTUAL_API_FILL",
            "economicsSource": "EXACT_API_EXECUTION",
            "source": "OTHER",
            "trackingType": "REAL",
        }


@dataclass
class WagerDiagnostics:
    """Counts only. Safe to print in a public Actions log.

    Same structural guarantee as the accounting diagnostics: no field here can
    hold a ticker, a price, a quantity or a date.
    """

    episodes_considered: int = 0
    wagers_built: int = 0

    refused_identity_not_importable: int = 0
    refused_not_closed: int = 0
    refused_market_not_classified: int = 0
    refused_sport_unresolved: int = 0
    refused_no_destination_importer: int = 0
    refused_game_date_not_established: int = 0
    refused_cost_basis_incomplete: int = 0
    refused_fees_incomplete: int = 0
    refused_fee_allocation_ambiguous: int = 0
    refused_no_settlement_economics: int = 0
    refused_non_binary_settlement: int = 0

    #: What the ROUTABLE episodes -- those with an importable identity -- were
    #: classified as. Without this the refusal counts say a sport could not be
    #: resolved but never what the market was, and "296 unresolved" reads as a
    #: classifier defect when it may be an account that simply trades markets
    #: this router is right to refuse.
    routable_classified_mlb: int = 0
    routable_classified_nfl: int = 0
    routable_classified_cfb: int = 0
    routable_classified_tennis: int = 0
    routable_classified_other: int = 0
    routable_classified_unresolved: int = 0
    routable_not_classified: int = 0

    #: WHY the routable-but-unresolved markets could not be classified. A count
    #: of "unresolved" says the classifier could not tell; it does not say
    #: whether the metadata was absent, malformed, or present-but-unrecognised,
    #: and those are three different repairs. Named counters rather than a
    #: mapping, so this stays structurally counts-only.
    unresolved_metadata_lookup_failed: int = 0
    unresolved_no_metadata: int = 0
    unresolved_malformed_event_metadata: int = 0
    unresolved_competition_absent: int = 0
    unresolved_competition_unknown: int = 0
    unresolved_competition_ambiguous: int = 0
    unresolved_milestone_conflict: int = 0
    unresolved_evidence_conflict: int = 0
    unresolved_ambiguous_family: int = 0
    unresolved_insufficient: int = 0
    unresolved_reason_not_recorded: int = 0

    #: MEASUREMENT ONLY -- the verdict is unchanged. Deliberately NOT named
    #: with the ``unresolved_`` prefix: that prefix is the reason breakdown, and
    #: a test sums it against the unresolved total. A second axis sharing the
    #: prefix would double-count and break an invariant that is doing real work.
    #: Of the markets a terminal
    #: L1/L2 refusal stopped, how many carried decisive evidence at a lower
    #: level, and which. L4 is KALSHI'S OWN series metadata; L5 is this
    #: project's registry. Falling back on the first would be consulting the
    #: exchange, falling back on the second would be overruling it, so they are
    #: counted apart and no policy is changed on either.
    rescue_by_kalshi_series: int = 0
    rescue_by_local_registry: int = 0
    rescue_by_nothing: int = 0

    #: Where the game dates that WERE established came from. An explicit field
    #: is evidence; a ticker parse is a convention, and knowing the split is
    #: what tells us whether that convention is load-bearing.
    game_date_from_event_field: int = 0
    game_date_from_event_ticker: int = 0

    #: Rows that round-trip through the importer's own required-field rules.
    rows_valid_against_destination: int = 0
    rows_invalid_against_destination: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(vars(self))

    @property
    def refusals_total(self) -> int:
        return sum(
            value
            for name, value in vars(self).items()
            if name.startswith("refused_")
        )

    def render(self) -> str:
        lines = [
            "shadow wagers (built in memory; nothing sent, nothing written):",
            f"  episodes considered: {self.episodes_considered}",
            f"  wagers built: {self.wagers_built}",
            f"  episodes refused: {self.refusals_total}",
            f"    no importable identity: {self.refused_identity_not_importable}",
            f"    still open: {self.refused_not_closed}",
            f"    market not classified (outside the bound): "
            f"{self.refused_market_not_classified}",
            f"    sport unresolved: {self.refused_sport_unresolved}",
            f"    sport has no destination importer: "
            f"{self.refused_no_destination_importer}",
            f"    game date not established: "
            f"{self.refused_game_date_not_established}",
            f"    cost basis incomplete: {self.refused_cost_basis_incomplete}",
            f"    fees incomplete: {self.refused_fees_incomplete}",
            f"    fee allocation ambiguous (reversal): "
            f"{self.refused_fee_allocation_ambiguous}",
            f"    no settlement economics: {self.refused_no_settlement_economics}",
            f"    settlement not binary: {self.refused_non_binary_settlement}",
            "",
            "  routable episodes (importable identity) by classification:",
            f"    MLB: {self.routable_classified_mlb}",
            f"    NFL: {self.routable_classified_nfl}",
            f"    CFB: {self.routable_classified_cfb}",
            f"    TENNIS: {self.routable_classified_tennis}",
            f"    OTHER (not a sport this router carries): "
            f"{self.routable_classified_other}",
            f"    UNRESOLVED: {self.routable_classified_unresolved}",
            "      why the classifier could not tell:",
            f"        metadata lookup failed: "
            f"{self.unresolved_metadata_lookup_failed}",
            f"        no metadata resolved: {self.unresolved_no_metadata}",
            f"        malformed event metadata: "
            f"{self.unresolved_malformed_event_metadata}",
            f"        competition absent: {self.unresolved_competition_absent}",
            f"        competition unknown: {self.unresolved_competition_unknown}",
            f"        competition ambiguous in taxonomy: "
            f"{self.unresolved_competition_ambiguous}",
            f"        milestone conflict: {self.unresolved_milestone_conflict}",
            f"        evidence conflict: {self.unresolved_evidence_conflict}",
            f"        ambiguous family, no league: "
            f"{self.unresolved_ambiguous_family}",
            f"        insufficient authoritative metadata: "
            f"{self.unresolved_insufficient}",
            f"        reason not recorded: {self.unresolved_reason_not_recorded}",
            "      what a fall-through WOULD have decided (measurement only):",
            f"        Kalshi's own series metadata (L4): "
            f"{self.rescue_by_kalshi_series}",
            f"        this project's series registry (L5): "
            f"{self.rescue_by_local_registry}",
            f"        nothing would have decided it: "
            f"{self.rescue_by_nothing}",
            f"    never classified (outside the bound): "
            f"{self.routable_not_classified}",
            "",
            "  game date evidence, where one was established:",
            f"    from an explicit event field: {self.game_date_from_event_field}",
            f"    from the event ticker's date segment: "
            f"{self.game_date_from_event_ticker}",
            "",
            f"  rows valid against the destination contract: "
            f"{self.rows_valid_against_destination}",
            f"  rows INVALID against the destination contract: "
            f"{self.rows_invalid_against_destination}",
            "",
            "  NOTE: nothing here was sent anywhere. These are the rows a router",
            "        WOULD produce, and the refusals are the point: they say what",
            "        routing would cost in accuracy today, before it can cost it.",
        ]
        return "\n".join(lines)


_REFUSAL_COUNTERS = {
    WagerRefusal.IDENTITY_NOT_IMPORTABLE: "refused_identity_not_importable",
    WagerRefusal.NOT_CLOSED: "refused_not_closed",
    WagerRefusal.MARKET_NOT_CLASSIFIED: "refused_market_not_classified",
    WagerRefusal.SPORT_UNRESOLVED: "refused_sport_unresolved",
    WagerRefusal.NO_DESTINATION_IMPORTER: "refused_no_destination_importer",
    WagerRefusal.GAME_DATE_NOT_ESTABLISHED: "refused_game_date_not_established",
    WagerRefusal.COST_BASIS_INCOMPLETE: "refused_cost_basis_incomplete",
    WagerRefusal.FEES_INCOMPLETE: "refused_fees_incomplete",
    WagerRefusal.FEE_ALLOCATION_AMBIGUOUS: "refused_fee_allocation_ambiguous",
    WagerRefusal.NO_SETTLEMENT_ECONOMICS: "refused_no_settlement_economics",
    WagerRefusal.NON_BINARY_SETTLEMENT: "refused_non_binary_settlement",
}


def build_shadow_wager(
    episode: PositionEpisode,
    classification: Classification | None,
    context: MarketContext | None,
) -> tuple[ShadowWager | None, WagerRefusal | None]:
    """Build one routable wager, or say exactly what stopped it.

    The checks run cheapest-and-most-fundamental first, so the refusal reported
    is the FIRST thing wrong rather than an arbitrary one of several. An episode
    with no importable identity is refused on that ground even if its game date
    is also unknown, because the identity is the thing that would have to be
    fixed first.
    """
    # 1. Identity. Both gates -- a provable opening and an earned position
    #    story -- are already folded into this one property, and it is the
    #    property an importer would actually need.
    if not episode.is_importable or episode.source_key is None:
        return None, WagerRefusal.IDENTITY_NOT_IMPORTABLE
    if episode.is_open:
        return None, WagerRefusal.NOT_CLOSED

    # 2. Destination. A sport that was never classified is not an unresolved
    #    sport: nothing was attempted, and conflating the two would read as a
    #    classifier defect rather than the budget the caller chose.
    if classification is None:
        return None, WagerRefusal.MARKET_NOT_CLASSIFIED
    sport = classification.sport
    if sport is None or sport is Sport.UNRESOLVED:
        return None, WagerRefusal.SPORT_UNRESOLVED
    if sport not in SPORTS_WITH_AN_IMPORTER:
        return None, WagerRefusal.NO_DESTINATION_IMPORTER

    # 3. Economics, before the date: a row with no cost basis cannot be fixed
    #    by establishing when it happened.
    if episode.fee_allocation_ambiguous:
        return None, WagerRefusal.FEE_ALLOCATION_AMBIGUOUS
    if not episode.cost_basis_complete or episode.average_entry_price is None:
        return None, WagerRefusal.COST_BASIS_INCOMPLETE
    if not episode.fee_complete:
        return None, WagerRefusal.FEES_INCOMPLETE
    if episode.settlement_revenue_dollars is None or episode.settlement_result is None:
        return None, WagerRefusal.NO_SETTLEMENT_ECONOMICS
    if episode.settlement_result.strip().lower() not in BINARY_RESULTS:
        return None, WagerRefusal.NON_BINARY_SETTLEMENT

    game_date, date_source = resolve_game_date(context)
    if game_date is None:
        return None, WagerRefusal.GAME_DATE_NOT_ESTABLISHED

    # The ledger is denominated on the signed YES axis; the destination records
    # the price of the contract actually held. For a long-NO episode those are
    # complements, and this is the single place the conversion happens.
    long_yes = episode.direction is Direction.LONG_YES
    entry_price = (
        episode.average_entry_price if long_yes else ONE - episode.average_entry_price
    )
    contracts = episode.total_opened_quantity
    contract_cost = entry_price * contracts
    revenue = episode.settlement_revenue_dollars
    stake = contract_cost + episode.fees_paid

    return (
        ShadowWager(
            source_bet_key=episode.source_key,
            market_ticker=episode.ticker,
            event_ticker=_event_ticker(context) if context else None,
            sport=sport,
            game_date=game_date,
            game_date_source=date_source,
            side="YES" if long_yes else "NO",
            contracts=contracts,
            entry_price=entry_price,
            contract_cost=contract_cost,
            total_fees=episode.fees_paid,
            stake=stake,
            gross_settlement_payout=revenue,
            # Net of everything the exchange charged, against everything it
            # paid. Both sides are exchange-stated; nothing is estimated.
            net_profit_loss=revenue - stake,
            # The exchange paid, or it did not. That is the result, read rather
            # than derived from which side the member thought they were on.
            result="WIN" if revenue > ZERO else "LOSS",
            entry_timestamp=episode.opened_at,
        ),
        None,
    )


#: What the importer demands of every row, read from the importer itself.
REQUIRED_ROW_FIELDS = ("gameDate", "stake", "sourceBetKey")


def row_is_valid(row: dict[str, Any]) -> bool:
    """Check a row against the destination's own required-field rules.

    Deliberately narrow: this is the importer's stated contract, not a guess at
    its internals. ``entryPrice`` and ``entryOdds`` are mutually exclusive and
    exactly one must be present.
    """
    for name in REQUIRED_ROW_FIELDS:
        value = row.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            return False
    has_price = row.get("entryPrice") is not None
    has_odds = row.get("entryOdds") is not None
    return has_price != has_odds


def build_shadow_wagers(
    episodes: list[PositionEpisode],
    classifications: dict[str, Classification],
    contexts: dict[str, MarketContext],
) -> tuple[list[ShadowWager], WagerDiagnostics]:
    """Build every wager that can be built, and count every one that cannot."""
    diagnostics = WagerDiagnostics()
    wagers: list[ShadowWager] = []

    unresolved_counters = {
        UnresolvedReason.METADATA_LOOKUP_FAILED: "unresolved_metadata_lookup_failed",
        UnresolvedReason.NO_METADATA: "unresolved_no_metadata",
        UnresolvedReason.MALFORMED_EVENT_METADATA:
            "unresolved_malformed_event_metadata",
        UnresolvedReason.COMPETITION_ABSENT: "unresolved_competition_absent",
        UnresolvedReason.COMPETITION_UNKNOWN: "unresolved_competition_unknown",
        UnresolvedReason.COMPETITION_AMBIGUOUS: "unresolved_competition_ambiguous",
        UnresolvedReason.MILESTONE_CONFLICT: "unresolved_milestone_conflict",
        UnresolvedReason.EVIDENCE_CONFLICT: "unresolved_evidence_conflict",
        UnresolvedReason.AMBIGUOUS_FAMILY: "unresolved_ambiguous_family",
        UnresolvedReason.INSUFFICIENT: "unresolved_insufficient",
    }

    routable_counters = {
        Sport.MLB: "routable_classified_mlb",
        Sport.NFL: "routable_classified_nfl",
        Sport.CFB: "routable_classified_cfb",
        Sport.TENNIS: "routable_classified_tennis",
        Sport.OTHER: "routable_classified_other",
        Sport.UNRESOLVED: "routable_classified_unresolved",
    }

    for episode in episodes:
        diagnostics.episodes_considered += 1
        # Recorded before the refusal, because the question "what ARE the
        # markets this account could route?" is not answered by the reason the
        # wager was refused.
        if episode.is_importable:
            classification = classifications.get(episode.ticker)
            if classification is None:
                diagnostics.routable_not_classified += 1
            else:
                name = routable_counters[classification.sport]
                setattr(diagnostics, name, getattr(diagnostics, name) + 1)
                if classification.sport is Sport.UNRESOLVED:
                    reason = classification.unresolved_reason
                    counter = (
                        unresolved_counters.get(reason)
                        if reason is not None
                        else None
                    )
                    if counter is None:
                        diagnostics.unresolved_reason_not_recorded += 1
                    else:
                        setattr(
                            diagnostics, counter,
                            getattr(diagnostics, counter) + 1,
                        )
                    rescuable = classification.terminal_rescuable_by
                    if rescuable is EvidenceLevel.L4_SERIES_METADATA:
                        diagnostics.rescue_by_kalshi_series += 1
                    elif rescuable is EvidenceLevel.L5_SERIES_REGISTRY:
                        diagnostics.rescue_by_local_registry += 1
                    else:
                        diagnostics.rescue_by_nothing += 1
        wager, refusal = build_shadow_wager(
            episode,
            classifications.get(episode.ticker),
            contexts.get(episode.ticker),
        )
        if refusal is not None:
            name = _REFUSAL_COUNTERS[refusal]
            setattr(diagnostics, name, getattr(diagnostics, name) + 1)
            continue
        assert wager is not None
        wagers.append(wager)
        diagnostics.wagers_built += 1
        if wager.game_date_source is GameDateSource.EVENT_FIELD:
            diagnostics.game_date_from_event_field += 1
        else:
            diagnostics.game_date_from_event_ticker += 1
        if row_is_valid(wager.to_import_row()):
            diagnostics.rows_valid_against_destination += 1
        else:
            diagnostics.rows_invalid_against_destination += 1

    return wagers, diagnostics
