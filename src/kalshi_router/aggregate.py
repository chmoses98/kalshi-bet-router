"""Privacy-safe aggregate reporting.

Everything this module can emit is a **count**.  There is no code path that
renders a ticker, a fill id, an order id, a price, a contract count, a monetary
value, or a market title.  That is enforced by construction --
:class:`AuditReport` has no field capable of holding such a value -- and by
``tests/test_privacy.py``, which scans rendered output for fixture secrets.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .sports import REPORT_ORDER, Sport


@dataclass
class AuditReport:
    """Counts only.  Safe to print in a public Actions log."""

    fills_fetched: int = 0
    unique_fills: int = 0
    duplicate_fill_ids_observed: int = 0

    classification_counts: dict[Sport, int] = field(
        default_factory=lambda: {sport: 0 for sport in REPORT_ORDER}
    )
    classification_failures: int = 0

    fills_requiring_metadata_lookup: int = 0
    unique_markets_observed: int = 0
    metadata_lookup_failures: int = 0
    metadata_partial_failures: int = 0
    metadata_cache_hits: int = 0

    buy_fills: int = 0
    sell_fills: int = 0
    yes_side_fills: int = 0
    no_side_fills: int = 0

    orders_observed: int = 0
    partial_order_groups: int = 0
    fills_without_order_id: int = 0
    fills_with_unverified_count: int = 0

    classifications_using_unverified_series_ticker: int = 0
    api_requests: int = 0

    @property
    def account_has_no_fills(self) -> bool:
        """An empty account is a valid success state, not a failure."""
        return self.fills_fetched == 0

    def as_dict(self) -> dict[str, int | bool]:
        data: dict[str, int | bool] = {
            "fills_fetched": self.fills_fetched,
            "unique_fills": self.unique_fills,
            "duplicate_fill_ids_observed": self.duplicate_fill_ids_observed,
            "classification_failures": self.classification_failures,
            "fills_requiring_metadata_lookup": self.fills_requiring_metadata_lookup,
            "unique_markets_observed": self.unique_markets_observed,
            "metadata_lookup_failures": self.metadata_lookup_failures,
            "metadata_partial_failures": self.metadata_partial_failures,
            "metadata_cache_hits": self.metadata_cache_hits,
            "buy_fills": self.buy_fills,
            "sell_fills": self.sell_fills,
            "yes_side_fills": self.yes_side_fills,
            "no_side_fills": self.no_side_fills,
            "orders_observed": self.orders_observed,
            "partial_order_groups": self.partial_order_groups,
            "fills_without_order_id": self.fills_without_order_id,
            "fills_with_unverified_count": self.fills_with_unverified_count,
            "classifications_using_unverified_series_ticker": (
                self.classifications_using_unverified_series_ticker
            ),
            "api_requests": self.api_requests,
            "account_has_no_fills": self.account_has_no_fills,
        }
        for sport in REPORT_ORDER:
            data[f"classification_{sport.value}"] = self.classification_counts.get(sport, 0)
        return data

    def render(self) -> str:
        """Render the aggregate block printed by the public audit."""
        lines = [
            "Kalshi Phase 0 read-only audit -- aggregate diagnostics",
            "=======================================================",
            f"fills fetched: {self.fills_fetched}",
            f"unique fills: {self.unique_fills}",
            f"duplicate fill IDs observed: {self.duplicate_fill_ids_observed}",
            "",
            "classification:",
        ]
        for sport in REPORT_ORDER:
            lines.append(f"  {sport.value}: {self.classification_counts.get(sport, 0)}")
        lines += [
            "",
            f"fills requiring additional metadata lookup: {self.fills_requiring_metadata_lookup}",
            f"unique markets observed: {self.unique_markets_observed}",
            f"metadata lookup failures: {self.metadata_lookup_failures}",
            f"metadata partial lookup failures: {self.metadata_partial_failures}",
            f"metadata cache hits: {self.metadata_cache_hits}",
            f"classification failures: {self.classification_failures}",
            "",
            f"buy fills: {self.buy_fills}",
            f"sell fills: {self.sell_fills}",
            f"YES-side fills: {self.yes_side_fills}",
            f"NO-side fills: {self.no_side_fills}",
            "",
            f"orders observed: {self.orders_observed}",
            f"partial-order groups (orders with >1 fill): {self.partial_order_groups}",
            f"fills without an order id: {self.fills_without_order_id}",
            f"fills whose contract count needs schema verification: {self.fills_with_unverified_count}",
            "",
            f"classifications relying on an unverified series ticker: "
            f"{self.classifications_using_unverified_series_ticker}",
            f"API requests issued: {self.api_requests}",
        ]
        if self.account_has_no_fills:
            lines += [
                "",
                "NOTE: the API responded successfully and the account returned no fills",
                "      in the sampled window. This is a valid empty state, not a failure.",
            ]
        return "\n".join(lines)
