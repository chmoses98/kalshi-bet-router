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
from dataclasses import dataclass
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


@dataclass
class TransportTelemetry:
    """How much a run spent on retries. Counts and seconds only.

    A run that spends four minutes in backoff and a run that sails through look
    IDENTICAL in the log without this, and the difference is exactly what says
    whether a 15-minute cadence is sustainable against Kalshi's limits.

    Nothing here can hold a URL, a ticker or an account identifier: every field
    is a number, and a test walks them.
    """

    requests: int = 0
    #: Attempts beyond the first, by reason.
    retries_rate_limited: int = 0
    retries_server_error: int = 0
    retries_transport_error: int = 0
    #: Whole seconds asked for in backoff. Rounded, because the jitter makes
    #: sub-second precision meaningless and a float invites false comparison.
    backoff_seconds: int = 0
    #: Requests that exhausted every retry and raised.
    exhausted: int = 0

    @property
    def retries(self) -> int:
        return (
            self.retries_rate_limited
            + self.retries_server_error
            + self.retries_transport_error
        )

    def as_dict(self) -> dict[str, int]:
        return dict(vars(self))

    def render(self) -> str:
        return "\n".join([
            "transport (counts and seconds only):",
            f"  requests: {self.requests}",
            f"  retries: {self.retries}",
            f"    rate limited (429): {self.retries_rate_limited}",
            f"    server error (5xx): {self.retries_server_error}",
            f"    transport error: {self.retries_transport_error}",
            f"  seconds spent backing off: {self.backoff_seconds}",
            f"  requests that exhausted every retry: {self.exhausted}",
        ])


def request_with_retries(
    transport: Transport,
    method: str,
    url: str,
    headers_factory: Callable[[], dict[str, str]],
    timeout: float,
    max_retries: int,
    operation: str,
    sleep: Callable[[float], None] = time.sleep,
    telemetry: "TransportTelemetry | None" = None,
) -> bytes:
    """Issue a request, retrying transient failures a bounded number of times.

    ``headers_factory`` is re-invoked for every attempt so that each retry is
    signed with a fresh timestamp; Kalshi rejects stale signatures.

    ``telemetry``, when given, records how much retrying this cost. It is
    optional so that no caller is obliged to care, and so that adding it could
    not change the function's behaviour for one that does not.
    """
    delays = backoff_delays(max_retries)
    last_error: Exception | None = None
    if telemetry is not None:
        telemetry.requests += 1

    for attempt in range(max_retries + 1):
        try:
            response = transport(method, url, headers_factory(), timeout)
        except TransportError as exc:
            last_error = exc
            if telemetry is not None:
                telemetry.retries_transport_error += 1
        else:
            if 200 <= response.status < 300:
                return response.body
            if response.status not in RETRYABLE_STATUSES:
                raise_for_status(response.status, operation)
            if telemetry is not None:
                if response.status == 429:
                    telemetry.retries_rate_limited += 1
                else:
                    telemetry.retries_server_error += 1
            try:
                raise_for_status(response.status, operation)
            except HttpStatusError as exc:
                last_error = exc

        if attempt < max_retries:
            if telemetry is not None:
                telemetry.backoff_seconds += round(delays[attempt])
            sleep(delays[attempt])

    if telemetry is not None:
        telemetry.exhausted += 1
    assert last_error is not None  # loop always records an error before exiting
    raise last_error
