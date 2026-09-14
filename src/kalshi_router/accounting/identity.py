"""Deterministic identities, and the boundary where they stop being trustworthy.

Nothing here is dispatched in Phase 1A.  The point is that when a downstream
importer eventually exists, re-processing the same history must produce the
**same** identifiers so an import can be retried safely.

What is stable, and when
------------------------

``Fill`` -- **stable immediately.**  Kalshi's ``fill_id`` is exchange-issued and
never changes.

``Order execution group`` -- **stable immediately.**  Kalshi's ``order_id``,
subject to the group genuinely belonging to one subaccount (enforced in
:mod:`.execution`).

``Position episode`` -- **stable only when the opening boundary is provable.**
An episode is "flat to flat", so its identity depends on knowing where flat was.
Over a bounded window that is unknowable:

    A window beginning at F10 makes F10 look like an OPEN.  Back-filling F1-F9
    may reveal the position was already open, so F10 was an INCREASE.  The
    episode then merges into an older one, its opening fill changes, and the
    identity keyed on F10 ceases to exist.

So a bounded-window episode gets a :class:`ProvisionalIdentity`, which carries no
``source_key``/``source_id`` at all.  It is a *different type* from
:class:`StableIdentity`, so downstream code cannot accidentally accept it: there
is no attribute to read, and :func:`require_importable_identity` refuses it.

Source keys embed subaccounts, tickers and fill ids, so they are **private**:
internal values, never rendered in aggregate output.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

NAMESPACE = "kalshi"


class IdentityNotImportable(Exception):
    """Raised when provisional identity is used where a stable one is required."""


@dataclass(frozen=True)
class StableIdentity:
    """An identity derived from a provable boundary.  Safe for idempotent import."""

    source_key: str

    @property
    def source_id(self) -> str:
        return digest_for(self.source_key)

    @property
    def is_importable(self) -> bool:
        return True


@dataclass(frozen=True)
class ProvisionalIdentity:
    """An identity whose boundary is not provable.  **Never** safe for import.

    Deliberately exposes no ``source_key`` or ``source_id``: the whole failure
    mode this guards against is a provisional key being mistaken for a canonical
    one, so the attribute simply does not exist.  ``debug_label`` is for human
    inspection only and is not stable.
    """

    debug_label: str

    @property
    def is_importable(self) -> bool:
        return False


Identity = StableIdentity | ProvisionalIdentity


def digest_for(source_key: str) -> str:
    """Fixed-width deterministic id for a source key."""
    return hashlib.sha256(source_key.encode("utf-8")).hexdigest()


def fill_source_key(fill_id: str) -> str:
    return f"{NAMESPACE}:fill:{fill_id}"


def order_source_key(order_id: str) -> str:
    return f"{NAMESPACE}:order:{order_id}"


def episode_source_key(subaccount: int | None, ticker: str, opening_fill_id: str) -> str:
    """Identity of one flat-to-flat span on one market, within one subaccount.

    The subaccount is part of the key because positions never net across
    subaccounts; two subaccounts holding the same ticker are two positions.
    """
    account = "default" if subaccount is None else str(subaccount)
    return f"{NAMESPACE}:episode:{account}:{ticker}:{opening_fill_id}"


def require_importable_identity(identity: Identity) -> str:
    """Return the source key, or refuse if the identity is provisional."""
    if isinstance(identity, StableIdentity):
        return identity.source_key
    raise IdentityNotImportable(
        "this episode's opening boundary is not provable from the supplied "
        "history, so it has no importable identity; replay complete or "
        "checkpointed history first"
    )
