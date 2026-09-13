"""Minimal HTTPS transport with fail-closed error mapping and bounded retries.

The transport is a plain callable so tests can substitute a synthetic one and
never touch the network.  Response bodies are only ever handed back to the
client for JSON decoding; on failure they are dropped rather than logged,
because an error body from a portfolio endpoint can echo account state.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, NamedTuple

from .errors import (
    AuthenticationError,
    HttpStatusError,
    RateLimitError,
    SchemaError,
    TransportError,
)

USER_AGENT = "kalshi-bet-router-phase0/0.1 (read-only audit)"

#: Statuses worth retrying.  Kalshi's 429 responses carry no ``Retry-After``
#: header, so the backoff schedule below is the only pacing signal available.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

Transport = Callable[[str, str, dict[str, str], float], "HttpResponse"]


class HttpResponse(NamedTuple):
    status: int
    body: bytes


def urllib_transport(
    method: str, url: str, headers: dict[str, str], timeout: float
) -> HttpResponse:
    """Perform one HTTPS request using the standard library."""
    request = urllib.request.Request(url=url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(status=response.status, body=response.read())
    except urllib.error.HTTPError as exc:
        # Read and discard: the body is not retained anywhere.
        try:
            exc.read()
        except Exception:  # pragma: no cover - defensive
            pass
        return HttpResponse(status=exc.code, body=b"")
    except urllib.error.URLError as exc:
        raise TransportError(f"network failure contacting Kalshi API: {exc.reason!r}") from None
    except (TimeoutError, OSError) as exc:
        raise TransportError(f"network failure contacting Kalshi API: {type(exc).__name__}") from None


def build_url(base_url: str, path: str, params: dict[str, Any] | None = None) -> str:
    """Join a base URL and API path, appending only non-``None`` query params."""
    url = base_url.rstrip("/") + path
    if params:
        filtered = {k: v for k, v in params.items() if v is not None}
        if filtered:
            url = f"{url}?{urllib.parse.urlencode(filtered)}"
    return url


def backoff_delays(max_retries: int, base: float = 0.5, cap: float = 8.0) -> list[float]:
    """Exponential backoff with full jitter, used for 429 and 5xx."""
    return [min(cap, base * (2**attempt)) * (0.5 + random.random() / 2) for attempt in range(max_retries)]


def decode_json_object(body: bytes, operation: str) -> dict[str, Any]:
    """Decode a JSON object body, failing closed on anything else.

    A truncated or non-JSON body is treated as a hard error rather than as an
    empty result, so a proxy error page can never be mistaken for "no fills".
    """
    if not body:
        raise SchemaError(f"empty response body for operation {operation!r}")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SchemaError(f"response for operation {operation!r} was not valid JSON") from None
    if not isinstance(payload, dict):
        raise SchemaError(
            f"response for operation {operation!r} was a {type(payload).__name__}, expected object"
        )
    return payload


def raise_for_status(status: int, operation: str) -> None:
    """Map a non-2xx status onto the exception hierarchy."""
    if 200 <= status < 300:
        return
    if status in (401, 403):
        raise AuthenticationError(status, operation)
    if status == 429:
        raise RateLimitError(status, operation)
    raise HttpStatusError(status, operation)


def request_with_retries(
    transport: Transport,
    method: str,
    url: str,
    headers_factory: Callable[[], dict[str, str]],
    timeout: float,
    max_retries: int,
    operation: str,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """Issue a request, retrying transient failures a bounded number of times.

    ``headers_factory`` is re-invoked for every attempt so that each retry is
    signed with a fresh timestamp; Kalshi rejects stale signatures.
    """
    delays = backoff_delays(max_retries)
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            response = transport(method, url, headers_factory(), timeout)
        except TransportError as exc:
            last_error = exc
        else:
            if 200 <= response.status < 300:
                return response.body
            if response.status not in RETRYABLE_STATUSES:
                raise_for_status(response.status, operation)
            try:
                raise_for_status(response.status, operation)
            except HttpStatusError as exc:
                last_error = exc

        if attempt < max_retries:
            sleep(delays[attempt])

    assert last_error is not None  # loop always records an error before exiting
    raise last_error
