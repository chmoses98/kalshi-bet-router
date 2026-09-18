"""The read-only balance capability, and the boundary around it.

Two things are under test and they are not the same thing:

  * the NUMBER is the right one -- available cash, never portfolio value;
  * the WIDENING is one route -- reading, never trading, and nothing about
    the account is persisted beyond the five published fields.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from kalshi_router.balance import (
    BALANCE_PATH,
    PUBLISHED_KEYS,
    REFUSED_SIZING_FIELDS,
    SOURCE_AUTHENTICATED_BALANCE,
    VALUE_TYPE_AVAILABLE_CASH,
    build_bankroll_context,
    fetch_bankroll_context,
    parse_balance_response,
)
from kalshi_router.errors import SchemaError


#: A realistic response, with every field the documented endpoint returns.
#: The extra fields are here on purpose: the test suite has to prove they
#: are ignored, which it cannot do if they are absent.
FULL_RESPONSE = {
    "balance": 123456,                 # $1,234.56 -- AVAILABLE CASH, in cents
    "balance_dollars": "1234.56",
    "portfolio_value": 987654,         # $9,876.54 -- mark-to-market, NOT cash
    "updated_ts": 1789000000,
    "balance_breakdown": [{"exchange_index": 0, "balance": 123456}],
}


# ---------------------------------------------------------------- the number

def test_available_cash_is_read_from_balance_in_cents():
    assert parse_balance_response(FULL_RESPONSE) == Decimal("1234.56")


def test_portfolio_value_is_never_the_sizing_number():
    """`portfolio_value` is 8x larger here. Substituting it would let the
    handicapper stake money that is already at risk in open positions."""
    context = build_bankroll_context(FULL_RESPONSE)
    assert context["bankroll"] == pytest.approx(1234.56)
    assert context["bankroll"] != pytest.approx(9876.54)
    assert context["valueType"] == VALUE_TYPE_AVAILABLE_CASH


def test_a_response_with_only_portfolio_value_is_refused_not_substituted():
    with pytest.raises(SchemaError, match="portfolio_value"):
        parse_balance_response({"portfolio_value": 987654, "updated_ts": 1})


@pytest.mark.parametrize(
    "payload",
    [
        {},                                   # no balance at all
        {"balance": None},
        {"balance": "1234"},                  # string cents
        {"balance": 1234.56},                 # float -- contract changed
        {"balance": True},                    # bool is an int in Python
        {"balance": -500},                    # negative deployable cash
        [],                                   # not an object
        None,
        "1234",
    ],
)
def test_malformed_balance_responses_fail_closed(payload):
    with pytest.raises(SchemaError):
        parse_balance_response(payload)


def test_zero_balance_parses_but_is_zero():
    """Zero is a real, readable state. It is the CONSUMER's job to refuse
    to size against it, which it must do explicitly rather than by the
    number failing to parse."""
    assert parse_balance_response({"balance": 0}) == 0


def test_cents_are_converted_exactly():
    for cents, dollars in ((1, "0.01"), (99, "0.99"), (100, "1.00"), (35000, "350.00")):
        assert parse_balance_response({"balance": cents}) == Decimal(dollars)


# --------------------------------------------------------------- the privacy

def test_published_context_carries_only_the_allowlisted_keys():
    context = build_bankroll_context(FULL_RESPONSE)
    assert set(context) == PUBLISHED_KEYS
    assert set(context) == {
        "schemaVersion", "bankroll", "currency", "observedAt", "source", "valueType",
    }


def test_no_account_metadata_or_raw_response_survives():
    """The explicit never-persist list from the mission brief."""
    context = build_bankroll_context({
        **FULL_RESPONSE,
        "account_id": "ACCT-SECRET-1",
        "member_id": "MEMBER-SECRET-1",
        "subaccount_id": "SUB-SECRET-1",
        "api_key_id": "KEY-SECRET-1",
        "positions": [{"ticker": "KXMLBGAME-XYZ", "position": 42}],
        "fills": [{"fill_id": "FILL-SECRET-1"}],
    })
    blob = json.dumps(context)
    for secret in ("ACCT-SECRET-1", "MEMBER-SECRET-1", "SUB-SECRET-1",
                   "KEY-SECRET-1", "KXMLBGAME-XYZ", "FILL-SECRET-1"):
        assert secret not in blob
    for field in REFUSED_SIZING_FIELDS + ("updated_ts", "account_id", "positions"):
        assert field not in context


def test_context_records_what_the_number_means_and_where_it_came_from():
    context = build_bankroll_context(FULL_RESPONSE, observed_at=datetime(
        2026, 9, 18, 14, 30, 5, tzinfo=timezone.utc))
    assert context["observedAt"] == "2026-09-18T14:30:05Z"
    assert context["source"] == SOURCE_AUTHENTICATED_BALANCE
    assert context["valueType"] == VALUE_TYPE_AVAILABLE_CASH
    assert context["currency"] == "USD"


def test_observed_at_is_the_read_time_not_the_exchange_timestamp():
    """`updated_ts` says when Kalshi recomputed the figure. The consumer's
    freshness window is about how stale the number in ITS hands is, and
    only the read time answers that."""
    context = build_bankroll_context({"balance": 1000, "updated_ts": 0})
    assert context["observedAt"].endswith("Z")
    assert datetime.strptime(context["observedAt"], "%Y-%m-%dT%H:%M:%SZ").year >= 2026


# ----------------------------------------------------------------- the route

def test_the_balance_route_is_the_documented_one():
    assert BALANCE_PATH == "/portfolio/balance"


def test_fetch_uses_the_clients_read_only_balance_method():
    class FakeClient:
        def __init__(self):
            self.calls = 0

        def get_balance(self):
            self.calls += 1
            return FULL_RESPONSE

    client = FakeClient()
    context = fetch_bankroll_context(client)
    assert client.calls == 1
    assert context["bankroll"] == pytest.approx(1234.56)
