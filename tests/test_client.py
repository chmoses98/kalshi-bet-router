"""Fill ingestion: pagination, bounds, fail-closed schema handling, retries."""

from __future__ import annotations

import pytest

from kalshi_router.client import KalshiReadOnlyClient
from kalshi_router.config import AuditConfig
from kalshi_router.errors import (
    AuthenticationError,
    HttpStatusError,
    RateLimitError,
    SchemaError,
    TransportError,
)

from .synthetic import FailingTransport, FakeTransport, make_fill, paged_fills_handler


def build_client(handler, signer, config=None, sleeps=None):
    transport = FakeTransport(handler)
    client = KalshiReadOnlyClient(
        signer=signer,
        config=config or AuditConfig(max_fills=500, page_limit=100, max_retries=2),
        transport=transport,
        sleep=(sleeps.append if sleeps is not None else (lambda _: None)),
    )
    return client, transport


# ------------------------------------------------------------------ pagination

def test_pagination_walks_every_page_in_order(signer):
    pages = [[make_fill(i) for i in range(0, 3)], [make_fill(i) for i in range(3, 5)]]
    client, transport = build_client(paged_fills_handler(pages), signer)
    fills = list(client.iter_fills(max_fills=100))
    assert [f["fill_id"] for f in fills] == [f"SYNTHFILL-{i:04d}" for i in range(5)]
    assert len(transport.paths) == 2
    assert "cursor=cursor-1" in transport.paths[1]


def test_pagination_stops_at_max_fills_without_fetching_more_pages(signer):
    pages = [[make_fill(i) for i in range(0, 3)], [make_fill(i) for i in range(3, 6)]]
    client, transport = build_client(paged_fills_handler(pages), signer)
    assert len(list(client.iter_fills(max_fills=2))) == 2
    assert len(transport.paths) == 1


def test_empty_fill_history_is_a_valid_state(signer):
    client, _ = build_client(paged_fills_handler([[]]), signer)
    assert list(client.iter_fills(max_fills=50)) == []


def test_page_limit_never_exceeds_remaining(signer):
    pages = [[make_fill(i) for i in range(3)]]
    client, transport = build_client(paged_fills_handler(pages), signer)
    list(client.iter_fills(max_fills=3, page_limit=100))
    assert "limit=3" in transport.paths[0]


# ---------------------------------------------------------------- fail closed

@pytest.mark.parametrize(
    "payload",
    [
        {"cursor": ""},                       # missing 'fills'
        {"fills": "not-a-list", "cursor": ""},
        {"fills": ["not-an-object"], "cursor": ""},
        {"fills": [], "cursor": 12345},
    ],
)
def test_malformed_fill_envelopes_fail_closed(signer, payload):
    client, _ = build_client(lambda m, p, q: (200, payload), signer)
    with pytest.raises(SchemaError):
        list(client.iter_fills(max_fills=10))


def test_non_json_body_fails_closed(signer):
    client, _ = build_client(lambda m, p, q: (200, b"<html>proxy error</html>"), signer)
    with pytest.raises(SchemaError):
        list(client.iter_fills(max_fills=10))


def test_empty_body_fails_closed_rather_than_looking_empty(signer):
    client, _ = build_client(lambda m, p, q: (200, None), signer)
    with pytest.raises(SchemaError):
        list(client.iter_fills(max_fills=10))


def test_json_array_body_fails_closed(signer):
    client, _ = build_client(lambda m, p, q: (200, b"[1,2,3]"), signer)
    with pytest.raises(SchemaError):
        list(client.iter_fills(max_fills=10))


def test_repeated_cursor_refuses_to_loop(signer):
    def handler(method, path, query):
        return 200, {"fills": [make_fill(1)], "cursor": "stuck"}

    client, _ = build_client(handler, signer)
    with pytest.raises(SchemaError, match="repeated a cursor"):
        list(client.iter_fills(max_fills=100))


def test_empty_page_with_live_cursor_fails_closed(signer):
    def handler(method, path, query):
        cursor = query.get("cursor", [None])[0]
        return 200, {"fills": [], "cursor": "" if cursor else "next"}

    client, _ = build_client(handler, signer)
    with pytest.raises(SchemaError, match="empty page"):
        list(client.iter_fills(max_fills=100))


