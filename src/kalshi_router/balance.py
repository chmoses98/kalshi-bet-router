"""Read-only account balance, and the minimal context derived from it.

WHY THIS MODULE EXISTS AT ALL
-----------------------------
Phase 0 pinned ``/portfolio/balance`` shut, and that was the right default:
nothing this repository did needed it, and an endpoint nobody needs is an
endpoint that should not be reachable.

The destination repository's real-money handicapping card *does* need it. A
stake size computed against a remembered, hand-typed or derived-from-an-
incomplete-ledger number is not a stake size, it is a guess wearing one.

So this is an explicit, narrow widening, and the boundary is exact:

    PERMITTED    reading the account's available cash balance
    NOT PERMITTED  placing, amending or cancelling an order; withdrawing,
                   depositing or transferring; any other account mutation

``GET /portfolio/balance`` is added to an EXACT-MATCH allowlist of its own
(:data:`kalshi_router.client.ACCOUNT_READ_ONLY_PATHS`), never to the prefix
allowlist -- a prefix would also admit ``/portfolio/balance/anything``, and
``/portfolio/*`` would admit order entry. Every trading route remains pinned
refused by :data:`kalshi_router.tests.TRADING_ROUTES`.

WHICH FIELD, AND WHY THAT ONE
------------------------------
Kalshi's ``GET /trade-api/v2/portfolio/balance`` returns, per its API
reference:

===================  =========================================================
``balance``          the member's **available balance, in cents** (integer)
``balance_dollars``  a dollar-denominated rendering of the same figure
``portfolio_value``  **mark-to-market value of open positions, in cents**
``updated_ts``       when the exchange last recomputed it
``balance_breakdown``  per-exchange-index split of the same balance
===================  =========================================================

This module reads ``balance`` and nothing else, because the question the
handicapping card asks is "how much can be deployed on a new wager right
now". ``portfolio_value`` answers a different question -- what open positions
are currently worth -- and staking against it would double-count exposure
that is already at risk. It is therefore not merely unused here; using it is
refused (:func:`parse_balance_response` ignores it and never falls back to
it).

``balance_dollars`` is ignored too, for a duller reason: two fields that must
agree are a field that can disagree. The integer cents are converted here,
once, with :class:`~decimal.Decimal`.

WHAT IS PUBLISHED, AND WHAT IS NOT
----------------------------------
:func:`build_bankroll_context` emits exactly five keys plus a schema version.
It is an allowlist, not a redaction pass -- a new field cannot leak by being
forgotten:

    bankroll, currency, observedAt, source, valueType

NEVER emitted: API keys, signatures, account ids, subaccount identifiers, the
raw API response, fill ids, positions, ``portfolio_value``,
``balance_breakdown``, or any other account metadata.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .errors import SchemaError

#: The one account route this module may read. EXACT match, never a prefix.
BALANCE_PATH = "/portfolio/balance"

#: What the published number means. Stated in the payload so a consumer can
#: never mistake available cash for portfolio value.
VALUE_TYPE_AVAILABLE_CASH = "KALSHI_AVAILABLE_CASH_BALANCE"

#: Where it came from. The destination repository refuses to size stakes
#: against anything else.
SOURCE_AUTHENTICATED_BALANCE = "kalshi_authenticated_balance"

CONTEXT_SCHEMA_VERSION = "1"
CURRENCY = "USD"

#: The complete set of keys a published context may carry.
PUBLISHED_KEYS = frozenset({
    "schemaVersion", "bankroll", "currency", "observedAt", "source", "valueType",
})

#: Fields that exist in the response and must NEVER become the sizing number.
#: Named explicitly so the refusal is testable rather than merely absent.
REFUSED_SIZING_FIELDS = ("portfolio_value", "balance_breakdown", "balance_dollars")


def parse_balance_response(payload: Any) -> Decimal:
    """Return the account's available cash, in dollars, or fail closed.

    Strict on purpose. Every rejected shape below is a shape where guessing
    would produce a plausible number that is wrong, and a wrong bankroll is
    worse than no bankroll: the card degrades gracefully to "sizing
    unavailable", but it cannot detect a number that is merely incorrect.

    * a non-object response            -> refused
    * ``balance`` missing              -> refused (never falls back to
                                          ``portfolio_value``)
    * ``balance`` not an integer       -> refused (Kalshi documents cents as
                                          an integer; a float here means the
                                          contract changed)
    * ``balance`` a bool               -> refused (``True`` is an ``int``)
    * ``balance`` negative             -> refused; an account cannot hold
                                          negative deployable cash, so this is
                                          a contract change, not a poor day
    """
    if not isinstance(payload, dict):
        raise SchemaError(
            f"balance response was {type(payload).__name__}, expected object"
        )
    if "balance" not in payload:
        raise SchemaError(
            "balance response is missing the 'balance' field; refusing to substitute "
            "portfolio_value, which is mark-to-market value of open positions and would "
            "overstate deployable cash"
        )
    cents = payload["balance"]
    if isinstance(cents, bool) or not isinstance(cents, int):
        raise SchemaError(
            f"balance response field 'balance' was {type(cents).__name__}, expected an "
            "integer number of cents"
        )
    if cents < 0:
        raise SchemaError("balance response reported a negative available balance")
    return (Decimal(cents) / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def build_bankroll_context(payload: Any, observed_at: datetime | None = None) -> dict[str, Any]:
    """The minimal, publishable bankroll context. An allowlist, not a filter.

    ``observedAt`` is the instant THIS process read the balance, not the
    exchange's own ``updated_ts``. The consumer's freshness window is about
    how stale the number in its hands is, and only the read time answers
    that.
    """
    dollars = parse_balance_response(payload)
    when = observed_at or datetime.now(tz=timezone.utc)
    context = {
        "schemaVersion": CONTEXT_SCHEMA_VERSION,
        "bankroll": float(dollars),
        "currency": CURRENCY,
        "observedAt": when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": SOURCE_AUTHENTICATED_BALANCE,
        "valueType": VALUE_TYPE_AVAILABLE_CASH,
    }
    # Structural, not aspirational: the payload is rebuilt from the allowlist
    # so a future edit that adds a key has to add it here too.
    assert set(context) == PUBLISHED_KEYS, "published context drifted from its allowlist"
    return context


def fetch_bankroll_context(client, observed_at: datetime | None = None) -> dict[str, Any]:
    """One authenticated GET, one minimal context. Nothing is retained."""
    return build_bankroll_context(client.get_balance(), observed_at=observed_at)
