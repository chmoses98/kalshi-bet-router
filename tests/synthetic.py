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


def canonical_outcome(action: str, side: str) -> str:
    """Documented equivalence: buy-yes and sell-no both position you for YES."""
    positioned_for_yes = (action == "buy") == (side == "yes")
    return "yes" if positioned_for_yes else "no"


def canonical_book_side(action: str, side: str) -> str:
    """``bid`` pairs with ``outcome_side=yes``; ``ask`` with ``no``."""
    return "bid" if canonical_outcome(action, side) == "yes" else "ask"


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
        # Current canonical direction fields, plus the deprecated pair, exactly
        # as the published Get Fills example carries both.
        "outcome_side": canonical_outcome(action, side),
        "book_side": canonical_book_side(action, side),
        "action": action,
        "side": side,
        "subaccount_number": 0,
        "is_taker": True,
        # Current (post Q1-2026 fixed-point migration) field shapes.
        "yes_price_dollars": "0.5700",
        "no_price_dollars": "0.4300",
        "created_time": "2026-09-01T12:00:00Z",
    }
    if count is not None:
        fill["count_fp"] = f"{count}.00"
    fill.update(extra)
    return fill


def make_event_metadata(
    competition: str | None = None, competition_scope: str | None = None, **extra: Any
) -> dict[str, Any]:
    """A synthetic ``GET /events/{ticker}/metadata`` body."""
    metadata: dict[str, Any] = {
        "image_url": "https://example.invalid/synthetic.png",
        "settlement_sources": [],
        "competition": competition,
        "competition_scope": competition_scope,
    }
    metadata.update(extra)
    return metadata


def make_taxonomy(sports: dict[str, list[str]], scopes: list[str] | None = None) -> dict[str, Any]:
    """A synthetic ``GET /search/filters_by_sport`` body."""
    return {
        "filters_by_sports": {
            sport: {
                "competitions": list(competitions),
                "scopes": list(scopes or ["Games", "Futures"]),
            }
            for sport, competitions in sports.items()
        },
        "sport_ordering": list(sports),
    }


def make_milestone(milestone_id: str, event_tickers: list[str], **extra: Any) -> dict[str, Any]:
    """A synthetic milestone linking a fixture to Kalshi event tickers."""
    milestone = {
        "id": milestone_id,
        "category": "Sports",
        "type": "football_game",
        "title": "Synthetic fixture",
        "primary_event_tickers": list(event_tickers),
        "related_event_tickers": [],
    }
    milestone.update(extra)
    return milestone


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
    taxonomy: dict[str, Any] | None = None,
    milestones: list[dict[str, Any]] | None = None,
) -> Handler:
    """Serve the full read-only surface used by an audit.

    ``metadata`` maps a ticker to its market / event / series object, and an
    ``"<event>/metadata"`` key to that event's metadata body.  Cursors are
    synthetic opaque strings; the final page returns ``""``.
    """
    metadata = metadata if metadata is not None else {}
    cursors = {f"cursor-{i}": i for i in range(len(pages))}

    def handler(method: str, path: str, query: dict[str, list[str]]) -> tuple[int, Any]:
        if path.endswith("/portfolio/fills"):
            cursor = query.get("cursor", [None])[0]
            index = cursors.get(cursor, 0) if cursor else 0
            body: dict[str, Any] = {"fills": pages[index]}
            body["cursor"] = f"cursor-{index + 1}" if index + 1 < len(pages) else ""
            return 200, body

        if path.endswith("/search/filters_by_sport"):
            if taxonomy is None:
                return 404, {"error": "not found"}
            return 200, taxonomy

        if path.endswith("/milestones"):
            if milestones is None:
                return 404, {"error": "not found"}
            competition = query.get("competition", [None])[0]
            matching = [
                m for m in milestones
                if competition is None or m.get("competition") in (None, competition)
            ]
            return 200, {"milestones": matching, "cursor": ""}

        # Event metadata must be matched before the plain event route.
        if "/events/" in path and path.endswith("/metadata"):
            event_ticker = path.rsplit("/", 2)[-2]
            obj = metadata.get(f"{event_ticker}/metadata")
            if obj is None:
                return 404, {"error": "not found"}
            return 200, obj

        for prefix, key in (("/markets/", "market"), ("/events/", "event"), ("/series/", "series")):
            if prefix in path:
                ticker = path.rsplit("/", 1)[-1]
                obj = metadata.get(ticker)
                if obj is None:
                    return 404, {"error": "not found"}
                return 200, {key: obj}
        return 404, {"error": "unknown path"}

    return handler


SYNTH_TICKER = "KXSYNTH-ACCT01-AAA"


def make_accounting_fill(
    index: int,
    quantity: str,
    action: str = "buy",
    side: str = "yes",
    yes_price: str | None = "0.5700",
    no_price: str | None = None,
    order_id: str | None = None,
    ticker: str = SYNTH_TICKER,
    minute: int | None = None,
    created_time: str | None = None,
    fee: str | None = None,
    fill_id: str | None = None,
    subaccount: int | None = 0,
    **extra: Any,
) -> dict[str, Any]:
    """A synthetic fill shaped for accounting tests.

    Every identifier is invented. No value here comes from a real account.
    """
    stamp = created_time
    if stamp is None:
        stamp = f"2026-09-01T12:{(minute if minute is not None else index):02d}:00Z"
    raw: dict[str, Any] = {
        "fill_id": fill_id or f"SYNTHFILL-{index:04d}",
        "order_id": order_id if order_id is not None else f"SYNTHORDER-{index:04d}",
        "ticker": ticker,
        "outcome_side": canonical_outcome(action, side),
        "book_side": canonical_book_side(action, side),
        "action": action,
        "side": side,
        "subaccount_number": subaccount,
        "count_fp": quantity,
        "created_time": stamp,
        "is_taker": True,
    }
    if yes_price is not None and side == "yes":
        raw["yes_price_dollars"] = yes_price
    if side == "no":
        raw["no_price_dollars"] = no_price if no_price is not None else "0.4300"
    if fee is not None:
        # Current published field name and representation: a dollar string.
        raw["fee_cost"] = fee
    raw.update(extra)
    return raw
