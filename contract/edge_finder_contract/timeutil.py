"""UTC-only timestamps. A naive datetime is an error here, never a guess."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

#: What a contract timestamp looks like once canonicalised: seconds (optionally fractional), Z.
ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class NaiveTimestampError(ValueError):
    """A timestamp without a zone reached the contract boundary."""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: object) -> datetime:
    """Parse an ISO-8601 timestamp WITH zone information into an aware UTC datetime.

    Accepts ``Z`` or any numeric offset (converted to UTC), and a ``datetime``. Refuses naive
    values, dates, numbers and empty strings: the caller must say what they are."""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise NaiveTimestampError("naive datetime")
        return value.astimezone(timezone.utc)
    if not isinstance(value, str) or not value.strip():
        raise NaiveTimestampError(f"not a timestamp: {value!r}")
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise NaiveTimestampError(f"unparseable timestamp: {value!r}") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise NaiveTimestampError(f"timestamp has no zone: {value!r}")
    return dt.astimezone(timezone.utc)


def to_iso(value: object, *, precision: str = "seconds") -> str:
    """Canonical ``YYYY-MM-DDTHH:MM:SS[.ffffff]Z``. ``precision`` is ``seconds`` or ``microseconds``."""
    dt = parse_ts(value)
    if precision == "microseconds" and dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    return dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_iso_or_none(value: object) -> str | None:
    if value is None or value == "":
        return None
    return to_iso(value)


def is_canonical(value: object) -> bool:
    return isinstance(value, str) and bool(ISO_UTC_RE.match(value))


def age_seconds(as_of: object, now: object | None = None) -> float | None:
    """Seconds between ``as_of`` and ``now`` (both aware). None when ``as_of`` is absent."""
    if as_of is None or as_of == "":
        return None
    reference = parse_ts(now) if now is not None else now_utc()
    return (reference - parse_ts(as_of)).total_seconds()


def to_date(value: object) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and DATE_RE.match(value):
        return value
    return parse_ts(value).date().isoformat()


def plus(ts: object, seconds: float) -> str:
    return to_iso(parse_ts(ts) + timedelta(seconds=seconds))
