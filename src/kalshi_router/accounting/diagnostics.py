"""Privacy-safe accounting diagnostics.

Every field here is a **count** or a **boolean**.  There is no field capable of
holding a ticker, a fill id, an order id, an episode identifier, a quantity, a
price, a fee, a monetary total or a P&L figure -- and a test asserts that
structurally, the same way :class:`~kalshi_router.aggregate.AuditReport` is
guarded.

Monetary and quantity values exist inside the engine; none of them reach here.
"""

from __future__ import annotations

from dataclasses import dataclass

from .engine import AccountingResult, HistoryCompleteness
from .position import TransitionKind


@dataclass
class AccountingDiagnostics:
    """Counts only.  Safe to print in a public Actions log."""

    fills_replayed: int = 0
    duplicate_fills_ignored: int = 0

    orders_observed: int = 0
    orders_with_partial_fills: int = 0
    fills_without_order_id: int = 0
    orders_without_a_price: int = 0

    markets_with_activity: int = 0
    #: Distinct subaccounts seen. Positions are never netted across them.
    subaccounts_observed: int = 0
    fills_without_a_subaccount_number: int = 0
    position_transitions: int = 0
    positions_opened: int = 0
    positions_increased: int = 0
    positions_reduced: int = 0
    positions_closed: int = 0
    positions_reversed: int = 0
    #: The exchange closed the market itself at expiry, with no fill.
    positions_settled: int = 0

    settlements_applied: int = 0
    settlements_without_a_position: int = 0
    settlements_refused_unreconciled: int = 0
    settlements_refused_ambiguous_subaccount: int = 0

    episodes_observed: int = 0
    episodes_open_at_end: int = 0
    episodes_closed: int = 0
    episodes_with_complete_cost_basis: int = 0
    episodes_with_complete_fees: int = 0
    #: Episodes touched by a cross-zero reversal, where the execution's fee spans
    #: two episodes and Kalshi documents no allocation rule.
    episodes_with_ambiguous_fee_allocation: int = 0
    episodes_provable: int = 0
    #: Episodes whose opening boundary is provable, so their identity is safe for
    #: a future idempotent import.
    episodes_with_importable_identity: int = 0

    #: Counts every fee-bearing EVENT, not only fills: a settlement carries its
    #: own fee_cost and is as much an economic event as an execution. Named for
    #: what it counts, after settlement replay made "fills" wrong.
    events_with_fee_field: int = 0
    events_missing_fee_field: int = 0
    orders_with_complete_fees: int = 0
    orders_missing_fees: int = 0
    #: True when every replayed fill reported a fee, so the account-level total
    #: is exact. Per-episode allocation can still be ambiguous (see above).
    account_fee_total_complete: bool = False

    #: False whenever the replay ran on a bounded window rather than a complete
    #: history.  The audit must not describe positions as the account's real
    #: state when this is False.
    #: Incremented when the replay could not run at all (for example a fill with
    #: no usable execution timestamp, which cannot be ordered deterministically).
    accounting_schema_failures: int = 0

    claims_complete_position_state: bool = False
    #: The exchange's own view contradicts the replay's position state.
    #: A complete FILL history is necessary but not sufficient for authority:
    #: settlements close positions without a fill, and the settlement route
    #: does not reach as far back as the archive fill route does.
    position_state_contradicted_by_exchange: bool = False
    history_is_complete: bool = False

    def as_dict(self) -> dict[str, int | bool]:
        return dict(vars(self))

    def render(self) -> str:
        lines = [
            "shadow accounting diagnostics (no routing, no persistence):",
            f"  fills replayed: {self.fills_replayed}",
            f"  duplicate fills ignored on replay: {self.duplicate_fills_ignored}",
            "",
            f"  orders observed: {self.orders_observed}",
            f"  orders with partial fills (>1 execution): {self.orders_with_partial_fills}",
            f"  fills without an order id: {self.fills_without_order_id}",
            f"  orders with no interpretable price: {self.orders_without_a_price}",
            "",
            f"  markets with position activity: {self.markets_with_activity}",
            f"  distinct subaccounts observed: {self.subaccounts_observed}",
            f"  fills without a subaccount number: {self.fills_without_a_subaccount_number}",
            f"  position transitions: {self.position_transitions}",
            f"    opened: {self.positions_opened}",
            f"    increased: {self.positions_increased}",
            f"    reduced: {self.positions_reduced}",
            f"    closed: {self.positions_closed}",
            f"    reversed: {self.positions_reversed}",
            f"    settled by the exchange: {self.positions_settled}",
            "",
            f"  settlements applied: {self.settlements_applied}",
            f"    naming a market not in this window: "
            f"{self.settlements_without_a_position}",
            f"    REFUSED, size disagreed with the replay: "
            f"{self.settlements_refused_unreconciled}",
            f"    REFUSED, ticker held in several subaccounts: "
            f"{self.settlements_refused_ambiguous_subaccount}",
            "",
            f"  position episodes observed: {self.episodes_observed}",
            f"    still open at end of window: {self.episodes_open_at_end}",
            f"    closed within window: {self.episodes_closed}",
            f"    with complete cost basis: {self.episodes_with_complete_cost_basis}",
            f"    with complete exchange fees: {self.episodes_with_complete_fees}",
            f"    provable from supplied history: {self.episodes_provable}",
            f"    with an importable identity: {self.episodes_with_importable_identity}",
            f"    with ambiguous fee allocation (reversal): "
            f"{self.episodes_with_ambiguous_fee_allocation}",
            "",
            f"  fee-bearing events (fills + settlements): "
            f"{self.events_with_fee_field}",
            f"  events missing a fee field: {self.events_missing_fee_field}",
            f"  accounting schema failures: {self.accounting_schema_failures}",
            f"  orders with complete fee data: {self.orders_with_complete_fees}",
            f"  orders with incomplete fee data: {self.orders_missing_fees}",
            f"  account-level fee total is exact: {self.account_fee_total_complete}",
            "",
            f"  history supplied is complete: {self.history_is_complete}",
            f"  position state claimed as authoritative: "
            f"{self.claims_complete_position_state and not self.position_state_contradicted_by_exchange}",
        ]
        if self.position_state_contradicted_by_exchange:
            lines.append(
                "  POSITION STATE IS CONTRADICTED BY THE EXCHANGE: the replay "
                "still holds"
            )
            lines.append(
                "  markets the exchange does not report, and no settlement "
                "explains them."
            )
            lines.append(
                "  A complete fill history does not by itself earn authority "
                "over positions."
            )
        if not self.claims_complete_position_state:
            lines += [
                "",
                "  NOTE: this replay ran over a bounded recent window, so the position",
                "        figures above describe only what happened inside that window.",
                "        They are NOT the account's position state, and nothing here may",
                "        be treated as a settled wager.",
            ]
        return "\n".join(lines)


