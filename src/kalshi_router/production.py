"""The production wager: what gets delivered, and every gate it must pass.

Phases 3-5. This module answers three questions the research phases deliberately
left open, and answers them in a way that fails closed on each.

**What is one wager?** The submitted ORDER, not the fill and not the position
episode. A fill is an execution detail -- one order can produce many, at
several prices -- and an episode is an exposure lifecycle that can span several
decisions. The order is the decision boundary: one thing the owner submitted.
Two order ids are two submissions, and they are never merged on a time
threshold, because "within thirty seconds" is a guess about intent wearing the
costume of a rule.

**Which wager is this?** A deterministic opaque source key derived only from
immutable exchange evidence. No clock, no run id, no random value, no position
in a batch -- anything that varies between runs would give the same wager two
identities and duplicate it.

**When is an order safe to import?** Only when no further fill can change it.
Importing a half-filled order corrupts its contract count, its VWAP and its
fee total, and the correction is worse than the delay.

Nothing here writes anywhere. Delivery is :mod:`kalshi_router.destination`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .accounting.execution import OrderExecution
from .timeaxis import parse_rfc3339_seconds, seconds_to_rfc3339

# --------------------------------------------------------------- source identity

#: Version prefix on every source key. Bumping it deliberately re-identifies
#: every wager, which is a migration and must never happen by accident -- so it
#: is a literal in source control rather than anything derived.
SOURCE_KEY_VERSION = "kalshi:v1"

#: Domain separation for the digest. Without it, a digest computed here could
#: collide with one computed elsewhere over the same fields for another purpose.
_DIGEST_DOMAIN = "kalshi-bet-router/source-key/v1"


def production_source_key(
    subaccount_number: int | None, ticker: str, order_id: str
) -> str:
    """A stable opaque identity for one submitted order.

    Three fields, all immutable exchange evidence:

    * the **subaccount**, because two subaccounts holding the same market are
      two independent positions and must never share an identity;
    * the **market ticker**, so an order id that somehow repeated across markets
      could not collapse two wagers into one;
    * the **order id**, which is what actually distinguishes one submission.

    The result is opaque on purpose. The downstream ledger is public and does
    not need Kalshi's internal order id to identify a row -- it needs a value
    that is the same every time this order is seen and different for every other
    order. A digest is both, and discloses neither.

    Deliberately absent: the current time, the workflow run id, a random value,
    and the wager's position in a batch. Each of those would give the same order
    a new identity on the next run, and the next run would import it again.
    """
    if not ticker or not order_id:
        raise ValueError("a source key needs both a market and an order")
    account = "default" if subaccount_number is None else str(subaccount_number)
    # LENGTH-PREFIXED, not separator-joined. A separator is only unambiguous
    # while no component can contain it, and "a ticker will never contain this
    # byte" is an assumption rather than a guarantee -- the first draft used
    # \x1f and a test forged a collision between ("A", "B\x1fC") and
    # ("A\x1fB", "C") immediately. Prefixing each field with its own byte
    # length makes the encoding injective for every possible input.
    material = "".join(
        f"{len(field.encode('utf-8'))}:{field}"
        for field in (_DIGEST_DOMAIN, account, ticker, order_id)
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{SOURCE_KEY_VERSION}:{digest}"


# ------------------------------------------------------------- the health signal


class HealthState(str, Enum):
    """What one production run means, in one word a human can act on.

    The mission asked for three states to stay distinguishable. In practice
    there are five, because "nothing was delivered" collapses four genuinely
    different situations and collapsing them is what makes an alert useless:

    * a quiet account (nothing to do),
    * activity this system is DESIGNED never to route (not a fault),
    * activity held by a gate that will open on its own (wait),
    * activity this system cannot record and will not fix by waiting (act).

    A boolean cannot say which, and "HEALTHY NO-OP: False" reads like bad news
    whether the cause is a wager pending settlement or a wager permanently
    unrecordable.
    """

    #: No post-cutover orders at all. Nothing to do, nothing wrong.
    HEALTHY_NO_OP = "healthy_no_op"
    #: At least one wager was eligible and handed to a destination.
    DELIVERED = "delivered"
    #: Post-cutover orders exist; every one is held by a gate that opens on its
    #: own. The next run is the fix. No action.
    DEFERRED = "deferred"
    #: Post-cutover orders exist and every one is terminal BY DESIGN -- history,
    #: or a sport with no importer. Correct behaviour, not a fault.
    NOT_ROUTABLE = "not_routable"
    #: At least one post-cutover order is a wager the owner really made that
    #: this system cannot record, and waiting will not change that. This is the
    #: only state that asks for a human.
    BLOCKED = "blocked"


# ------------------------------------------------------------ production cutover

#: The moment production recording begins, as a fixed instant.
#:
#: Everything at or before this is HISTORY: validation material, never
#: automatically imported, because the downstream ledgers already contain
#: manually entered wagers and a blind backfill would duplicate them.
#:
#: This is a literal, not a derived value, and that is the whole point. A
#: cutover of "the last successful run" moves every time a run fails, silently
#: widening or narrowing what gets imported; a cutover of "24 hours ago" moves
#: continuously. Either would make the set of imported wagers depend on when the
#: system happened to run rather than on what the owner actually did.
PRODUCTION_CUTOVER_ISO = "2026-09-15T00:00:00Z"


def production_cutover_seconds() -> Decimal:
    """The cutover on the shared epoch-second axis."""
    parsed = parse_rfc3339_seconds(PRODUCTION_CUTOVER_ISO)
    if parsed is None:  # pragma: no cover - the constant is checked by a test
        raise ValueError("PRODUCTION_CUTOVER_ISO is not a readable timestamp")
    return parsed


def is_after_cutover(order: OrderExecution) -> bool:
    """Whether this order was SUBMITTED after production began.

    Measured on the order's **first** execution, not its last. The first fill is
    when the decision met the market; a late fill on an old order does not make
    it a new wager, and keying on the last one would let a pre-cutover order
    drift across the line and be imported as though it were new.
    """
    return order.first_execution_time > production_cutover_seconds()


# -------------------------------------------------------------- order finality

class OrderFinality(str, Enum):
    """Whether an order can still change.

    An order that can still receive a fill has a provisional contract count, a
    provisional VWAP and a provisional fee total. Importing one means either
    publishing a wrong number or teaching the destination to accept corrections
    to canonical rows -- and the second is a far larger commitment than waiting.
    """

    #: The market is no longer open, so no further fill is possible. This is the
    #: exchange saying so, not an inference.
    FINAL_MARKET_CLOSED = "final_market_closed"
    #: The market is still open, but this order's last fill is older than the
    #: stabilization window and the window is supported by measured behaviour.
    FINAL_STABLE = "final_stable"
    #: A fill landed recently enough that another could still follow.
    PENDING_RECENT_FILL = "pending_recent_fill"
    #: Market status could not be established and the window does not apply.
    #: Fail closed: unknown finality is never final.
    UNKNOWN = "unknown"


#: Market statuses in which no further fill is possible.
CLOSED_MARKET_STATUSES = frozenset(
    {"closed", "settled", "finalized", "determined", "expired"}
)

#: How long an order must go without a new fill before an OPEN market's order is
#: treated as complete.
#:
#: This number is not a guess: :mod:`kalshi_router.finality` measures the
#: observed first-to-last fill span of every order in the account's history, and
#: the audit reports the distribution. The window is set far above the observed
#: maximum, so a "stable" verdict describes behaviour this account has actually
#: exhibited rather than behaviour that seems plausible.
STABILIZATION_SECONDS = Decimal(900)  # 15 minutes


def assess_finality(
    order: OrderExecution,
    now: Decimal,
    market_status: str | None,
    allow_stabilization: bool = False,
) -> OrderFinality:
    """Decide whether this order is safe to import.

    ``market_status`` comes from ``GET /markets/{ticker}``, which is read-only
    and already on the client's allowlist. Note what is NOT used: Kalshi's order
    endpoint. ``GET /portfolio/orders/{id}`` would answer this directly, but that
    path is the same one that creates and cancels orders, and this project's
    allowlist refuses the whole prefix. A test pins that refusal. Answering the
    question from market status and fill timing keeps the trading surface
    unreachable by construction rather than by care.

    ``allow_stabilization`` is off by default, so the only finality granted
    without configuration is the authoritative one.
    """
    if market_status is not None:
        if market_status.strip().lower() in CLOSED_MARKET_STATUSES:
            return OrderFinality.FINAL_MARKET_CLOSED

    if not allow_stabilization:
        return OrderFinality.UNKNOWN

    if now - order.last_execution_time >= STABILIZATION_SECONDS:
        return OrderFinality.FINAL_STABLE
    return OrderFinality.PENDING_RECENT_FILL


#: The finality verdicts that permit an import.
FINAL_VERDICTS = frozenset(
    {OrderFinality.FINAL_MARKET_CLOSED, OrderFinality.FINAL_STABLE}
)


# ------------------------------------------------------------- production gates

class ProductionRefusal(str, Enum):
    """Why an order was not auto-imported.

    Every value names a gate that did not pass. A refusal is the normal,
    expected outcome for most orders -- historical ones especially -- and it is
    never an error. What it must never be is silent.
    """

    #: Submitted at or before the cutover. History is validation material, not
    #: import material: the destination ledgers already hold manually entered
    #: wagers, and a blind backfill would duplicate them.
    BEFORE_CUTOVER = "before_cutover"
    #: The order could still receive another fill, so its contract count, VWAP
    #: and fee total are all provisional.
    ORDER_NOT_FINAL = "order_not_final"
    #: A fill in this order carried no price, so there is no VWAP to state.
    NO_EXECUTION_PRICE = "no_execution_price"
    #: A fill in this order carried no fee, so the total would be a floor rather
    #: than a total. Never reconstructed from the fee schedule.
    FEES_INCOMPLETE = "fees_incomplete"
    #: The market was never classified -- outside a bounded run's sweep.
    MARKET_NOT_CLASSIFIED = "market_not_classified"
    #: Classification ran and could not name the sport authoritatively.
    SPORT_UNRESOLVED = "sport_unresolved"
    #: The sport is known and its repository cannot accept a canonical wager.
    NO_DESTINATION_IMPORTER = "no_destination_importer"
    #: The destination requires a contest date this evidence cannot establish.
    GAME_DATE_NOT_ESTABLISHED = "game_date_not_established"
    #: A sell/reduction, which is not an original wager and has no agreed
    #: downstream representation yet.
    REDUCTION_NOT_REPRESENTABLE = "reduction_not_representable"


@dataclass(frozen=True)
class ProductionWager:
    """One submitted order, ready for delivery.

    SENSITIVE: carries a ticker, a quantity, a price and a fee. Never rendered;
    only counted. What reaches the destination is the row, and what reaches a
    public log is a count.
    """

    source_key: str
    market_ticker: str
    sport: str
    game_date: str
    side: str
    contracts: Decimal
    #: Quantity-weighted across every fill of this order. Never an arithmetic
    #: mean: an unevenly filled order's mean price is not its cost.
    vwap_price: Decimal
    total_fees: Decimal
    stake: Decimal
    first_execution_time: Decimal
    last_execution_time: Decimal
    fill_count: int
    finality: OrderFinality


# ------------------------------------------------ the production filter itself

@dataclass
class ProductionDiagnostics:
    """Counts only. The health signal a scheduled run publishes.

    Three outcomes have to stay distinguishable, because they call for
    completely different responses: a HEALTHY NO-OP (nothing new, nothing
    wrong), a DEFERRAL (something is pending and will resolve itself), and a
    FAILURE (something is wrong and will not).
    """

    orders_considered: int = 0
    eligible: int = 0

    refused_before_cutover: int = 0
    refused_order_not_final: int = 0
    refused_no_execution_price: int = 0
    refused_fees_incomplete: int = 0
    refused_market_not_classified: int = 0
    refused_sport_unresolved: int = 0
    refused_no_destination_importer: int = 0
    refused_game_date_not_established: int = 0
    refused_reduction_not_representable: int = 0

    #: Post-cutover orders, before any other gate. This is the number that says
    #: whether the system has anything at all to do -- zero means a healthy
    #: no-op, and every refusal below it is a deferral or a defect.
    orders_after_cutover: int = 0

    #: Pre-cutover orders admitted by an explicit recovery run. Zero on every
    #: normal run, and a test pins that -- a non-zero value here in a scheduled
    #: run would mean the cutover had stopped being the boundary it is.
    pre_cutover_admitted: int = 0

    #: WHY a post-cutover market could not be classified.
    #:
    #: "sport unresolved: 1" tells the owner a wager was refused; it does not
    #: tell them whether that is a taxonomy gap they should close or a market
    #: this router is right to refuse forever. Those call for opposite
    #: responses, so they are counted separately. Named int fields rather than a
    #: mapping, so the diagnostics stay structurally counts-only and no
    #: competition string can ride out in a key.
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
    unresolved_reason_unavailable: int = 0

    #: Which finality verdict post-cutover orders received.
    final_market_closed: int = 0
    final_stable: int = 0
    pending_recent_fill: int = 0
    finality_unknown: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(vars(self))

    @property
    def refusals_total(self) -> int:
        return sum(v for k, v in vars(self).items() if k.startswith("refused_"))

    @property
    def is_healthy_no_op(self) -> bool:
        """Nothing to do, and nothing wrong with that.

        Distinct from a deferral: a post-cutover order held back by a gate is
        the system working, but it is not nothing. Kept as its own property
        because it is the one question with a yes/no answer; everything else
        goes through :attr:`health`.
        """
        return self.orders_after_cutover == 0

    @property
    def health(self) -> HealthState:
        """One word for what this run means. See :class:`HealthState`.

        Ordered by ATTENTION REQUIRED, not by severity of outcome: a run that
        delivered one wager and cannot record another is BLOCKED, because the
        delivery needs nothing from anyone and the refusal does.
        """
        if self.blocked_orders:
            return HealthState.BLOCKED
        if self.deferred_orders:
            return HealthState.DEFERRED
        if self.eligible:
            return HealthState.DELIVERED
        if self.orders_after_cutover:
            return HealthState.NOT_ROUTABLE
        return HealthState.HEALTHY_NO_OP

    @property
    def deferred_orders(self) -> int:
        """Post-cutover orders held by a gate that opens on its own."""
        return sum(
            getattr(self, _REFUSAL_COUNTERS[refusal])
            for refusal in SELF_RESOLVING_REFUSALS
        )

    @property
    def blocked_orders(self) -> int:
        """Post-cutover wagers this system cannot record, and waiting will not
        change that. The only number that asks for a human."""
        return sum(
            getattr(self, _REFUSAL_COUNTERS[refusal])
            for refusal in NEEDS_ATTENTION_REFUSALS
        )

    def render(self) -> str:
        lines = [
            "production filter (counts only; nothing delivered by this report):",
            f"  orders considered: {self.orders_considered}",
            f"  after the production cutover: {self.orders_after_cutover}",
            f"  pre-cutover admitted by recovery: {self.pre_cutover_admitted}",
            f"  ELIGIBLE for delivery: {self.eligible}",
            "",
            "  finality of the orders considered:",
            f"    final, market closed: {self.final_market_closed}",
            f"    final, stabilized: {self.final_stable}",
            f"    pending, recent fill: {self.pending_recent_fill}",
            f"    unknown: {self.finality_unknown}",
            "",
            f"  refused: {self.refusals_total}",
            f"    before the cutover (history, not an error): "
            f"{self.refused_before_cutover}",
            f"    order not final: {self.refused_order_not_final}",
            f"    no execution price: {self.refused_no_execution_price}",
            f"    fees incomplete: {self.refused_fees_incomplete}",
            f"    market not classified: {self.refused_market_not_classified}",
            f"    sport unresolved: {self.refused_sport_unresolved}",
            f"    no destination importer: {self.refused_no_destination_importer}",
            f"    game date not established: "
            f"{self.refused_game_date_not_established}",
            f"    reduction not representable: "
            f"{self.refused_reduction_not_representable}",
            "",
            "",
            # UNIT CHANGE, and it is load-bearing. Every count above this
            # line is ORDERS; these are distinct MARKET TICKERS, because a
            # market is classified once however many orders were placed on it.
            # A live run refused 34 orders under reasons summing to 32 -- two
            # markets carried two orders each -- and with both under one
            # heading and no unit stated, the only way to read that was as an
            # arithmetic error in the report.
            "  why those markets were unresolved "
            "(distinct MARKETS, not orders -- one market can carry several):",
            f"    metadata lookup failed: {self.unresolved_metadata_lookup_failed}",
            f"    no metadata resolved: {self.unresolved_no_metadata}",
            f"    malformed event metadata: {self.unresolved_malformed_event_metadata}",
            f"    competition absent: {self.unresolved_competition_absent}",
            f"    competition unknown to the taxonomy: "
            f"{self.unresolved_competition_unknown}",
            f"    competition ambiguous in the taxonomy: "
            f"{self.unresolved_competition_ambiguous}",
            f"    milestone conflict: {self.unresolved_milestone_conflict}",
            f"    evidence conflict: {self.unresolved_evidence_conflict}",
            f"    ambiguous family without a league: {self.unresolved_ambiguous_family}",
            f"    insufficient authoritative metadata: {self.unresolved_insufficient}",
            f"    reason unavailable: {self.unresolved_reason_unavailable}",
            "",
            f"  deferred (a gate that opens on its own): {self.deferred_orders}",
            f"  BLOCKED (cannot be recorded, and waiting will not help): "
            f"{self.blocked_orders}",
            f"  HEALTH: {self.health.value}",
        ]
        return "\n".join(lines)


#: Refusals that the NEXT RUN can clear without anyone doing anything.
#:
#: Exactly one qualifies today: an order that is not final yet becomes final
#: when its market closes or its stabilization window elapses. Nothing else on
#: this list is a matter of waiting.
SELF_RESOLVING_REFUSALS = frozenset({ProductionRefusal.ORDER_NOT_FINAL})

#: Refusals that are CORRECT AND PERMANENT, and therefore not a fault.
#:
#: A pre-cutover order is history and was never going to be imported. A sport
#: with no destination importer is a design fact the owner already knows -- the
#: other three repositories mechanically refuse to hold a wager, which is their
#: decision, not this system's failure.
BY_DESIGN_REFUSALS = frozenset({
    ProductionRefusal.BEFORE_CUTOVER,
    ProductionRefusal.NO_DESTINATION_IMPORTER,
})

#: Everything else: a wager the owner really made that this system cannot
#: record. Derived by SUBTRACTION rather than listed, so a refusal added later
#: is treated as needing attention until someone deliberately says otherwise --
#: the safe direction for a list whose job is to decide what gets ignored.
NEEDS_ATTENTION_REFUSALS = (
    frozenset(ProductionRefusal) - SELF_RESOLVING_REFUSALS - BY_DESIGN_REFUSALS
)


_REFUSAL_COUNTERS = {
    ProductionRefusal.BEFORE_CUTOVER: "refused_before_cutover",
    ProductionRefusal.ORDER_NOT_FINAL: "refused_order_not_final",
    ProductionRefusal.NO_EXECUTION_PRICE: "refused_no_execution_price",
    ProductionRefusal.FEES_INCOMPLETE: "refused_fees_incomplete",
    ProductionRefusal.MARKET_NOT_CLASSIFIED: "refused_market_not_classified",
    ProductionRefusal.SPORT_UNRESOLVED: "refused_sport_unresolved",
    ProductionRefusal.NO_DESTINATION_IMPORTER: "refused_no_destination_importer",
    ProductionRefusal.GAME_DATE_NOT_ESTABLISHED: "refused_game_date_not_established",
    ProductionRefusal.REDUCTION_NOT_REPRESENTABLE: "refused_reduction_not_representable",
}

_FINALITY_COUNTERS = {
    OrderFinality.FINAL_MARKET_CLOSED: "final_market_closed",
    OrderFinality.FINAL_STABLE: "final_stable",
    OrderFinality.PENDING_RECENT_FILL: "pending_recent_fill",
    OrderFinality.UNKNOWN: "finality_unknown",
}


def evaluate_order(
    order: OrderExecution,
    classification_sport: str | None,
    game_date: str | None,
    market_status: str | None,
    now: Decimal,
    destinations: frozenset[str],
    allow_stabilization: bool = False,
    include_pre_cutover: bool = False,
) -> tuple[ProductionWager | None, ProductionRefusal | None, OrderFinality | None]:
    """Apply every gate to one order, cheapest and most decisive first.

    The ordering is deliberate. The cutover is checked before anything else
    because it is free and it rejects almost everything -- and because a
    pre-cutover order is not a failure of any later gate, so reporting it as one
    would make the history look broken.

    ``classification_sport`` is the sport's name or ``None`` when the market was
    never classified; the caller distinguishes "not attempted" from "attempted
    and unresolved" by passing ``"UNRESOLVED"`` for the latter.
    """
    # RECOVERY ONLY. ``include_pre_cutover`` defaults to False at every layer,
    # so a caller that forgets about it gets the safe behaviour. It exists for
    # the recovery path, where the owner has decided a specific historical
    # window should be imported despite the destination already holding
    # manually entered wagers -- and that decision is theirs, stated at
    # dispatch, never inferred from a run's circumstances.
    #
    # It is safe only because it is not a bypass of the duplicate check: the
    # source key is derived from the order, so a pre-cutover order already in
    # the ledger under the same key lands on DUPLICATE_NOOP, and one recorded
    # manually with DIFFERENT economics lands on CONFLICT and is refused.
    if not include_pre_cutover and not is_after_cutover(order):
        return None, ProductionRefusal.BEFORE_CUTOVER, None

    finality = assess_finality(order, now, market_status, allow_stabilization)
    if finality not in FINAL_VERDICTS:
        return None, ProductionRefusal.ORDER_NOT_FINAL, finality

    # Economics before destination: a wager with no price cannot be fixed by
    # finding somewhere to put it.
    if order.vwap_price is None:
        return None, ProductionRefusal.NO_EXECUTION_PRICE, finality
    if order.total_fee is None:
        return None, ProductionRefusal.FEES_INCOMPLETE, finality

    if classification_sport is None:
        return None, ProductionRefusal.MARKET_NOT_CLASSIFIED, finality
    if classification_sport in ("UNRESOLVED", "OTHER"):
        return None, ProductionRefusal.SPORT_UNRESOLVED, finality
    if classification_sport not in destinations:
        return None, ProductionRefusal.NO_DESTINATION_IMPORTER, finality

    if not game_date:
        return None, ProductionRefusal.GAME_DATE_NOT_ESTABLISHED, finality

    long_yes = order.outcome_side.value == "yes"
    contract_price = order.vwap_price if long_yes else Decimal(1) - order.vwap_price
    contracts = order.total_quantity
    stake = contract_price * contracts + order.total_fee

    return (
        ProductionWager(
            source_key=production_source_key(
                order.subaccount_number, order.ticker, order.order_id
            ),
            market_ticker=order.ticker,
            sport=classification_sport,
            game_date=game_date,
            side="YES" if long_yes else "NO",
            contracts=contracts,
            vwap_price=contract_price,
            total_fees=order.total_fee,
            stake=stake,
            first_execution_time=order.first_execution_time,
            last_execution_time=order.last_execution_time,
            fill_count=order.fill_count,
            finality=finality,
        ),
        None,
        finality,
    )


#: UnresolvedReason -> the counter it increments. Keyed by the classifier's own
#: enum so a new reason is a KeyError in a test rather than a silent zero.
UNRESOLVED_COUNTERS: dict[str, str] = {
    "metadata_lookup_failed": "unresolved_metadata_lookup_failed",
    "no_metadata_resolved": "unresolved_no_metadata",
    "malformed_event_metadata": "unresolved_malformed_event_metadata",
    "competition_absent": "unresolved_competition_absent",
    "competition_unknown": "unresolved_competition_unknown",
    "competition_ambiguous_in_taxonomy": "unresolved_competition_ambiguous",
    "milestone_competition_conflict": "unresolved_milestone_conflict",
    "evidence_conflict": "unresolved_evidence_conflict",
    "ambiguous_sport_family_without_league": "unresolved_ambiguous_family",
    "insufficient_authoritative_metadata": "unresolved_insufficient",
}


def evaluate_production(
    orders,
    sports: dict[str, str],
    game_dates: dict[str, str],
    market_statuses: dict[str, str],
    now: Decimal,
    destinations: frozenset[str],
    allow_stabilization: bool = False,
    include_pre_cutover: bool = False,
) -> tuple[list[ProductionWager], ProductionDiagnostics]:
    """Filter every order down to the ones safe to deliver, and count the rest."""
    diagnostics = ProductionDiagnostics()
    eligible: list[ProductionWager] = []

    for order in orders:
        diagnostics.orders_considered += 1
        wager, refusal, finality = evaluate_order(
            order,
            sports.get(order.ticker),
            game_dates.get(order.ticker),
            market_statuses.get(order.ticker),
            now,
            destinations,
            allow_stabilization,
            include_pre_cutover,
        )
        if include_pre_cutover and not is_after_cutover(order):
            # Counted as what it is, so a recovery run's report still says how
            # much of what it delivered was history rather than new activity.
            diagnostics.pre_cutover_admitted += 1
        if refusal is not ProductionRefusal.BEFORE_CUTOVER:
            diagnostics.orders_after_cutover += 1
        if finality is not None:
            name = _FINALITY_COUNTERS[finality]
            setattr(diagnostics, name, getattr(diagnostics, name) + 1)
        if refusal is not None:
            counter = _REFUSAL_COUNTERS[refusal]
            setattr(diagnostics, counter, getattr(diagnostics, counter) + 1)
            continue
        assert wager is not None
        eligible.append(wager)
        diagnostics.eligible += 1

    return eligible, diagnostics


# ------------------------------------------------------- the destination row

def to_import_row(wager: ProductionWager) -> dict:
    """The importer's row shape for one production wager.

    Fields the IMPORTER owns -- betId, validationStatus, provenance, createdAt
    -- are absent. A router that emitted them would be inventing values the
    destination produces.

    Fields that would claim MODEL PROVENANCE are absent too, and that absence is
    deliberate. A Kalshi execution proves the owner placed this bet. It does not
    prove anything recommended it, so recommendationId, modelEvaluationId,
    productionRunId and modelFairProbability are all left unset rather than
    filled in with something plausible.

    Money crosses to float here because that is what the destination schema
    declares. Every value was computed in Decimal and is converted once, at this
    boundary; no arithmetic happens after it.
    """
    return {
        "sourceBetKey": wager.source_key,
        "gameDate": wager.game_date,
        "marketTicker": wager.market_ticker,
        "side": wager.side,
        "stake": float(wager.stake),
        "entryPrice": float(wager.vwap_price),
        # Quantity, NOT an economics field: the destination's canonical builder
        # takes `contracts` as its own parameter, and it is deliberately absent
        # from _EXECUTION_ECONOMICS_FIELDS. It also comes straight from the
        # fills' own quantities -- never divided back out of the stake, which
        # would turn a rounding artefact into a contract count.
        "contracts": float(wager.contracts),
        "executionEconomics": execution_economics(wager),
        "status": "pending",
        "source": "OTHER",
        "entryMethod": "IMPORTED_RECEIPT",
        "trackingType": "REAL",
    }


def execution_economics(wager: ProductionWager) -> dict:
    """The exact exchange economics, in the destination's canonical shape.

    NESTED, NOT FLAT, and that distinction cost a real wager. These fields were
    previously emitted at the top level of the row, where the destination's
    batch importer never looks: it reads `row["executionEconomics"]` and expands
    it through `_execution_economics_defaults`. So every one of them silently
    became null on the first genuine delivery -- including `totalFees`, which is
    the entire reason for routing from actual fills rather than reconstructing a
    fee schedule.

    Every key here is checked against the destination's own
    `_EXECUTION_ECONOMICS_FIELDS`, which REFUSES an unknown key rather than
    dropping it. That refusal is a feature and this function is written to rely
    on it: a typo fails the import loudly instead of vanishing, which is exactly
    what did not happen last time.

    WHAT IS DELIBERATELY LEFT NULL. Only what this order actually evidences is
    filled in. `exitFees`, `grossCashReturned`, `grossSettlementPayout`,
    `exitSaleProceeds` and `realizedROI` belong to a position that has closed,
    and this one has not. `feeType` would need taker/maker resolved across every
    fill of the order, which the aggregate does not currently carry -- so it
    stays null rather than being guessed at MIXED. A null here means "not
    established", and filling it to avoid a null would be the lie.
    """
    contract_cost = wager.vwap_price * wager.contracts
    return {
        # contracts x VWAP. The price is quantity-weighted across the order's
        # own fills, so this is the cost actually paid, not a nominal one.
        "contractCost": float(contract_cost),
        "averageFillPrice": float(wager.vwap_price),
        # The exchange's own fee, summed from its fills. Nothing here is
        # reconstructed from a fee schedule, which is what the lower tiers of
        # feeStatus and feeSource mean.
        "totalFees": float(wager.total_fees),
        "actualCashConsumed": float(contract_cost + wager.total_fees),
        "executionStatus": "HELD_TO_SETTLEMENT",
        "feeStatus": "ACTUAL_API_FILL",
        # EXACT_ORDER_EXECUTION rather than EXACT_API_FILL: the fee is summed
        # over one ORDER's fills, which is the unit this router records.
        "feeSource": "EXACT_ORDER_EXECUTION",
        "economicsSource": "EXACT_API_EXECUTION",
        # The destination maps EXACT_API_EXECUTION to HIGH in its own
        # _CONFIDENCE_BY_SOURCE table; this states that mapping rather than
        # inventing a confidence of its own.
        "economicsConfidence": "HIGH",
    }


# --------------------------------------------- the other destinations' shapes
#
# EACH DESTINATION HAS ITS OWN VOCABULARY, and they genuinely differ. The MLB
# ledger speaks camelCase and calls the quantity-weighted price `entryPrice`
# with the economics nested under `executionEconomics`. The NFL kind speaks
# snake_case and calls it `actual_price`. The CFB ledger also speaks snake_case
# but calls it `execution_price`. Emitting one shape and hoping is exactly how
# the MLB economics silently became null, so each destination gets a function
# written against ITS schema.


def to_nfl_import_row(wager: ProductionWager, import_batch_id: str) -> dict:
    """One wager in the NFL repository's ``imported_wager.v1`` shape.

    ABSENT ON PURPOSE -- `imported_wager_id`. That is the destination's own
    identity, and this router does not mint destination identities: it omits
    MLB's `betId` for the same reason. The NFL importer derives it
    deterministically from `source_bet_key`, so a re-run lands on the same
    record rather than a second one.

    ABSENT ON PURPOSE -- `season` and `week`. The NFL record requires both, and
    this router knows only the contest's date. Turning a date into an NFL week
    means consulting the NFL calendar, which is the destination's own knowledge
    and not evidence this router holds. `nfl_edge.data.nfl_calendar` resolves
    both from the REAL schedule at import time. A week inferred here from a date
    would be a guess wearing a fact's clothes, and the `contracts` field that
    landed null in the MLB ledger is what that costs.

    ABSENT ON PURPOSE -- every recommendation-provenance field. A Kalshi
    execution proves the owner placed this bet; it proves nothing about what
    recommended it. The NFL record refuses those fields outright, so sending one
    would fail the import rather than quietly assert model backing.
    """
    return {
        "source_bet_key": wager.source_key,
        "import_batch_id": import_batch_id,
        "entry_method": "IMPORTED_RECEIPT",
        "game_date": wager.game_date,
        "market_ticker": wager.market_ticker,
        "side": wager.side,
        "executed_at": seconds_to_rfc3339(wager.first_execution_time),
        "contracts": float(wager.contracts),
        # NFL calls the quantity-weighted average `actual_price`, and its
        # docstring is explicit that it is an average over the ORDER's own
        # fills rather than a stored guess at a price nobody paid.
        "actual_price": float(wager.vwap_price),
        "stake": float(wager.stake),
        "fees_paid": float(wager.total_fees),
        # Read from the exchange's own fills, so not estimated. The NFL record
        # refuses a net_profit_loss stated from estimated fees, and this is the
        # flag that distinguishes the two.
        "fees_are_estimated": False,
        "fee_state": "ACTUAL_API_FILL",
        "venue": "kalshi",
    }


def to_cfb_import_row(wager: ProductionWager, import_batch_id: str) -> dict:
    """One wager in the CFB repository's ``cfb_accounted_wager.v1`` shape.

    ABSENT ON PURPOSE -- `wager_id`, for the same reason `imported_wager_id` is
    absent above: it is the destination's identity to mint, deterministically
    from `source_bet_key`.

    `season` and `week` are OPTIONAL in the CFB record, so they are simply not
    sent. Optional is not an invitation to fill something in.

    RECORDING IS NOT ENDORSING. The CFB model is research-only and stays that
    way; this row says the owner placed a College Football bet, and says nothing
    about whether anything recommended it. The CFB ledger refuses every
    model-provenance field outright, including `model_supported=False`, which
    still asserts the model had an opinion.
    """
    return {
        "source_bet_key": wager.source_key,
        "import_batch_id": import_batch_id,
        "entry_method": "IMPORTED_RECEIPT",
        "game_date": wager.game_date,
        "market_ticker": wager.market_ticker,
        "side": wager.side,
        "executed_at": seconds_to_rfc3339(wager.first_execution_time),
        "contracts": float(wager.contracts),
        # CFB calls it `execution_price`; NFL calls the same number
        # `actual_price`. Same quantity-weighted average, different vocabulary.
        "execution_price": float(wager.vwap_price),
        "stake": float(wager.stake),
        "fees_paid": float(wager.total_fees),
        "fees_are_estimated": False,
        "venue": "kalshi",
    }


#: Which emitter speaks each destination's language. A sport absent from this
#: map has no payload shape and must be refused rather than sent in some other
#: sport's vocabulary.
ROW_BUILDERS = {
    "NFL": to_nfl_import_row,
    "CFB": to_cfb_import_row,
}
