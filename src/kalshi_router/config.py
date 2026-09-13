"""Environment-driven configuration.

Nothing in this module ever returns secret material through ``__repr__`` or
through logging helpers.  The private key is held by :mod:`kalshi_router.auth`
and is never stored on the config object.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .errors import ConfigurationError

# Kalshi's current documented production REST root.  The historical
# ``api.elections.kalshi.com`` and ``trading-api.kalshi.com`` hosts served the
# same ``/trade-api/v2`` path space; the base URL is overridable so that a host
# migration does not require a code change.
DEFAULT_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"

# Path prefix that must be included in the string that is signed.  Kalshi signs
# the full path from the API root, so this prefix is part of the signed message.
API_PATH_PREFIX = "/trade-api/v2"

ENV_KEY_ID = "KALSHI_API_KEY_ID"
ENV_PRIVATE_KEY = "KALSHI_PRIVATE_KEY"
ENV_BASE_URL = "KALSHI_API_BASE_URL"

# Phase 0 deliberately samples a bounded recent window rather than an account
# lifetime.  The ceiling keeps a single audit within one rate-limit budget and
# keeps peak memory bounded, since fills are never written to disk.
DEFAULT_MAX_FILLS = 200
MAX_FILLS_CEILING = 500
DEFAULT_PAGE_LIMIT = 100
PAGE_LIMIT_CEILING = 1000


@dataclass(frozen=True)
class AuditConfig:
    """Non-secret run parameters for one audit."""

    base_url: str = DEFAULT_BASE_URL
    max_fills: int = DEFAULT_MAX_FILLS
    page_limit: int = DEFAULT_PAGE_LIMIT
    timeout_seconds: float = 20.0
    max_retries: int = 4

    def __post_init__(self) -> None:
        if not 1 <= self.max_fills <= MAX_FILLS_CEILING:
            raise ConfigurationError(
                f"max_fills must be between 1 and {MAX_FILLS_CEILING}; got {self.max_fills}"
            )
        if not 1 <= self.page_limit <= PAGE_LIMIT_CEILING:
            raise ConfigurationError(
                f"page_limit must be between 1 and {PAGE_LIMIT_CEILING}; got {self.page_limit}"
            )
        if not self.base_url.startswith("https://"):
            raise ConfigurationError("base_url must be an https:// URL")


def read_credentials(env: dict[str, str] | None = None) -> tuple[str, str]:
    """Return ``(key_id, private_key_pem)`` from the environment.

    Fails closed when either credential is absent or blank.  The returned PEM is
    handed straight to the signer and must not be logged, echoed or stored.
    """
    source = os.environ if env is None else env
    key_id = (source.get(ENV_KEY_ID) or "").strip()
    private_key = source.get(ENV_PRIVATE_KEY) or ""

    missing = []
    if not key_id:
        missing.append(ENV_KEY_ID)
    if not private_key.strip():
        missing.append(ENV_PRIVATE_KEY)
    if missing:
        raise ConfigurationError(
            "Missing required credential environment variable(s): " + ", ".join(sorted(missing))
        )
    return key_id, private_key


def base_url_from_env(env: dict[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    return (source.get(ENV_BASE_URL) or "").strip() or DEFAULT_BASE_URL