def test_metadata_response_missing_its_object_fails_closed(signer):
    client, _ = build_client(lambda m, p, q: (200, {"unexpected": {}}), signer)
    with pytest.raises(SchemaError):
        client.get_market("KXMLBGAME-SYNTH01-NYY")


# -------------------------------------------------------------- HTTP statuses

@pytest.mark.parametrize("status", [401, 403])
def test_auth_rejection_is_immediate_and_not_retried(signer, status):
    calls = []

    def handler(method, path, query):
        calls.append(path)
        return status, {"error": "denied"}

    client, _ = build_client(handler, signer)
    with pytest.raises(AuthenticationError) as excinfo:
        list(client.iter_fills(max_fills=10))
    assert len(calls) == 1
    assert str(status) in str(excinfo.value)


def test_rate_limit_is_retried_then_succeeds(signer):
    state = {"n": 0}

    def handler(method, path, query):
        state["n"] += 1
        if state["n"] == 1:
            return 429, {"error": "slow down"}
        return 200, {"fills": [make_fill(1)], "cursor": ""}

    sleeps: list[float] = []
    client, _ = build_client(handler, signer, sleeps=sleeps)
    assert len(list(client.iter_fills(max_fills=10))) == 1
    assert len(sleeps) == 1 and sleeps[0] > 0


def test_rate_limit_retries_are_bounded_then_raise(signer):
    sleeps: list[float] = []
    client, _ = build_client(
        lambda m, p, q: (429, {"error": "slow down"}),
        signer,
        config=AuditConfig(max_retries=2),
        sleeps=sleeps,
    )
    with pytest.raises(RateLimitError):
        list(client.iter_fills(max_fills=10))
    assert len(sleeps) == 2  # max_retries sleeps, then give up


def test_server_error_is_retried(signer):
    state = {"n": 0}

    def handler(method, path, query):
        state["n"] += 1
        return (503, {"e": 1}) if state["n"] == 1 else (200, {"fills": [], "cursor": ""})

    client, _ = build_client(handler, signer, sleeps=[])
    assert list(client.iter_fills(max_fills=10)) == []


def test_client_error_is_not_retried(signer):
    calls = []

    def handler(method, path, query):
        calls.append(1)
        return 400, {"error": "bad request"}

    client, _ = build_client(handler, signer)
    with pytest.raises(HttpStatusError):
        list(client.iter_fills(max_fills=10))
    assert len(calls) == 1


def test_network_failure_is_retried_then_reported_without_secrets(signer):
    inner = FakeTransport(paged_fills_handler([[]]))
    failing = FailingTransport(inner, failures=99)
    client = KalshiReadOnlyClient(
        signer=signer,
        config=AuditConfig(max_retries=1),
        transport=failing,
        sleep=lambda _: None,
    )
    with pytest.raises(TransportError) as excinfo:
        list(client.iter_fills(max_fills=10))
    assert failing.calls == 2
    assert "PRIVATE" not in str(excinfo.value).upper()


def test_network_failure_recovers_within_retry_budget(signer):
    inner = FakeTransport(paged_fills_handler([[make_fill(1)]]))
    failing = FailingTransport(inner, failures=1)
    client = KalshiReadOnlyClient(
        signer=signer,
        config=AuditConfig(max_retries=2),
        transport=failing,
        sleep=lambda _: None,
    )
    assert len(list(client.iter_fills(max_fills=10))) == 1


# ------------------------------------------------------------ read-only guard

def test_only_get_requests_are_issued(signer):
    methods = []

    class RecordingTransport(FakeTransport):
        def __call__(self, method, url, headers, timeout):
            methods.append(method)
            return super().__call__(method, url, headers, timeout)

    transport = RecordingTransport(paged_fills_handler([[make_fill(1)]]))
    client = KalshiReadOnlyClient(signer=signer, config=AuditConfig(), transport=transport)
    list(client.iter_fills(max_fills=5))
    assert set(methods) == {"GET"}


