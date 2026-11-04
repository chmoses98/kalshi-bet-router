import json, importlib.util, pathlib
import pytest
from kalshi_router.client import KalshiReadOnlyClient, WalkStats, _assert_read_only, ORDERS_PATH
from kalshi_router.config import AuditConfig
from kalshi_router.errors import SchemaError
from tests.test_client import build_client

SPEC = importlib.util.spec_from_file_location("pol", pathlib.Path(__file__).parent.parent / "scripts" / "probe_order_linkage.py")
pol = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(pol)


def order(i, coid=None, status="executed"):
    o = {"order_id": f"ORD-{i}", "ticker": "KXSYNTH-1", "status": status, "created_time": "2026-01-01T00:00:00Z",
         "taker_fill_count": 1, "maker_fill_count": 0, "taker_fees_dollars": "0.01"}
    if coid is not None:
        o["client_order_id"] = coid
    return o


def test_exact_orders_list_route_allowed_but_subroutes_refused():
    _assert_read_only(ORDERS_PATH)
    for p in ("/portfolio/orders/ORD-1", "/portfolio/orders/batched", "/portfolio/orders/ORD-1/amend",
              "/portfolio/orders/batched/cancel", "/portfolio/orders/queue_position", "/portfolio/orders/"):
        with pytest.raises(SchemaError):
            _assert_read_only(p)


def test_iter_orders_is_get_only_bounded_and_filters_params(signer):
    seen = []
    def handler(m, p, q):
        seen.append((m, p))
        return 200, {"orders": [order(i) for i in range(5)], "cursor": "c1"}
    client, transport = build_client(handler, signer)
    rows = list(client.iter_orders(3, page_limit=5))
    assert len(rows) == 3 and {m for m, _ in seen} == {"GET"}
    assert all("/portfolio/orders" in p for _, p in seen)
    with pytest.raises(SchemaError):
        list(client.iter_orders(0))
    with pytest.raises(SchemaError):
        list(client.iter_orders(5, status="bogus"))


def test_probe_counts_only_and_never_prints_values():
    orders = [order(1, "sift:abc"), order(2, "manual-xyz"), order(3), order(4, status="resting")]
    fills = [{"order_id": "ORD-1"}, {"order_id": "ORD-3"}, {"order_id": "ORD-999"}, {"fill_id": "F"}]
    out = pol.summarize(orders, fills)
    c = out["counts"]
    assert c["orders_sampled"] == 4 and c["client_order_id_present"] == 2 and c["client_order_id_sift_prefixed"] == 1
    assert c["fills_sampled"] == 4 and c["fills_with_order_id"] == 3 and c["fills_joined_to_sampled_order"] == 2
    assert c["joined_orders_with_client_order_id"] == 1 and c["joined_orders_sift_prefixed"] == 1
    blob = json.dumps(out)
    for secret in ("ORD-1", "sift:abc", "manual-xyz", "KXSYNTH", "2026-01-01", "0.01"):
        assert secret not in blob


def test_probe_field_names_must_be_safe_identifiers():
    out = pol.summarize([{"order_id": "o", "KXBAD-1": 1, "ok_name": 2}], [])
    assert "ok_name" in out["order_field_names"] and not any("KX" in k for k in out["order_field_names"])


def test_no_client_order_id_anywhere_yields_zero_not_inference():
    out = pol.summarize([order(i) for i in range(10)], [{"order_id": f"ORD-{i}"} for i in range(10)])
    assert out["counts"]["client_order_id_present"] == 0 and out["counts"]["joined_orders_with_client_order_id"] == 0
