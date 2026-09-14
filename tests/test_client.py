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
    "path", ["/portfolio/orders", "/portfolio/balance", "/exchange/status"]
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
    ["/portfolio/orders", "/portfolio/balance",
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
    "/portfolio/balance",
    "/portfolio/resting_order_total_value",
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
    """Structural, so a future prefix cannot quietly admit order entry."""
    from kalshi_router.client import READ_ONLY_PATH_PREFIXES

    for prefix in READ_ONLY_PATH_PREFIXES:
        assert "order" not in prefix
        assert "balance" not in prefix


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