@pytest.mark.parametrize(
    "path", ["/portfolio/orders", "/portfolio/balance/transfer", "/exchange/status"]
)
def test_non_allowlisted_paths_are_refused_before_any_request(signer, path):
    calls = []
    client, _ = build_client(lambda m, p, q: (calls.append(p), (200, {}))[1], signer)
    with pytest.raises(SchemaError, match="read-only"):
        client._get(path, "forbidden")
    assert calls == []


def test_every_request_is_signed_with_the_three_auth_headers(signer):
    client, transport = build_client(paged_fills_handler([[make_fill(1)]]), signer)
    list(client.iter_fills(max_fills=5))
    for names in transport.header_names:
        assert "KALSHI-ACCESS-KEY" in names
        assert "KALSHI-ACCESS-SIGNATURE" in names
        assert "KALSHI-ACCESS-TIMESTAMP" in names


# ------------------------------------------------- Phase 0.1 read-only routes

def test_event_metadata_is_returned_from_the_top_level(signer):
    body = {"competition": "Pro Baseball", "competition_scope": "Game"}
    client, transport = build_client(lambda m, p, q: (200, body), signer)
    metadata = client.get_event_metadata("KXMLBGAME-SYNTH01")
    assert metadata["competition"] == "Pro Baseball"
    assert transport.paths[0].endswith("/events/KXMLBGAME-SYNTH01/metadata")


def test_event_metadata_accepts_a_wrapped_envelope(signer):
    body = {"metadata": {"competition": "Pro Football", "competition_scope": "Game"}}
    client, _ = build_client(lambda m, p, q: (200, body), signer)
    assert client.get_event_metadata("KXNFLGAME-SYNTH01")["competition"] == "Pro Football"


@pytest.mark.parametrize("body", [b"not json", b"[1,2]", None])
def test_malformed_event_metadata_fails_closed(signer, body):
    client, _ = build_client(lambda m, p, q: (200, body), signer)
    with pytest.raises(SchemaError):
        client.get_event_metadata("KXMLBGAME-SYNTH01")


def test_event_metadata_http_error_propagates_for_the_resolver_to_absorb(signer):
    client, _ = build_client(lambda m, p, q: (404, {"error": "nope"}), signer)
    with pytest.raises(HttpStatusError):
        client.get_event_metadata("KXMLBGAME-SYNTH01")


def test_taxonomy_and_milestone_routes_are_allowlisted(signer):
    client, transport = build_client(lambda m, p, q: (200, {"filters_by_sports": {}}), signer)
    assert client.get_filters_by_sport() == {"filters_by_sports": {}}
    client.get_milestones({"category": "Sports", "competition": "Pro Football"})
    assert transport.paths[0].endswith("/search/filters_by_sport")
    assert "category=Sports" in transport.paths[1]


def test_new_routes_are_still_signed_and_still_get_only(signer):
    methods = []

    class Recording(FakeTransport):
        def __call__(self, method, url, headers, timeout):
            methods.append(method)
            return super().__call__(method, url, headers, timeout)

    transport = Recording(lambda m, p, q: (200, {"filters_by_sports": {}}))
    client = KalshiReadOnlyClient(signer=signer, config=AuditConfig(), transport=transport)
    client.get_filters_by_sport()
    assert methods == ["GET"]
    assert "KALSHI-ACCESS-SIGNATURE" in transport.header_names[0]


@pytest.mark.parametrize(
    "path",
    ["/portfolio/orders", "/portfolio/balances",
     "/search/anything_else", "/milestone_admin"],
)
def test_mutating_and_unlisted_routes_remain_refused(signer, path):
    client, _ = build_client(lambda m, p, q: (200, {}), signer)
    with pytest.raises(SchemaError, match="read-only"):
        client._get(path, "forbidden")


