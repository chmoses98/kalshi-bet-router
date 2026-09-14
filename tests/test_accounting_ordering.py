"""Deterministic fill ordering: the basis of replay determinism."""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from kalshi_router.accounting.ordering import (
    canonical_fill_sort_key,
    parse_execution_time,
    sort_fills,
)
from kalshi_router.errors import SchemaError
from kalshi_router.models import normalize_fill

from .synthetic import make_accounting_fill


def fill(**kwargs):
    return normalize_fill(make_accounting_fill(**kwargs))


def test_rfc3339_timestamp_parses_to_exact_epoch_seconds():
    f = fill(index=1, quantity="1.00", created_time="1970-01-01T00:00:10Z")
    assert parse_execution_time(f) == Decimal(10)


def test_trailing_z_and_explicit_offset_agree():
    a = fill(index=1, quantity="1.00", created_time="2026-09-01T12:00:00Z")
    b = fill(index=2, quantity="1.00", created_time="2026-09-01T12:00:00+00:00")
    assert parse_execution_time(a) == parse_execution_time(b)


def test_offset_naive_timestamp_is_treated_as_utc_not_local():
    """Otherwise the ordering would vary with the machine's timezone."""
    a = fill(index=1, quantity="1.00", created_time="2026-09-01T12:00:00")
    b = fill(index=2, quantity="1.00", created_time="2026-09-01T12:00:00Z")
    assert parse_execution_time(a) == parse_execution_time(b)


def test_falls_back_to_unix_ts_when_created_time_absent():
    raw = make_accounting_fill(index=1, quantity="1.00")
    del raw["created_time"]
    raw["ts"] = 1788000000
    assert parse_execution_time(normalize_fill(raw)) == Decimal(1788000000)


def test_unparseable_created_time_falls_back_to_ts():
    raw = make_accounting_fill(index=1, quantity="1.00", created_time="not-a-time")
    raw["ts"] = 42
    assert parse_execution_time(normalize_fill(raw)) == Decimal(42)


def test_missing_every_timestamp_fails_closed():
    raw = make_accounting_fill(index=1, quantity="1.00")
    del raw["created_time"]
    with pytest.raises(SchemaError, match="timestamp"):
        parse_execution_time(normalize_fill(raw))


def test_sort_key_is_time_then_fill_id():
    a = fill(index=1, quantity="1.00", created_time="2026-09-01T12:00:00Z")
    b = fill(index=2, quantity="1.00", created_time="2026-09-01T12:00:00Z")
    assert canonical_fill_sort_key(a) < canonical_fill_sort_key(b)


def test_identical_timestamps_are_broken_by_fill_id_deterministically():
    same = "2026-09-01T12:00:00Z"
    fills = [
        fill(index=3, quantity="1.00", created_time=same),
        fill(index=1, quantity="1.00", created_time=same),
        fill(index=2, quantity="1.00", created_time=same),
    ]
    assert [f.fill_id for f in sort_fills(fills)] == [
        "SYNTHFILL-0001", "SYNTHFILL-0002", "SYNTHFILL-0003"
    ]


def test_shuffled_input_sorts_identically_every_time():
    fills = [fill(index=i, quantity="1.00", minute=i) for i in range(12)]
    expected = [f.fill_id for f in sort_fills(fills)]
    for seed in range(15):
        shuffled = list(fills)
        random.Random(seed).shuffle(shuffled)
        assert [f.fill_id for f in sort_fills(shuffled)] == expected


def test_mixed_created_time_and_ts_streams_order_coherently():
    a = fill(index=1, quantity="1.00", created_time="1970-01-01T00:00:05Z")
    raw = make_accounting_fill(index=2, quantity="1.00")
    del raw["created_time"]
    raw["ts"] = 3
    b = normalize_fill(raw)
    assert [f.fill_id for f in sort_fills([a, b])] == [b.fill_id, a.fill_id]
