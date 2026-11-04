"""Offline tests for the bounded secret-safe read-only Kalshi fill-availability probe."""
import json
import pytest

from scripts import probe_execution_capture as p
from tests.synthetic import make_fill


def test_counts_only_and_no_private_payload_leaks():
    raw = make_fill(1)
    raw.update({
        "order_id": "SENSITIVE-ORDER-123",
        "fill_id": "SENSITIVE-FILL-123",
        "trade_id": "SENSITIVE-TRADE-123",
        "client_order_id": "SENSITIVE-CLIENT-123",
        "is_taker": False,
        "fee_cost": "0.0000",
        "yes_bid_dollars": "0.48",
        "yes_ask_dollars": "0.52",
        "secret_future_field": "DO_NOT_PRINT_ME",
    })
    data = p.summarize_fills([raw])
    assert set(data) == set(p.KEYS)
    assert data["sample_rows"] == data["normalized"] == 1
    assert data["client_order_id_present"] == 1
    assert data["explicit_maker"] == 1
    assert data["exchange_fee_present"] == 1
    assert data["exchange_fee_zero"] == 1
    assert data["entry_yes_bid_and_ask_present"] == 1
    out = json.dumps(data)
    for private in ("SENSITIVE", "DO_NOT_PRINT_ME", "0.48", "0.52", "KXMLBGAME"):
        assert private not in out


def test_missing_fields_do_not_claim_client_order_link_or_maker():
    raw = make_fill(2)
    raw.pop("client_order_id", None)
    raw.pop("is_taker", None)
    raw.pop("fee_cost", None)
    raw.pop("fee_cost_dollars", None)
    raw.pop("fee_dollars", None)
    raw.pop("fees_paid_dollars", None)
    counts = p.summarize_fills([raw])
    assert counts["client_order_id_present"] == 0
    assert counts["explicit_maker"] == 0
    assert counts["taker_unknown"] == 1
    assert counts["exchange_fee_missing"] == 1


def test_rejected_rows_are_counted_never_echoed():
    raw = {"order_id": "PRIVATE-O", "client_order_id": "PRIVATE-C",
           "ticker": "PRIVATE-T", "created_time": "2026-11-04"}
    counts = p.summarize_fills([raw, {"garbage": "PRIVATE-F"}, None])
    assert counts["sample_rows"] == 3
    assert counts["normalized"] == 0
    assert counts["rejected"] == 3
    assert counts["client_order_id_present"] == 1
    assert "PRIVATE" not in json.dumps(counts)


def test_empty_fill_sample_is_not_claimed_to_have_missing_fields():
    counts = p.summarize_fills([])
    assert set(counts) == set(p.KEYS)
    assert all(v == 0 for v in counts.values())


class Stub:
    def __init__(self):
        self.called = []
    def iter_fills(self, *, max_fills, page_limit, stats):
        self.called.append(("live", max_fills, page_limit))
        stats.pages = 1
        stats.exhausted = True
        return iter([make_fill(1)])
    def iter_historical_fills(self, *, max_fills, page_limit, stats):
        self.called.append(("historical", max_fills, page_limit))
        stats.pages = 1
        stats.truncated = True
        return iter([make_fill(2)])


def test_only_bounded_get_fills_routes_are_read():
    client = Stub()
    result = p.probe(client, sample=100)
    assert client.called == [("live", 100, 100)]
    assert result["source"] == "recent_fills"
    assert result["counts"]["sample_rows"] == 1
    assert result["exhausted"] is True and result["truncated"] is False
    assert "orders" not in client.called
    older = p.probe(client, sample=70, historical=True)
    assert client.called[-1] == ("historical", 70, 70)
    assert older["truncated"] is True
    assert older["source"] == "historical_fills"
    assert "recommendation" in older["qualification"]


@pytest.mark.parametrize("sample", [-1, 0, 201, True, None])
def test_unbounded_or_invalid_samples_refused(sample):
    with pytest.raises(ValueError):
        p.probe(Stub(), sample=sample)