#: Every Kalshi route that places, changes or cancels an order. Phase C widened
#: the allowlist to reach position and settlement history, so this pins the line
#: that widening must never cross: reading what the account HOLDS is allowed,
#: acting on it is not.
TRADING_ROUTES = [
    "/portfolio/orders",
    "/portfolio/orders/batched",
    "/portfolio/orders/ORDER123",
    "/portfolio/orders/ORDER123/amend",
    "/portfolio/orders/ORDER123/decrease",
    "/portfolio/orders/batched/cancel",
    "/portfolio/orders/queue_position",
    "/portfolio/resting_order_total_value",
    # Account MUTATION, as opposed to the account READ below. The owner
    # authorised reading the balance; moving money was never part of it,
    # and these are pinned so that widening cannot creep.
    "/portfolio/balance/transfer",
    "/portfolio/withdrawals",
    "/portfolio/deposits",
    "/portfolio/transfers",
]


@pytest.mark.parametrize("path", TRADING_ROUTES)
def test_no_trading_route_is_reachable(signer, path):
    """No Kalshi trading endpoint may be implemented -- pinned, not assumed."""
    calls = []
    client, _ = build_client(lambda m, p, q: (calls.append(p), (200, {}))[1], signer)
    with pytest.raises(SchemaError, match="read-only"):
        client._get(path, "forbidden")
    assert calls == []


def test_the_allowlist_itself_contains_no_order_route():
    """Structural, so a future prefix cannot quietly admit order entry.

    `balance` is still barred from the PREFIX list specifically. It is
    reachable only through the exact-match account allowlist below, so
    that no `/portfolio/balance/<something>` sub-route can ever ride in
    behind it.
    """
    from kalshi_router.client import READ_ONLY_PATH_PREFIXES

    for prefix in READ_ONLY_PATH_PREFIXES:
        assert "order" not in prefix
        assert "balance" not in prefix


# ------------------------------------------- authorised read-only balance

def test_balance_is_reachable_and_is_a_signed_get(signer):
    """The owner-authorised widening: READING the balance is permitted."""
    methods = []

    class Recording(FakeTransport):
        def __call__(self, method, url, headers, timeout):
            methods.append(method)
            return super().__call__(method, url, headers, timeout)

    transport = Recording(lambda m, p, q: (200, {"balance": 123456}))
    client = KalshiReadOnlyClient(signer=signer, config=AuditConfig(), transport=transport)
    assert client.get_balance() == {"balance": 123456}
    assert methods == ["GET"]
    assert "KALSHI-ACCESS-SIGNATURE" in transport.header_names[0]
    assert transport.paths[0].endswith("/portfolio/balance")


@pytest.mark.parametrize(
    "path",
    [
        "/portfolio/balance/",
        "/portfolio/balanceX",
        "/portfolio/balance/transfer",
        "/portfolio/balance/withdraw",
    ],
)
def test_balance_allowlist_is_exact_match_not_a_prefix(signer, path):
    """A prefix would admit every future sub-route of /portfolio/balance.

    This is the failure the exact-match set exists to prevent, so it is
    pinned rather than reviewed for.
    """
    calls = []
    client, _ = build_client(lambda m, p, q: (calls.append(p), (200, {}))[1], signer)
    with pytest.raises(SchemaError, match="read-only"):
        client._get(path, "forbidden")
    assert calls == []


def test_account_allowlist_admits_nothing_but_the_balance_read():
    """Structural: the widening is one route, and stays one route."""
    from kalshi_router.client import ACCOUNT_READ_ONLY_PATHS

    assert ACCOUNT_READ_ONLY_PATHS == frozenset({"/portfolio/balance"})
    for path in ACCOUNT_READ_ONLY_PATHS:
        for forbidden in ("order", "transfer", "withdraw", "deposit", "cancel", "amend"):
            assert forbidden not in path


def test_no_mutating_method_exists_on_the_client():
    """READ access is not trading access, and the class shape proves it.

    A test on paths alone would still pass if someone added a
    `place_order` that built its own request. This asserts the client
    exposes no mutating capability at all.
    """
    from kalshi_router.client import KalshiReadOnlyClient as C

    names = [n for n in dir(C) if not n.startswith("__")]
    for verb in ("order", "place", "cancel", "amend", "withdraw",
                 "deposit", "transfer", "post", "put", "delete", "patch"):
        assert not any(verb in n.lower() for n in names), (
            f"{verb!r} appears in the read-only client's API: {names}"
        )


