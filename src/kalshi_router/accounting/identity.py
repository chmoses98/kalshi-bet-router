"""Deterministic identities for future idempotent downstream import.

Nothing here is dispatched in Phase 1A.  The point is that when a downstream
importer eventually exists, re-processing the same fill history must produce the
**same** identifiers, so an import can be retried safely.

Rules
-----
* A **fill** is identified by Kalshi's ``fill_id``.  The exchange already
  guarantees it; inventing anything else would be worse.
* An **order execution group** is identified by Kalshi's ``order_id``.
* A **position episode** is identified by the market ticker plus the ``fill_id``
  of the fill that took the position off flat.  Both are immutable exchange
  evidence, so the identity is stable under replay.

No identifier is derived from wall-clock time, iteration order, or a freshly
generated UUID.  Each is exposed both as a readable source key and as a
``sha256`` digest of that key, so a downstream system can use a fixed-width id
without the composition becoming guesswork.

Source keys embed market tickers and fill ids, so they are **private**: they are
internal values, never rendered in aggregate output.
"""

from __future__ import annotations

import hashlib

NAMESPACE = "kalshi"


def _digest(source_key: str) -> str:
    return hashlib.sha256(source_key.encode("utf-8")).hexdigest()


def fill_source_key(fill_id: str) -> str:
    return f"{NAMESPACE}:fill:{fill_id}"


def order_source_key(order_id: str) -> str:
    return f"{NAMESPACE}:order:{order_id}"


def episode_source_key(ticker: str, opening_fill_id: str) -> str:
    """Identity of one flat-to-flat span on one market.

    Keyed on the opening fill rather than on a sequence number, so inserting
    older history later cannot renumber existing episodes.
    """
    return f"{NAMESPACE}:episode:{ticker}:{opening_fill_id}"


def digest_for(source_key: str) -> str:
    """Fixed-width deterministic id for a source key."""
    return _digest(source_key)