def build_diagnostics(result: AccountingResult) -> AccountingDiagnostics:
    """Reduce a replay to counts, discarding every sensitive value."""
    diagnostics = AccountingDiagnostics(
        fills_replayed=result.fills_replayed,
        duplicate_fills_ignored=result.duplicate_fills_ignored,
        orders_observed=result.order_stats.orders,
        orders_with_partial_fills=result.order_stats.partial_orders,
        fills_without_order_id=result.order_stats.fills_without_order_id,
        orders_without_a_price=result.order_stats.orders_without_price,
        orders_with_complete_fees=result.order_stats.orders_with_complete_fees,
        orders_missing_fees=result.order_stats.orders_missing_fees,
        markets_with_activity=len(result.ledgers),
        subaccounts_observed=len(result.subaccounts_observed),
        account_fee_total_complete=result.total_fees is not None,
        position_transitions=len(result.transitions),
        claims_complete_position_state=result.claims_complete_position_state,
        history_is_complete=result.completeness is HistoryCompleteness.COMPLETE,
        settlements_applied=result.settlements_applied,
        settlements_without_a_position=result.settlements_without_a_position,
        settlements_refused_unreconciled=result.settlements_refused_unreconciled,
        settlements_refused_ambiguous_subaccount=(
            result.settlements_refused_ambiguous_subaccount
        ),
    )

    counters = {
        TransitionKind.OPEN: "positions_opened",
        TransitionKind.INCREASE: "positions_increased",
        TransitionKind.REDUCE: "positions_reduced",
        TransitionKind.CLOSE: "positions_closed",
        TransitionKind.REVERSE: "positions_reversed",
        TransitionKind.SETTLE: "positions_settled",
    }
    for transition in result.transitions:
        name = counters[transition.kind]
        setattr(diagnostics, name, getattr(diagnostics, name) + 1)
        if transition.fee_dollars is None:
            diagnostics.events_missing_fee_field += 1
        else:
            diagnostics.events_with_fee_field += 1
    diagnostics.fills_without_a_subaccount_number = sum(
        1 for ledger in result.ledgers.values() if ledger.subaccount_number is None
    )

    episodes = result.episodes
    diagnostics.episodes_observed = len(episodes)
    diagnostics.episodes_open_at_end = sum(1 for e in episodes if e.is_open)
    diagnostics.episodes_closed = sum(1 for e in episodes if not e.is_open)
    diagnostics.episodes_with_complete_cost_basis = sum(
        1 for e in episodes if e.cost_basis_complete
    )
    diagnostics.episodes_with_complete_fees = sum(1 for e in episodes if e.fee_complete)
    diagnostics.episodes_provable = sum(1 for e in episodes if e.provable)
    diagnostics.episodes_with_importable_identity = sum(
        1 for e in episodes if e.is_importable
    )
    diagnostics.episodes_with_ambiguous_fee_allocation = sum(
        1 for e in episodes if e.fee_allocation_ambiguous
    )
    return diagnostics