@pytest.mark.parametrize(
    "path", ["/portfolio/positions", "/portfolio/settlements",
             "/historical/fills", "/historical/cutoff",
             "/historical/settlements"],
)
def test_phase_c_read_only_history_routes_are_allowed(signer, path):
    client, _ = build_client(lambda m, p, q: (200, {}), signer)
    client._get(path, "allowed")  # must not raise


# ===================== Phase C: history and reconciliation routes ============

def paged(key, pages):
    """A cursor-paginated fake for a portfolio collection."""
    state = {"i": 0}

    def handler(method, path, query):
        i = state["i"]
        state["i"] += 1
        rows = pages[i]
        cursor = "c%d" % i if i + 1 < len(pages) else ""
        return 200, {key: rows, "cursor": cursor}

    return handler


def test_positions_are_not_truncated_by_max_fills(signer):
    """A short position list would fake a reconciliation failure.

    max_fills bounds the FILL sample deliberately. Applying it to positions
    would drop tickers the exchange says are held, and the replay would then
    look like it was missing history when it was really missing a page.
    """
    pages = [[{"ticker": "A"}] * 50, [{"ticker": "B"}] * 50, [{"ticker": "C"}]]
    client, _ = build_client(paged("market_positions", pages), signer)
    rows = list(client.iter_positions())
    assert len(rows) == 101


def test_settlements_walk_every_page(signer):
    pages = [[{"ticker": "A"}], [{"ticker": "B"}]]
    client, _ = build_client(paged("settlements", pages), signer)
    assert [r["ticker"] for r in client.iter_settlements()] == ["A", "B"]


def test_historical_fills_use_the_same_bounded_walk(signer):
    pages = [[{"fill_id": "h1"}, {"fill_id": "h2"}], [{"fill_id": "h3"}]]
    client, _ = build_client(paged("fills", pages), signer)
    assert [f["fill_id"] for f in client.iter_historical_fills(max_fills=2)] == ["h1", "h2"]


@pytest.mark.parametrize("key,method", [
    ("market_positions", "iter_positions"),
    ("settlements", "iter_settlements"),
])
def test_a_missing_collection_fails_closed_rather_than_looking_empty(signer, key, method):
    client, _ = build_client(lambda m, p, q: (200, {"cursor": ""}), signer)
    with pytest.raises(SchemaError, match="missing"):
        list(getattr(client, method)())


@pytest.mark.parametrize("method", ["iter_positions", "iter_settlements"])
def test_repeated_cursor_refuses_to_loop_on_portfolio_routes(signer, method):
    client, _ = build_client(lambda m, p, q: (200, {"market_positions": [{"t": 1}],
                                                    "settlements": [{"t": 1}],
                                                    "cursor": "same"}), signer)
    with pytest.raises(SchemaError, match="refusing to loop"):
        list(getattr(client, method)())


def test_an_empty_account_has_no_positions_and_that_is_valid(signer):
    client, _ = build_client(lambda m, p, q: (200, {"market_positions": [], "cursor": ""}), signer)
    assert list(client.iter_positions()) == []


def test_historical_cutoff_is_fetched_as_an_object(signer):
    client, _ = build_client(lambda m, p, q: (200, {"cutoff_ts": 1788000000}), signer)
    assert client.get_historical_cutoff()["cutoff_ts"] == 1788000000


# ================= Phase C.15: settlement coverage and its archive probe ======

def test_the_settlements_walk_reports_how_it_ended(signer):
    from kalshi_router.client import WalkStats

    client, _ = build_client(paged("settlements", [[{"ticker": "A"}], [{"ticker": "B"}]]), signer)
    stats = WalkStats()
    rows = list(client.iter_settlements(stats=stats))
    assert len(rows) == 2
    assert stats.pages == 2
    assert stats.rows == 2
    # The walk takes no budget, so exhaustion is the only way it ends -- which
    # is what makes its earliest row a usable coverage floor.
    assert stats.exhausted
    assert not stats.truncated


