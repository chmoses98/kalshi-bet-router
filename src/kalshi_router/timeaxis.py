"""One time axis for every event the replay orders or compares.

Fills carry ``created_time`` (RFC3339) or ``ts`` (Unix seconds); settlements
carry ``settled_time`` (RFC3339).  Position accounting compares them against
each other -- "did this episode's activity end before settlement evidence
begins?" is a question about two different endpoints' timestamps -- so they
must land on a single scale, parsed the same way.

The scale is **exact seconds since the Unix epoch** as a
:class:`~decimal.Decimal`.  Binary floating point is not used: a float epoch
second cannot represent every RFC3339 fractional timestamp exactly, and an
ordering that depends on rounding is an ordering that can change between
machines.

Every function here returns ``None`` for an unusable value rather than
substituting a default.  A timestamp the parser cannot read is not "now" and is
not "the epoch"; callers decide what an unreadable time means, and in this
package that decision is always fail-closed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def parse_rfc3339_seconds(raw: object) -> Decimal | None:
    """Parse an RFC3339 timestamp into exact epoch seconds, or ``None``.

    Kalshi reports UTC.  A value carrying no offset is therefore read as UTC
    rather than as local time, which would otherwise make the parse depend on
    the machine's timezone.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    # ``fromisoformat`` accepts a trailing 'Z' from Python 3.11 onward, but the
    # replacement is done anyway so the parse does not depend on the interpreter.
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return Decimal(str((parsed - EPOCH).total_seconds()))


def seconds_to_days(span: Decimal) -> int:
    """Whole days in a span of seconds, truncated toward zero.

    Used for reporting how far back a route reaches.  A day count is a property
    of the route's reach; it carries no ticker, quantity or amount, so it is
    safe for a public log in a way an absolute timestamp is not.
    """
    return int(span / Decimal(86400))


def seconds_to_rfc3339(seconds: Decimal | None) -> str | None:
    """Render exact epoch seconds back as an RFC3339 UTC instant, or ``None``.

    The inverse of :func:`parse_rfc3339_seconds`, needed because the NFL and CFB
    destinations record an ``executed_at`` string while this package carries
    every instant as exact epoch seconds.

    ``None`` in, ``None`` out -- an unreadable execution time is not "now" and
    is not the epoch, and a destination that requires the field should refuse
    the row rather than receive a fabricated instant.

    Always UTC with a trailing ``Z``, because that is what Kalshi reports and
    what both destinations store; a local rendering would make the recorded
    instant depend on which machine ran the import.

    Sub-second precision is TRUNCATED, not rounded, and never routed through a
    float: this module exists because a float epoch second cannot represent
    every RFC3339 instant exactly. The destinations record seconds, so the
    remainder has nowhere to go -- and truncating is the direction that cannot
    move an execution into a later second than the one it happened in.
    """
    if seconds is None:
        return None
    whole = int(seconds // 1)
    moment = EPOCH + timedelta(seconds=whole)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
