"""Synthetic fixtures and a fake transport.

Every identifier here is invented. No value in this module comes from a real
Kalshi account, and the RSA key is generated at test time and never persisted.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any, Callable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_router.http import HttpResponse
from kalshi_router.errors import TransportError

FAKE_KEY_ID = "synthetic-key-id-0000"

#: Tokens that must never appear in aggregate output. Used by the privacy tests.
SENSITIVE_TOKENS = (
    "SYNTHFILL",
    "SYNTHORDER",
    "KXMLBGAME-SYNTH",
    "KXNFLGAME-SYNTH",
    FAKE_KEY_ID,
)


def generate_fake_private_key_pem() -> str:
    """Generate a throwaway RSA key. Not a credential; never written to disk."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def make_fill(
    index: int,
    ticker: str = "KXMLBGAME-SYNTH01-NYY",
    action: str = "buy",
    side: str = "yes",
    order_id: str | None = None,
    count: int | None = 10,
    fill_id: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """One synthetic fill in the documented Kalshi shape."""
    fill: dict[str, Any] = {
        "fill_id": fill_id or f"SYNTHFILL-{index:04d}",
        "order_id": order_id if order_id is not None else f"SYNTHORDER-{index:04d}",
        "trade_id": f"SYNTHTRADE-{index:04d}",
        "ticker": ticker,
        "action": action,
        "side": side,
        "is_taker": True,
        "yes_price": 57,
        "no_price": 43,
        "created_time": "2026-09-01T12:00:00Z",
    }
    if count is not None:
        fill["count"] = count
    fill.update(extra)
    return fill


def make_market(ticker: str, event_ticker: str, **extra: Any) -> dict[str, Any]:
    market = {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "title": "Synthetic market title",
        "status": "finalized",
    }
    market.update(extra)
    return market


def make_event(event_ticker: str, series_ticker: str, **extra: Any) -> dict[str, Any]:
    event = {
        "event_ticker": event_ticker,
        "series_ticker": series_ticker,
        "title": "Synthetic Team A vs Synthetic Team B",
    }
    event.update(extra)
    return event


def make_series(series_ticker: str, **extra: Any) -> dict[str, Any]:
    series = {"ticker": series_ticker, "title": "Synthetic series"}
    series.update(extra)
    return series


Handler = Callable[[str, str, dict[str, list[str]]], tuple[int, Any]]


class FakeTransport:
    """A transport that answers from a handler instead of the network.

    Records the paths requested (never the headers) so tests can assert on the
    call pattern without touching credential material.
    """

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self.paths: list[str] = []
        self.header_names: list[list[str]] = []

    def __call__(
        self, method: str, url: str, headers: dict[str, str], timeout: float
    ) -> HttpResponse:
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query)
        self.paths.append(parsed.path if not parsed.query else f"{parsed.path}?{parsed.query}")
        self.header_names.append(sorted(headers))
        status, payload = self._handler(method, parsed.path, query)
        if isinstance(payload, (bytes, bytearray)):
            return HttpResponse(status=status, body=bytes(payload))
        if payload is None:
            return HttpResponse(status=status, body=b"")
        return HttpResponse(status=status, body=json.dumps(payload).encode("utf-8"))


class FailingTransport:
    """Raises a network error for the first ``failures`` calls, then delegates."""

    def __init__(self, inner: FakeTransport, failures: int) -> None:
        self._inner = inner
        self._remaining = failures
        self.calls = 0

    def __call__(self, method, url, headers, timeout) -> HttpResponse:
        self.calls += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise TransportError("network failure contacting Kalshi API: 'synthetic'")
        return self._inner(method, url, headers, timeout)


def paged_fills_handler(
    pages: list[list[dict[str, Any]]],
    metadata: dict[str, Any] | None = None,
) -> Handler:
    """Serve ``/portfolio/fills`` as cursor-paginated pages plus metadata routes.

    Cursors are synthetic opaque strings; the final page returns ``""``.
    """
    metadata = metadata or {}
    cursors = {f"cursor-{i}": i for i in range(len(pages))}

    def handler(method: str, path: str, query: dict[str, list[str]]) -> tuple[int, Any]:
        if path.endswith("/portfolio/fills"):
            cursor = query.get("cursor", [None])[0]
            index = cursors.get(cursor, 0) if cursor else 0
            body: dict[str, Any] = {"fills": pages[index]}
            body["cursor"] = f"cursor-{index + 1}" if index + 1 < len(pages) else ""
            return 200, body
        for prefix, key in (("/markets/", "market"), ("/events/", "event"), ("/series/", "series")):
            if prefix in path:
                ticker = path.rsplit("/", 1)[-1]
                obj = metadata.get(ticker)
                if obj is None:
                    return 404, {"error": "not found"}
                return 200, {key: obj}
        return 404, {"error": "unknown path"}

    return handler