def test_the_archive_settlements_probe_costs_exactly_one_request(signer):
    calls = []

    def handler(method, path, query):
        calls.append(path)
        return 200, {"settlements": [{"ticker": "A"}], "cursor": "more"}

    client, _ = build_client(handler, signer)
    payload = client.probe_historical_settlements()
    # A probe asks whether the route answers. It does not walk it, cursor or no.
    assert calls == ["/trade-api/v2/historical/settlements"]
    assert payload["settlements"] == [{"ticker": "A"}]


def test_the_archive_settlements_probe_surfaces_a_missing_route(signer):
    client, _ = build_client(lambda m, p, q: (404, {}), signer)
    with pytest.raises(HttpStatusError) as excinfo:
        client.probe_historical_settlements()
    assert excinfo.value.status == 404


# ---------------------------------------------------------------------------
# Transport telemetry: what retrying cost.
#
# A run that spends four minutes in backoff and a run that sails through look
# IDENTICAL in the log without this, and the difference is exactly what says
# whether a 15-minute cadence is sustainable against Kalshi's limits.
# ---------------------------------------------------------------------------

from kalshi_router.http import HttpResponse, TransportTelemetry, request_with_retries


def _attempts(*statuses):
    """A transport that returns each status in turn."""
    responses = iter(statuses)

    def transport(method, url, headers, timeout):
        return HttpResponse(status=next(responses), body=b"{}")

    return transport


def test_a_clean_request_records_one_request_and_no_retries():
    telemetry = TransportTelemetry()

    request_with_retries(
        transport=_attempts(200),
        method="GET",
        url="https://example.test/x",
        headers_factory=dict,
        timeout=1.0,
        max_retries=3,
        operation="probe",
        sleep=lambda _: None,
        telemetry=telemetry,
    )

    assert telemetry.requests == 1
    assert telemetry.retries == 0
    assert telemetry.exhausted == 0


def test_a_rate_limit_is_counted_apart_from_a_server_error():
    """429 and 503 mean different things: one says slow down, the other says
    the exchange is unwell. Pooling them would hide which."""
    telemetry = TransportTelemetry()

    request_with_retries(
        transport=_attempts(429, 503, 200),
        method="GET",
        url="https://example.test/x",
        headers_factory=dict,
        timeout=1.0,
        max_retries=3,
        operation="probe",
        sleep=lambda _: None,
        telemetry=telemetry,
    )

    assert telemetry.retries_rate_limited == 1
    assert telemetry.retries_server_error == 1
    assert telemetry.retries == 2
    assert telemetry.requests == 1, "one REQUEST, retried twice"


def test_backoff_seconds_accumulate():
    telemetry = TransportTelemetry()

    request_with_retries(
        transport=_attempts(429, 429, 200),
        method="GET",
        url="https://example.test/x",
        headers_factory=dict,
        timeout=1.0,
        max_retries=3,
        operation="probe",
        sleep=lambda _: None,
        telemetry=telemetry,
    )

    assert telemetry.backoff_seconds >= 0
    assert isinstance(telemetry.backoff_seconds, int)


def test_a_request_that_exhausts_every_retry_is_counted():
    telemetry = TransportTelemetry()

    with pytest.raises(Exception):
        request_with_retries(
            transport=_attempts(503, 503, 503, 503),
            method="GET",
            url="https://example.test/x",
            headers_factory=dict,
            timeout=1.0,
            max_retries=3,
            operation="probe",
            sleep=lambda _: None,
            telemetry=telemetry,
        )

    assert telemetry.exhausted == 1


def test_telemetry_is_optional_and_changes_nothing_when_absent():
    """No caller is obliged to care, and adding it could not alter behaviour
    for one that does not."""
    body = request_with_retries(
        transport=_attempts(429, 200),
        method="GET",
        url="https://example.test/x",
        headers_factory=dict,
        timeout=1.0,
        max_retries=3,
        operation="probe",
        sleep=lambda _: None,
    )

    assert body == b"{}"


def test_the_telemetry_is_structurally_counts_only():
    telemetry = TransportTelemetry()

    for name, value in telemetry.as_dict().items():
        assert isinstance(value, int), f"{name} is {type(value).__name__}"
    rendered = telemetry.render()
    assert "https://" not in rendered
