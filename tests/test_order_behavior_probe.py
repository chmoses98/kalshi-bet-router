import importlib.util, json, pathlib

SPEC = importlib.util.spec_from_file_location("pob", pathlib.Path(__file__).parent.parent / "scripts" / "probe_order_behavior.py")
pob = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(pob)


def o(status="executed", typ="limit", init="10", fill="10", maker="4.0", taker="0", a="2026-01-01T00:00:00Z", b="2026-01-01T00:00:30Z", mf="0.02", tf="0"):
    return {"status": status, "type": typ, "initial_count_fp": init, "fill_count_fp": fill, "maker_fill_cost_dollars": maker,
            "taker_fill_cost_dollars": taker, "created_time": a, "last_update_time": b, "maker_fees_dollars": mf, "taker_fees_dollars": tf,
            "order_id": "SECRET-ORDER", "ticker": "KXSECRET-1", "client_order_id": "secret-client"}


def test_cells_count_fill_completeness_and_maker_taker_split():
    out = pob.summarize([o(), o(fill="4"), o(status="canceled", fill="0", maker="0", mf="0"), o(typ="market", maker="0", taker="5", tf="0.1")])
    ex = out["executed|limit"]
    assert ex["orders"] == 2 and ex["fully_filled"] == 1 and ex["partially_filled"] == 1 and ex["fills_all_maker"] == 2
    assert out["canceled|limit"]["zero_fill"] == 1 and out["canceled|limit"]["no_fill_cost"] == 1
    assert out["executed|market"]["fills_all_taker"] == 1 and out["executed|market"]["taker_fee_charged"] == 1
    assert ex["age_lt_60s"] == 2


def test_age_buckets_and_unknowns():
    assert pob.bucket(3) == "lt_5s" and pob.bucket(59) == "lt_60s" and pob.bucket(599) == "lt_10m" and pob.bucket(3599) == "lt_1h" and pob.bucket(9e9) == "ge_1h"
    out = pob.summarize([o(a=None), o(init=None), {"status": "weird", "type": "odd"}])
    assert out["executed|limit"]["age_unknown"] == 1 and out["executed|limit"]["fill_unknown"] == 1
    assert "other|other" in out


def test_output_has_no_values():
    blob = json.dumps(pob.summarize([o()]))
    for secret in ("SECRET", "KXSECRET", "secret-client", "2026-01-01", "4.0", "0.02"):
        assert secret not in blob
