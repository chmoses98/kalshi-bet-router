import importlib.util, json, pathlib
from decimal import Decimal as D
from types import SimpleNamespace as NS

SPEC = importlib.util.spec_from_file_location("spf", pathlib.Path(__file__).parent.parent / "scripts" / "probe_series_fees.py")
spf = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(spf)


def fill(price, count, fee, taker):
    return NS(price_dollars=D(price), count=D(count), fee_dollars=D(fee), is_taker=taker)


def test_series_of():
    assert spf.series_of("KXNFLSPREAD-26NOV01AAABBB-AAA7") == "KXNFLSPREAD"


def test_published_keeps_only_scalar_fee_fields():
    out = spf.published({"fee_type": "quadratic", "fee_multiplier": 1, "title": "x", "tags": ["a"], "Fee_Bad-1": 3,
                         "fee_blob": {"a": 1}, "fee_long": "x" * 80})
    assert out == {"fee_type": "quadratic", "fee_multiplier": 1}


def test_implied_rates_and_unobserved_maker_is_never_assumed():
    rows = [fill("0.5", 100, "1.75", True), fill("0.5", 100, "1.75", True)]
    out = spf.implied(rows)
    assert out["taker_fills"] == 2 and out["implied_taker_rate_median"] == 0.07
    assert out["maker_rate"] == "UNOBSERVED" and out["implied_maker_rate_median"] is None
    out2 = spf.implied(rows + [fill("0.5", 100, "0.4375", False)])
    assert out2["maker_rate"] == "OBSERVED" and out2["implied_maker_rate_median"] == 0.0175


def test_no_value_leaks_into_probe_output_shape():
    out = spf.implied([fill("0.37", 12, "0.2", True)])
    assert set(out) == {"taker_fills", "maker_fills", "taker_unknown", "implied_taker_rate_median",
                        "implied_maker_rate_median", "maker_rate"}
    assert "0.37" not in json.dumps(out)
