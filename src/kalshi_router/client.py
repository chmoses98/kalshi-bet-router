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
    "/markets/",
    "/events/",
    "/series/",
    "/search/filters_by_sport",
    "/milestones",
)

FILLS_PATH = "/portfolio/fills"


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

    def iter_fills(self, max_fills: int | None = None, page_limit: int | None = None) -> Iterator[dict[str, Any]]:
        """Yield raw fill objects, newest first, up to ``max_fills``.

        Pagination follows Kalshi's opaque-cursor scheme: each response carries a
        ``cursor`` whose value is passed to the next request.  An empty or absent
        cursor terminates the walk.  A repeated cursor is treated as a server or
        proxy fault and fails closed rather than looping forever.

        An account with no fills yields nothing and is a valid, successful state;
        it is distinguished from an API failure, which raises.
        """
        remaining = self._config.max_fills if max_fills is None else max_fills
        per_page = self._config.page_limit if page_limit is None else page_limit
        cursor: str | None = None
        seen_cursors: set[str] = set()

        while remaining > 0:
            params: dict[str, Any] = {"limit": min(per_page, remaining)}
            if cursor:
                params["cursor"] = cursor
            payload = self._get(FILLS_PATH, "get_fills", params)

            fills = payload.get("fills")
            if fills is None:
                raise SchemaError("fills response is missing the 'fills' field")
            if not isinstance(fills, list):
                raise SchemaError(
                    f"fills response field 'fills' was {type(fills).__name__}, expected list"
                )

            for fill in fills:
                if not isinstance(fill, dict):
                    raise SchemaError(
                        f"fills response contained a {type(fill).__name__} entry, expected object"
                    )
                yield fill
                remaining -= 1
                if remaining <= 0:
                    return

            next_cursor = payload.get("cursor") or ""
            if not isinstance(next_cursor, str):
                raise SchemaError(
                    f"fills response field 'cursor' was {type(next_cursor).__name__}, expected string"
                )
            if not next_cursor:
                return
            if next_cursor in seen_cursors:
                raise SchemaError("fills pagination repeated a cursor; refusing to loop")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

            # A page shorter than requested with a cursor still set is legal;
            # an empty page with a cursor is a server fault.
            if not fills:
                raise SchemaError("fills pagination returned an empty page with a live cursor")

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


def _require_object(payload: dict[str, Any], key: str, operation: str) -> dict[str, Any]:
    value = payload.get(key)
    if value is None:
        raise SchemaError(f"response for {operation!r} is missing the {key!r} field")
    if not isinstance(value, dict):
        raise SchemaError(
            f"response field {key!r} for {operation!r} was {type(value).__name__}, expected object"
        )
    return value
