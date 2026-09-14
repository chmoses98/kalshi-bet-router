"""Read-only Kalshi client.

Phase 0 is read-only by construction, not by convention:

* :meth:`KalshiReadOnlyClient._get` is the only request path, and it hard-codes
  ``GET``.
* Every path is checked against :data:`READ_ONLY_PATH_PREFIXES` before a request
  is signed, so a future edit that reaches for ``/portfolio/orders`` fails a
  test rather than placing a trade.

Endpoints used (all documented Kalshi ``/trade-api/v2`` routes):

=========================================  ===============================================
``GET /portfolio/fills``                   member fills, cursor-paginated
``GET /markets/{ticker}``                  market -> event_ticker, category
``GET /events/{event_ticker}``             event  -> series_ticker, title
``GET /events/{event_ticker}/metadata``    event  -> competition, competition_scope
``GET /series/{series_ticker}``            series -> category/categories/tags
``GET /search/filters_by_sport``           public sport/competition/scope taxonomy
``GET /milestones``                        public competition -> event ticker links
=========================================  ===============================================

The last three carry no account information: the taxonomy and milestone routes
are public catalogue data, queried by competition rather than by anything the
account traded.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from .auth import KalshiSigner
from .config import API_PATH_PREFIX, AuditConfig
from .errors import SchemaError
from .milestones import MILESTONES_PATH
from .taxonomy import FILTERS_BY_SPORT_PATH
from .http import (
    Transport,
    build_url,
    decode_json_object,
    request_with_retries,
    urllib_transport,
)

#: Only these path prefixes may be requested.  Order entry, cancellation and
#: any other mutating route is absent on purpose.
READ_ONLY_PATH_PREFIXES = (
    "/portfolio/fills",
    "/portfolio/positions",
    "/portfolio/settlements",
    "/historical/fills",
    "/historical/cutoff",
    "/markets/",
    "/events/",
    "/series/",
    "/search/filters_by_sport",
    "/milestones",
)

@dataclass
class WalkStats:
    """How a paginated walk ended.

    The distinction this exists for: a walk that stopped because the cursor ran
    out saw everything, and a walk that stopped because a budget ran out did
    not.  Both produce a list of rows and look identical afterwards, so without
    recording which happened, "this is the complete history" is a claim nothing
    can contradict.
    """

    pages: int = 0
    rows: int = 0
    #: The server said there was nothing more.
    exhausted: bool = False
    #: A caller-imposed limit stopped the walk early.
    truncated: bool = False


FILLS_PATH = "/portfolio/fills"
POSITIONS_PATH = "/portfolio/positions"
SETTLEMENTS_PATH = "/portfolio/settlements"
HISTORICAL_FILLS_PATH = "/historical/fills"
HISTORICAL_CUTOFF_PATH = "/historical/cutoff"


def _assert_read_only(path: str) -> None:
    if not any(path.startswith(prefix) for prefix in READ_ONLY_PATH_PREFIXES):
        raise SchemaError(
            f"refusing to request non-allowlisted path {path!r}; Phase 0 is read-only"
        )


class KalshiReadOnlyClient:
    """Authenticated, read-only access to a member's fills and public metadata."""

    def __init__(
        self,
        signer: KalshiSigner,
        config: AuditConfig,
        transport: Transport = urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._signer = signer
        self._config = config
        self._transport = transport
        self._sleep = sleep
        #: Count of HTTP requests issued, for privacy-safe diagnostics.
        self.request_count = 0

    def _get(self, path: str, operation: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        _assert_read_only(path)
        signed_path = API_PATH_PREFIX + path
        url = build_url(self._config.base_url, path, params)
        self.request_count += 1
        body = request_with_retries(
            transport=self._transport,
            method="GET",
            url=url,
            # Re-signed per attempt: Kalshi rejects stale timestamps.
            headers_factory=lambda: {**self._signer.headers("GET", signed_path), "Accept": "application/json"},
            timeout=self._config.timeout_seconds,
            max_retries=self._config.max_retries,
            operation=operation,
            sleep=self._sleep,
        )
        return decode_json_object(body, operation)

    # ------------------------------------------------------------------ fills

    def iter_fills(
        self,
        max_fills: int | None = None,
        page_limit: int | None = None,
        stats: WalkStats | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield raw fill objects, newest first, up to ``max_fills``.

        Pagination follows Kalshi's opaque-cursor scheme: each response carries a
        ``cursor`` whose value is passed to the next request.  An empty or absent
        cursor terminates the walk.  A repeated cursor is treated as a server or
        proxy fault and fails closed rather than looping forever.

        An account with no fills yields nothing and is a valid, successful state;
        it is distinguished from an API failure, which raises.
        """
        yield from self._iter_fill_pages(
            FILLS_PATH, "get_fills", max_fills, page_limit, stats
        )

    def _iter_fill_pages(
        self,
        path: str,
        operation: str,
        max_fills: int | None,
        page_limit: int | None,
        stats: WalkStats | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Bounded cursor walk over a fills route.

        Shared by the live and archived routes so both inherit the same
        fail-closed pagination rules rather than growing a second copy that can
        drift.
        """
        remaining = self._config.max_fills if max_fills is None else max_fills
        per_page = self._config.page_limit if page_limit is None else page_limit
        cursor: str | None = None
        seen_cursors: set[str] = set()

        while remaining > 0:
            params: dict[str, Any] = {"limit": min(per_page, remaining)}
            if cursor:
                params["cursor"] = cursor
            payload = self._get(path, operation, params)

            fills = _require_list(payload, "fills", "fills")
            if stats is not None:
                stats.pages += 1

            for fill in fills:
                if not isinstance(fill, dict):
                    raise SchemaError(
                        f"fills response contained a {type(fill).__name__} entry, expected object"
                    )
                yield fill
                if stats is not None:
                    stats.rows += 1
                remaining -= 1
                if remaining <= 0:
                    # Stopped by the caller's budget, not by the server. The walk
                    # saw part of the history and must never be mistaken for all
                    # of it.
                    if stats is not None:
                        stats.truncated = True
                    return

            cursor = _next_cursor(payload, "fills", seen_cursors)
            if cursor is None:
                if stats is not None:
                    stats.exhausted = True
                return

            # A page shorter than requested with a cursor still set is legal;
            # an empty page with a cursor is a server fault.
            if not fills:
                raise SchemaError("fills pagination returned an empty page with a live cursor")

    def _iter_cursor_pages(
        self, path: str, operation: str, key: str, page_limit: int | None
    ) -> Iterator[dict[str, Any]]:
        """Unbounded cursor walk over a portfolio collection.

        Positions and settlements are **not** truncated by ``max_fills``: a
        partial position list would silently turn "this ticker is missing from
        the exchange's own view" into a reconciliation failure that is really
        just a short page.
        """
        per_page = self._config.page_limit if page_limit is None else page_limit
        cursor: str | None = None
        seen_cursors: set[str] = set()

        while True:
            params: dict[str, Any] = {"limit": per_page}
            if cursor:
                params["cursor"] = cursor
            payload = self._get(path, operation, params)

            rows = _require_list(payload, key, key)
            for row in rows:
                if not isinstance(row, dict):
                    raise SchemaError(
                        f"{key} response contained a {type(row).__name__} entry, expected object"
                    )
                yield row

            cursor = _next_cursor(payload, key, seen_cursors)
            if cursor is None:
                return
            if not rows:
                raise SchemaError(f"{key} pagination returned an empty page with a live cursor")

    # ---------------------------------------------------------------- history

    def iter_historical_fills(
        self,
        max_fills: int | None = None,
        page_limit: int | None = None,
        stats: WalkStats | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield raw fills from the archive, using the same cursor walk.

        Fills older than ``GET /historical/cutoff`` are served **only** from this
        route, so a replay that reads ``/portfolio/fills`` alone is bounded by
        that cutoff whether or not it realises it.
        """
        yield from self._iter_fill_pages(
            HISTORICAL_FILLS_PATH, "get_historical_fills", max_fills, page_limit, stats
        )

    def get_historical_cutoff(self) -> dict[str, Any]:
        """The timestamp before which fills live only in the archive."""
        return self._get(HISTORICAL_CUTOFF_PATH, "get_historical_cutoff")

    def iter_positions(self, page_limit: int | None = None) -> Iterator[dict[str, Any]]:
        """Yield the exchange's own position rows, one per ticker.

        This is the reconciliation target, not an accounting input: it says what
        the account holds, never how it came to hold it.
        """
        yield from self._iter_cursor_pages(
            POSITIONS_PATH, "get_positions", "market_positions", page_limit
        )

    def iter_settlements(self, page_limit: int | None = None) -> Iterator[dict[str, Any]]:
        """Yield settlement rows.

        A settlement closes a position **without a fill**, so a fills-only replay
        cannot see it and will report a market as still open long after the
        exchange has paid it out.
        """
        yield from self._iter_cursor_pages(
            SETTLEMENTS_PATH, "get_settlements", "settlements", page_limit
        )

    # --------------------------------------------------------------- metadata

    def get_market(self, ticker: str) -> dict[str, Any]:
        payload = self._get(f"/markets/{ticker}", "get_market")
        return _require_object(payload, "market", "get_market")

    def get_event(self, event_ticker: str) -> dict[str, Any]:
        payload = self._get(f"/events/{event_ticker}", "get_event")
        return _require_object(payload, "event", "get_event")

    def get_series(self, series_ticker: str) -> dict[str, Any]:
        payload = self._get(f"/series/{series_ticker}", "get_series")
        return _require_object(payload, "series", "get_series")

    def get_event_metadata(self, event_ticker: str) -> dict[str, Any]:
        """Fetch ``competition`` / ``competition_scope`` for one event.

        The documented response carries the metadata fields at the top level.
        A wrapped form is also accepted, since the surrounding envelope is the
        one part of this route we could not confirm against a live response
        before writing it; either way a non-object fails closed.
        """
        payload = self._get(f"/events/{event_ticker}/metadata", "get_event_metadata")
        for key in ("metadata", "event_metadata"):
            wrapped = payload.get(key)
            if isinstance(wrapped, dict):
                return wrapped
        return payload

    def get_filters_by_sport(self) -> dict[str, Any]:
        """Fetch the public sport/competition/scope taxonomy (once per audit)."""
        return self._get(FILTERS_BY_SPORT_PATH, "get_filters_by_sport")

    def get_milestones(self, params: dict[str, Any]) -> dict[str, Any]:
        """Fetch one page of public milestones, filtered by category/competition."""
        return self._get(MILESTONES_PATH, "get_milestones", params)


def _require_list(payload: dict[str, Any], key: str, label: str) -> list[Any]:
    """A missing collection is a fault, never an empty account."""
    value = payload.get(key)
    if value is None:
        raise SchemaError(f"{label} response is missing the {key!r} field")
    if not isinstance(value, list):
        raise SchemaError(
            f"{label} response field {key!r} was {type(value).__name__}, expected list"
        )
    return value


def _next_cursor(payload: dict[str, Any], label: str, seen: set[str]) -> str | None:
    """Return the next cursor, or ``None`` when the walk is done."""
    cursor = payload.get("cursor") or ""
    if not isinstance(cursor, str):
        raise SchemaError(
            f"{label} response field 'cursor' was {type(cursor).__name__}, expected string"
        )
    if not cursor:
        return None
    if cursor in seen:
        raise SchemaError(f"{label} pagination repeated a cursor; refusing to loop")
    seen.add(cursor)
    return cursor


def _require_object(payload: dict[str, Any], key: str, operation: str) -> dict[str, Any]:
    value = payload.get(key)
    if value is None:
        raise SchemaError(f"response for {operation!r} is missing the {key!r} field")
    if not isinstance(value, dict):
        raise SchemaError(
            f"response field {key!r} for {operation!r} was {type(value).__name__}, expected object"
        )
    return value
