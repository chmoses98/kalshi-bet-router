"""Kalshi API-key authentication (RSA-PSS request signing).

Contract implemented here, per Kalshi's documented API-key scheme:

* ``KALSHI-ACCESS-KEY``       -- the API key id.
* ``KALSHI-ACCESS-TIMESTAMP`` -- current time in **milliseconds** since epoch.
* ``KALSHI-ACCESS-SIGNATURE`` -- base64 of an RSA-PSS signature over the ASCII
  message ``f"{timestamp_ms}{METHOD}{path}"`` where ``METHOD`` is the uppercase
  HTTP verb and ``path`` is the full request path from the API root
  (``/trade-api/v2/...``) **with the query string excluded**.
* PSS parameters: SHA-256 digest, MGF1-SHA256, salt length equal to the digest
  length (32 bytes).

Security invariants for this module:

* The private key object never leaves this module.
* ``__repr__`` is overridden so the signer cannot be accidentally interpolated
  into a log line.
* No exception raised here embeds key material or a signature.
* :func:`redacted_header_names` exists so callers can log *which* headers were
  attached without logging their values.
"""

from __future__ import annotations

import base64
import binascii
import time

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .errors import CredentialError

HEADER_KEY_ID = "KALSHI-ACCESS-KEY"
HEADER_TIMESTAMP = "KALSHI-ACCESS-TIMESTAMP"
HEADER_SIGNATURE = "KALSHI-ACCESS-SIGNATURE"

#: Header names that must be scrubbed before any diagnostic output.
SENSITIVE_HEADERS = frozenset({HEADER_KEY_ID, HEADER_SIGNATURE, "Authorization", "Cookie"})


def build_signature_message(timestamp_ms: int, method: str, path: str) -> str:
    """Return the exact string Kalshi expects to be signed.

    ``path`` must start at the API root and must not carry a query string; any
    ``?...`` suffix is stripped defensively so that a caller passing a fully
    built URL path cannot silently produce an unauthenticated-looking signature.
    """
    signed_path = path.split("?", 1)[0]
    if not signed_path.startswith("/"):
        raise CredentialError("signature path must be absolute and begin with '/'")
    return f"{timestamp_ms}{method.upper()}{signed_path}"


def _normalize_pem(raw: str) -> bytes:
    """Accept the shapes a PEM can take once it has been through a secret store.

    GitHub Actions preserves real newlines in a multi-line secret, but the same
    value is often pasted with escaped ``\\n`` sequences, or base64-wrapped to
    survive a single-line transport.  All three are accepted; anything else
    fails closed.
    """
    text = raw.strip()
    if not text:
        raise CredentialError("private key is empty")

    if "-----BEGIN" not in text:
        # Possibly a base64-wrapped PEM.  Decode strictly; never echo the input.
        try:
            decoded = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CredentialError(
                "private key is not PEM and is not valid base64-wrapped PEM"
            ) from exc
        if b"-----BEGIN" not in decoded:
            raise CredentialError("decoded private key material is not a PEM document")
        return decoded

    if "\\n" in text and "\n" not in text:
        text = text.replace("\\n", "\n")
    return text.encode("utf-8")


class KalshiSigner:
    """Signs read-only Kalshi requests with an RSA private key."""

    __slots__ = ("_key_id", "_private_key")

    def __init__(self, key_id: str, private_key_pem: str) -> None:
        if not key_id.strip():
            raise CredentialError("API key id is empty")
        pem_bytes = _normalize_pem(private_key_pem)
        try:
            key = serialization.load_pem_private_key(pem_bytes, password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm):
            # Deliberately does not chain the original message: some backends
            # include a fragment of the input in their error text.
            raise CredentialError(
                "private key could not be parsed as an unencrypted PEM private key"
            ) from None
        if not isinstance(key, rsa.RSAPrivateKey):
            raise CredentialError("private key is not an RSA key; Kalshi requires RSA-PSS")
        self._key_id = key_id.strip()
        self._private_key = key

    def __repr__(self) -> str:  # pragma: no cover - trivial, but security relevant
        return "<KalshiSigner key_id=***redacted*** private_key=***redacted***>"

    __str__ = __repr__

    def sign(self, timestamp_ms: int, method: str, path: str) -> str:
        """Return the base64 RSA-PSS signature for one request."""
        message = build_signature_message(timestamp_ms, method, path)
        signature = self._private_key.sign(
            message.encode("ascii"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256.digest_size,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("ascii")

    def headers(self, method: str, path: str, timestamp_ms: int | None = None) -> dict[str, str]:
        """Build the three authentication headers for one request."""
        ts = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
        return {
            HEADER_KEY_ID: self._key_id,
            HEADER_TIMESTAMP: str(ts),
            HEADER_SIGNATURE: self.sign(ts, method, path),
        }


def redacted_header_names(headers: dict[str, str]) -> list[str]:
    """Return header *names* only, for safe diagnostics.

    Used instead of logging a header mapping, so that a future change cannot
    turn a debug line into a credential disclosure.
    """
    return sorted(headers)
