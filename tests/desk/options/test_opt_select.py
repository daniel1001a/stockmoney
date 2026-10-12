"""Contract selection, delta fallback, liquidity filters, OCC symbols, universe."""
from dataclasses import replace
from datetime import date, timedelta

import pytest

from stockmoney.desk.options import select as sel
from stockmoney.desk.options import shadow as sh
from stockmoney.desk.options import universe as uni
from stockmoney.desk.options._iface import OptionContract, occ_symbol, parse_occ
from stockmoney.desk.options.config import DeltaBand, DteBucket, OptionsConfig

from opt_fakes import DAY, et

CFG = OptionsConfig()
NOW = et(10, 0)
ZERO = DteBucket("0dte", 0, 0)
SHORT = DteBucket("1-3dte", 1, 3)


def c(strike, right="C", delta=None, bid=1.00, ask=1.04, expiry=DAY, oi=5000, vol=500, age_s=1, iv=None):
    return OptionContract(occ_symbol("SPY", expiry, right, strike), "SPY", right, float(strike), expiry, bid, ask,
                          None, vol, oi, delta, iv, NOW - timedelta(seconds=age_s))


def test_occ_round_trip():
    s = occ_symbol("SPY", date(2026, 10, 13), "C", 781)
    assert s == "SPY261013C00781000"
    assert parse_occ(s) == ("SPY", date(2026, 10, 13), "C", 781.0)
    assert parse_occ(occ_symbol("TSLA", date(2026, 10, 16), "P", 452.5))[3] == 452.5
    with pytest.raises(ValueError):
        parse_occ("SPY 261013C781")


def test_bucket_range_skips_weekend_and_holiday():
    fri = date(2026, 10, 16)
    assert sel.bucket_range(fri, SHORT) == (date(2026, 10, 19), date(2026, 10, 21))
    assert sel.bucket_range(date(2026, 11, 25), SHORT)[0] == date(2026, 11, 27)  # Thanksgiving skipped


def test_picks_band_center_with_provider_delta():
    chain = [c(598, delta=0.62), c(600, delta=0.52), c(601, delta=0.44), c(602, delta=0.36), c(600, "P", delta=-0.48)]
    ch, why, _ = sel.choose(chain, 600.0, NOW, "long", ZERO, DeltaBand(0.40, 0.50), CFG)
    assert why == "" and ch.contract.strike == 601 and ch.delta_source == "provider"
    ch, _, _ = sel.choose(chain, 600.0, NOW, "short", ZERO, DeltaBand(0.40, 0.50), CFG)
    assert ch.contract.right == "P" and ch.delta == pytest.approx(0.48)


def test_nearest_expiry_in_bucket():
    chain = [c(600, delta=0.5, expiry=date(2026, 10, 15)), c(600, delta=0.5, expiry=date(2026, 10, 14))]
    ch, _, _ = sel.choose(chain, 600.0, NOW, "long", SHORT, DeltaBand(0.40, 0.60), CFG)
    assert ch.contract.expiry == date(2026, 10, 14)
    _, why, _ = sel.choose(chain, 600.0, NOW, "long", ZERO, DeltaBand(0.40, 0.60), CFG)
    assert why == "no_expiry_in_bucket"


def test_delta_fallback_from_mid_when_provider_has_none():
    t = sel.years_to_expiry(DAY, NOW)
    px = sel.bs_price(600.0, 601.0, t, 0.20, "C")
    chain = [c(601, bid=round(px - 0.02, 2), ask=round(px + 0.02, 2))]
    d, src = sel.contract_delta(chain[0], 600.0, NOW)
    assert src == "bs_mid" and d == pytest.approx(sel.bs_delta(600.0, 601.0, t, 0.20, "C"), abs=0.03)
    d2, src2 = sel.contract_delta(replace(chain[0], iv=0.20), 600.0, NOW)
    assert src2 == "bs_iv"
    assert sel.contract_delta(c(601, bid=0.0, ask=0.0), 600.0, NOW) == (None, "none")


@pytest.mark.parametrize("kw,reason", [
    (dict(bid=None), "no_two_sided_quote"),
    (dict(bid=0.0, ask=0.05), "no_two_sided_quote"),
    (dict(age_s=120), "stale_quote"),
    (dict(bid=0.10, ask=0.12), "mid_below_min"),
    (dict(bid=1.00, ask=1.30), "spread_too_wide"),
    (dict(oi=10), "open_interest_low"),
    (dict(vol=1), "volume_low"),
])
def test_liquidity_reasons(kw, reason):
    assert sel.liquidity_reason(c(600, delta=0.5, **kw), NOW, CFG) == reason


def test_unknown_oi_and_volume_are_not_filtered():
    assert sel.liquidity_reason(c(600, delta=0.5, oi=None, vol=None), NOW, CFG) is None


def test_no_liquid_contract_reason_and_counts():
    chain = [c(600, delta=0.5, bid=1.0, ask=1.5), c(601, delta=0.45, age_s=999)]
    ch, why, detail = sel.choose(chain, 600.0, NOW, "long", ZERO, DeltaBand(0.40, 0.60), CFG)
    assert ch is None and why == "no_liquid_contract"
    assert detail["spread_too_wide"] == 1 and detail["stale_quote"] == 1


def test_shadow_prices_sizing_and_ticks():
    assert sh.buy_price(1.04, CFG) == 1.04 and sh.sell_price(1.00, CFG) == 1.00
    assert sh.contracts_for(1.04, CFG) == 9 and sh.contracts_for(10.5, CFG) == 0
    assert sh.round_tick(0.64, 0.01, up=True) == 0.64 and sh.round_tick(3.12, 0.05, up=True) == 3.15
    assert sh.round_tick(3.12, 0.05, up=False) == 3.10
    assert sh.option_tick("NVDA", 5.0) == 0.05 and sh.option_tick("SPY", 5.0) == 0.01
    assert sh.half_spread_cost(1.00, 1.04, 3, CFG) == pytest.approx(6.0)


def test_universe_cap_priority_and_logging():
    cfg = OptionsConfig(daily_universe_cap=5)
    members, dropped = uni.build(cfg, ["XYZ", "SPY"], ["ABC"])
    assert [m.symbol for m in members] == ["XYZ", "SPY", "QQQ", "TQQQ", "NVDA"]
    assert members[0].origin == "plan" and members[0].uclass == "high_vol"
    assert ("ABC", "daily_cap") in dropped and all(r == "daily_cap" for _, r in dropped)
    full, dropped = uni.build(OptionsConfig(), [], [])
    assert len(full) == 17 and not dropped
    assert {m.uclass for m in full} == {"index", "mega_cap", "high_vol"}


def test_scan_hit_rules():
    import pandas as pd
    from opt_fakes import day_bars
    b = day_bars([600.0], pm_close=620.0, pm_volume=100_000)
    pm = b[b.index < pd.Timestamp(et(9, 30))]
    pm = pm[pm.index.date == DAY]
    ok, why, _ = uni.scan_hit(pm, 600.0, CFG)
    assert ok
    ok, why, _ = uni.scan_hit(pm, 615.0, CFG)
    assert not ok and why == "scan_gap_small"
    ok, why, _ = uni.scan_hit(pm.assign(volume=1), 600.0, CFG)
    assert not ok and why == "scan_volume_low"
