"""Exception hierarchy for the Phase 0 read-only auditor.

Every exception in this module is constructed so that its string form is safe
to print in a public GitHub Actions log.  Implementations must never place
credential material, signatures, fill identifiers, tickers, prices or counts
into an exception message.
"""

from __future__ import annotations


class KalshiRouterError(Exception):
    """Base class for all errors raised by this package."""


class ConfigurationError(KalshiRouterError):
    """Required configuration or credentials are missing or unusable.

    Raised before any network call is attempted, so that the process fails
    closed rather than emitting unauthenticated requests.
    """


class CredentialError(ConfigurationError):
    """A credential is present but cannot be loaded as an RSA private key."""


class TransportError(KalshiRouterError):
    """A network-level failure (DNS, TLS, connection reset, timeout)."""


class HttpStatusError(KalshiRouterError):
    """The API returned a non-success HTTP status.

    Only the status code and the request's logical operation name are retained.
    Response bodies are deliberately discarded: an error body from a portfolio
    endpoint can echo account state.
    """

    def __init__(self, status: int, operation: str) -> None:
        self.status = status
        self.operation = operation
        super().__init__(f"Kalshi API returned HTTP {status} for operation {operation!r}")


class RateLimitError(HttpStatusError):
    """HTTP 429; retries were exhausted."""


class AuthenticationError(HttpStatusError):
    """HTTP 401/403; the credentials were rejected by Kalshi."""


class SchemaError(KalshiRouterError):
    """A response did not match the documented schema.

    The offending payload is never included in the message; only a description
    of which field was wrong.  This is a fail-closed condition: the auditor
    refuses to guess at the meaning of a malformed response.
    """


class SensitiveOutputRefused(KalshiRouterError):
    """Sensitive local diagnostics were requested in a non-private context."""
