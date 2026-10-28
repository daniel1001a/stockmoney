"""OCC option symbol helpers and the OptionContract quote helpers."""
from __future__ import annotations

import math
from datetime import date

import pytest

from stockmoney.desk.execution.symbols import OCC_LEN, OccSymbol, is_occ_symbol, occ_symbol, parse_occ
from stockmoney.desk.execution.types import AssetClass, OptionContract


def test_asset_class_value_matches_alpaca():
    assert AssetClass.OPTION.value == "us_option"


@pytest.mark.parametrize("args, compact, padded", [
    (("SPY", date(2026, 10, 13), "C", 580), "SPY261013C00580000", "SPY   261013C00580000"),
    (("AAPL", date(2024, 1, 19), "C", 100), "AAPL240119C00100000", "AAPL  240119C00100000"),
    (("PTON", date(2024, 1, 26), "C", 0.5), "PTON240126C00000500", "PTON  240126C00000500"),
    (("TSLA", date(2024, 1, 26), "P", 210), "TSLA240126P00210000", "TSLA  240126P00210000"),
    (("NVDA", date(2026, 12, 18), "P", 187.5), "NVDA261218P00187500", "NVDA  261218P00187500"),
    (("GOOGL", date(2027, 1, 15), "C", 1234.125), "GOOGL270115C01234125", "GOOGL 270115C01234125"),
    (("SPXW", date(2026, 10, 13), "P", 5800), "SPXW261013P05800000", "SPXW  261013P05800000"),
])
def test_occ_symbol_both_spellings(args, compact, padded):
    assert occ_symbol(*args) == compact
    p = occ_symbol(*args, padded=True)
    assert p == padded and len(p) == OCC_LEN


@pytest.mark.parametrize("occ", [
    "SPY261013C00580000", "SPY   261013C00580000", "AAPL240119C00100000", "PTON240126C00000500",
    "GOOGL270115C01234125", "AAPL1260116C00150000", "ABCDEF260116P00001000",
])
def test_round_trip(occ):
    parts = parse_occ(occ)
    padded = len(occ) == OCC_LEN and " " in occ
    assert occ_symbol(parts.root, parts.expiry, parts.right, parts.strike, padded=padded) == occ
    # and the other spelling parses to the same contract
    other = occ_symbol(parts.root, parts.expiry, parts.right, parts.strike, padded=not padded)
    assert parse_occ(other) == parts


def test_parse_fields():
    assert parse_occ("NVDA261218P00187500") == OccSymbol("NVDA", date(2026, 12, 18), "P", 187.5)
    # a root ending in a digit (adjusted contract) is kept whole
    assert parse_occ("AAPL1260116C00150000").root == "AAPL1"


def test_right_aliases_and_case():
    assert occ_symbol("spy", date(2026, 10, 13), "call", 580) == "SPY261013C00580000"
    assert occ_symbol("SPY", date(2026, 10, 13), "put", 580) == "SPY261013P00580000"


@pytest.mark.parametrize("args", [
    ("", date(2026, 10, 13), "C", 580),
    ("TOOLONGX", date(2026, 10, 13), "C", 580),
    ("1SPY", date(2026, 10, 13), "C", 580),
    ("BRK-B", date(2026, 10, 13), "C", 580),
    ("SPY", date(2026, 10, 13), "X", 580),
    ("SPY", date(2026, 10, 13), "C", 0),
    ("SPY", date(2026, 10, 13), "C", -5),
    ("SPY", date(2026, 10, 13), "C", 580.0001),
    ("SPY", date(2026, 10, 13), "C", 100_000),
    ("SPY", date(2026, 10, 13), "C", float("nan")),
    ("SPY", date(2026, 10, 13), "C", float("inf")),
    ("SPY", date(1999, 10, 13), "C", 580),
    ("SPY", "2026-10-13", "C", 580),
])
def test_occ_symbol_rejects(args):
    with pytest.raises(ValueError):
        occ_symbol(*args)


@pytest.mark.parametrize("bad", [
    "", "AAPL", "BRK-B", "BRK.B", "BTC/USD", "SPY261013X00580000", "SPY261313C00580000",
    "SPY261013C0058000", "SPY261013C00000000", "SPY 261013C00580000", "spy261013C00580000",
    "SPY261013C00580000 ", "TOOLONG261013C00580000", "261013C00580000", None, 12345,
])
def test_parse_rejects(bad):
    with pytest.raises(ValueError):
        parse_occ(bad)
    assert not is_occ_symbol(bad)


@pytest.mark.parametrize("sym", ["AAPL", "BRK-B", "BRK.B", "BTC/USD", "SOXL", "GOOGL", "ABCDEF"])
def test_stock_and_crypto_symbols_are_not_occ(sym):
    assert not is_occ_symbol(sym)


def c(**kw) -> OptionContract:
    base = dict(occ="SPY261013C00580000", underlying="SPY", right="C", strike=580.0, expiry=date(2026, 10, 13))
    base.update(kw)
    return OptionContract(**base)


def test_mid_and_spread():
    k = c(bid=1.0, ask=1.2)
    assert k.mid == pytest.approx(1.1)
    assert k.spread_pct == pytest.approx(0.2 / 1.1)
    assert k.multiplier == 100


@pytest.mark.parametrize("bid, ask", [
    (None, 1.0), (1.0, None), (1.2, 1.0), (-0.1, 1.0), (0.0, 0.0), (float("nan"), 1.0), (1.0, float("inf")),
])
def test_mid_none_for_bad_quotes(bid, ask):
    k = c(bid=bid, ask=ask)
    assert k.mid is None and k.spread_pct is None


def test_zero_bid_has_mid():
    k = c(bid=0.0, ask=0.10)
    assert k.mid == pytest.approx(0.05)
    assert math.isclose(k.spread_pct, 2.0)
