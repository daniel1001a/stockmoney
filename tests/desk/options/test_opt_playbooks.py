"""Playbooks are pure functions of the bars closed by ``now`` (no look-ahead)."""
import json
from datetime import timedelta

import pandas as pd
import pytest

from stockmoney.desk.options import playbooks as pb
from stockmoney.desk.options.config import OptionsConfig
from stockmoney.desk.options.plan import PlanItem

from opt_fakes import DAY, bars, day_bars, et, orb_breakout_day, profile

CFG = OptionsConfig()
PROF = profile()  # 20 previous sessions at 50,000 shares a minute


def gap_up_day():
    """Gap +1.5% (prev 600, open 609), first 5 minutes 608.5..609.5, then up."""
    closes = [609.0, 609.2, 608.8, 609.1, 609.0] + [609.0 + 0.1 * i for i in range(1, 60)] + [614.9] * 200
    return day_bars(closes, prev_close=600.0, pm_close=608.0, pm_volume=50_000, opens=[609.0] + closes[:-1])


def gap_fade_day():
    """Gap up to 609 (premarket high ~608.05), pokes above, closes back inside."""
    closes = [609.5, 610.0, 609.0, 607.5, 607.0] + [606.0] * 200
    return day_bars(closes, prev_close=600.0, pm_close=608.0, opens=[609.0] + closes[:-1])


def vwap_day():
    """Sharp drop in the first minutes (well below VWAP), then a reclaim
    before 09:45 (outside the vwap_reclaim window) and flat above VWAP."""
    closes = [600.0, 598.0, 596.0, 595.0, 595.5, 596.5, 597.5, 598.5, 599.5, 600.5, 601.0, 601.5, 602.0] + [602.0] * 200
    return day_bars(closes, prev_close=600.0, opens=[600.0] + closes[:-1])


def vwap_late_day():
    """Steady slide below a falling VWAP until 09:51, then two closes back
    above it (09:56 and 09:57 bars): vwap_reclaim long known at 09:58."""
    closes = [600.0 - 0.2 * i for i in range(22)] + [596.4, 596.8, 597.2, 597.6, 598.4, 599.4, 600.2] + [600.2] * 100
    return day_bars(closes, prev_close=600.0, opens=[600.0] + closes[:-1])


def test_orb15_long_at_breakout_close():
    s = pb.orb15(orb_breakout_day(), et(12, 0), 600.0, "SPY", CFG, {"profile": PROF})
    # range 599.95..600.95; the 09:50 bar closes 600.98 on a 4x volume spike -> known at 09:51
    assert s and s.direction == "long" and s.at == et(9, 51)
    assert s.inv_close == pytest.approx(600.95) and s.invalidation == pytest.approx(599.95)
    # day RVOL through 09:50: (20 x 100k + 400k) / (21 x 50k)
    assert s.meta["day_rvol"] == pytest.approx(2.4e6 / 1.05e6, abs=1e-3)
    assert s.meta["benchmark"] == "SPY" and s.meta["risk_ref"] is not None


def test_orb15_filters():
    b = orb_breakout_day()
    low_rvol = profile(rth_volume=200_000)  # history busier than today: RVOL < 1.5
    assert pb.orb15(b, et(12, 0), 600.0, "SPY", CFG, {"profile": low_rvol}) is None
    s = pb.orb15(b, et(12, 0), 600.0, "SPY", CFG, {})  # no profile: fires, RVOL unknown
    assert s and s.meta["day_rvol"] is None
    flat = b.copy()
    flat.loc[flat.index == pd.Timestamp(et(9, 50)), "volume"] = 120_000  # no volume spike
    later = pb.orb15(flat, et(12, 0), 600.0, "SPY", CFG, {"profile": PROF})
    assert later is None or later.at > et(9, 51)
    # NVDA breaks out but its benchmark QQQ is down since the open: no trigger
    qqq_down = day_bars([600.0 - 0.05 * i for i in range(200)])
    assert pb.orb15(b, et(12, 0), 600.0, "NVDA", CFG, {"profile": PROF, "bench": qqq_down}) is None
    qqq_up_less = day_bars([600.0 + 0.01 * i for i in range(200)])
    s = pb.orb15(b, et(12, 0), 600.0, "NVDA", CFG, {"profile": PROF, "bench": qqq_up_less})
    assert s and s.meta["benchmark"] == "QQQ" and s.meta["ret_open"] >= s.meta["bench_ret_open"] > 0
    qqq_up_more = day_bars([600.0 + 0.2 * i for i in range(200)])  # NVDA lags QQQ: no trigger
    assert pb.orb15(b, et(12, 0), 600.0, "NVDA", CFG, {"profile": PROF, "bench": qqq_up_more}) is None


def test_gap_go_long():
    s = pb.gap_go(gap_up_day(), et(12, 0), 600.0, "SPY", CFG, {"profile": PROF})
    # first 5 bars high 609.25; the 09:37 bar closes 609.3 on volume = the first-5 mean
    assert s and s.direction == "long" and s.at == et(9, 38)
    assert s.meta["rvol5"] == pytest.approx(2.0) and s.meta["gap"] == pytest.approx(0.015)
    assert s.invalidation == pytest.approx(max(609.0, 609.15))  # opening price vs trigger bar low


def test_gap_go_filters():
    assert pb.gap_go(gap_up_day(), et(12, 0), 600.0, "SPY", CFG, {"profile": profile(rth_volume=60_000)}) is None
    s = pb.gap_go(gap_up_day(), et(12, 0), 600.0, "SPY", CFG, {})  # no history: fires, flagged
    assert s and s.meta["rvol5"] is None
    # a single stock needs 1.5%: 609 / 599 is 1.67%, 609 / 600.5 is 1.42%
    assert pb.gap_go(gap_up_day(), et(12, 0), 599.0, "NVDA", CFG, {"profile": PROF}) is not None
    assert pb.gap_go(gap_up_day(), et(12, 0), 600.5, "NVDA", CFG, {"profile": PROF}) is None
    weak = gap_up_day()
    weak.loc[weak.index >= pd.Timestamp(et(9, 35)), "volume"] = 50_000  # below the first-5 mean
    assert pb.gap_go(weak, et(12, 0), 600.0, "SPY", CFG, {"profile": PROF}) is None


def test_gap_go_window_ends_1015():
    closes = [609.0, 609.2, 608.8, 609.1, 609.0] + [609.0] * 50 + [610.0] * 100  # breaks out at 10:25
    b = day_bars(closes, prev_close=600.0, opens=[609.0] + closes[:-1])
    assert pb.gap_go(b, et(12, 0), 600.0, "SPY", CFG, {"profile": PROF}) is None


def test_gap_go_needs_gap():
    assert pb.gap_go(orb_breakout_day(), et(12, 0), 600.0, "SPY", CFG, {"profile": PROF}) is None


def test_gap_fade_short():
    s = pb.gap_fade(gap_fade_day(), et(12, 0), 600.0, "SPY", CFG)
    assert s and s.direction == "short" and s.invalidation >= 610.0 and s.at >= et(9, 36)


def test_vwap_reclaim_long():
    s = pb.vwap_reclaim(vwap_late_day(), et(12, 0), 600.0, "SPY", CFG)
    assert s and s.direction == "long" and s.inv_vwap and s.invalidation is None
    assert et(9, 46) <= s.at <= et(10, 0) and abs(s.meta["vwap_slope"]) >= CFG.vwap_slope_min


def test_vwap_reclaim_window_and_flat_vwap():
    assert pb.vwap_reclaim(vwap_day(), et(12, 0), 600.0, "SPY", CFG) is None  # reclaimed at 09:39, before 09:45
    steep = OptionsConfig(vwap_slope_min=0.05)
    assert pb.vwap_reclaim(vwap_late_day(), et(12, 0), 600.0, "SPY", steep) is None


def test_no_entry_before_0935():
    """A trigger that would come at 09:31 is never taken (global 09:35 start)."""
    closes = [600.0, 603.0] + [603.0] * 50
    b = day_bars(closes, rth_volume=lambda i: 500_000 if i == 1 else 100_000)
    item = _item("break_above", 601.0, earliest="09:35")
    s = pb.grok_trigger(item, b, et(12, 0), CFG)
    assert s is None or s.at >= et(9, 36)


def test_no_trigger_after_entry_window():
    """The same breakout shifted past 11:30 is no trigger."""
    closes = [600.5 + (0.4 if i % 2 else -0.4) for i in range(125)] + [602.0 + 0.1 * i for i in range(30)]
    b = day_bars(closes, rth_volume=lambda i: 100_000 if i < 125 else 500_000)
    assert pb.orb15(b, et(13, 0), 600.0, "SPY", CFG, {"profile": PROF}) is None


def test_missing_open_bar_means_no_gap():
    b = gap_up_day()
    b = b[b.index != pd.Timestamp(et(9, 30))]
    assert pb.gap_go(b, et(12, 0), 600.0, "SPY", CFG, {"profile": PROF}) is None


def test_profile_ignores_today_and_needs_sessions():
    from opt_fakes import history_bars
    h = history_bars()
    p = pb.build_profile(pd.concat([h, gap_up_day()]).sort_index(), DAY, CFG)
    assert p == PROF and p.sessions == 20 and p.cum[4] == pytest.approx(250_000)
    short = pb.build_profile(history_bars(sessions=5), DAY, CFG)
    assert short.sessions == 5
    rth = pb.split_session(gap_up_day(), CFG)[1]
    assert pb.day_rvol(rth, pd.Timestamp(et(9, 35)), short, CFG) is None  # fewer than rvol_min_sessions


DAYS = {"orb": orb_breakout_day, "gap_up": gap_up_day, "gap_fade": gap_fade_day, "vwap": vwap_day,
        "vwap_late": vwap_late_day}


@pytest.mark.parametrize("name", sorted(pb.RULES))
@pytest.mark.parametrize("day", sorted(DAYS))
def test_no_look_ahead(name, day):
    """At every minute: (a) the decision with the full day's bars equals the
    decision with the bars truncated at that minute, and (b) once a signal
    exists it never changes as later bars arrive."""
    full = DAYS[day]()
    bench = day_bars([600.0 + 0.01 * i for i in range(300)])
    fn = pb.RULES[name]
    first = None
    t = et(9, 30)
    cut = lambda df, t: df[df.index + pd.Timedelta(minutes=1) <= pd.Timestamp(t)]
    while t <= et(11, 45):
        a = fn(full, t, 600.0, "NVDA", CFG, {"profile": PROF, "bench": bench})
        b = fn(cut(full, t), t, 600.0, "NVDA", CFG, {"profile": PROF, "bench": cut(bench, t)})
        assert a == b, (name, day, t)
        if first is None and a is not None:
            first = a
            assert a.at <= t
        elif first is not None:
            assert a == first, (name, day, t)
        t += timedelta(seconds=30)


def _item(kind, level, bias="long", inv=None, confirm=1, earliest="09:35", latest="11:30", sym="SPY"):
    inv = inv if inv is not None else (590.0 if bias == "long" else 620.0)
    return PlanItem.model_validate_json(json.dumps({
        "underlying": sym, "bias": bias, "playbook": "other", "thesis": "t", "catalyst": "",
        "trigger": {"kind": kind, "level": level, "confirm_closes": confirm, "earliest": earliest, "latest": latest},
        "invalidation": {"kind": "below" if bias == "long" else "above", "underlying_level": inv},
        "target_underlying_pct": 1, "max_hold_minutes": 60, "preferred_dte": "any", "confidence": 0.5,
        "evidence_urls": []}))


def test_grok_break_above_confirm_closes():
    b = orb_breakout_day()
    s1 = pb.grok_trigger(_item("break_above", 601.5, confirm=1), b, et(12, 0), CFG)
    s3 = pb.grok_trigger(_item("break_above", 601.5, confirm=3), b, et(12, 0), CFG)
    assert s1 and s3 and s3.at == s1.at + timedelta(minutes=2)
    assert s1.invalidation == 590.0 and s1.direction == "long"


def test_grok_window_respected():
    b = orb_breakout_day()
    assert pb.grok_trigger(_item("break_above", 601.5, earliest="09:35", latest="09:50"), b, et(12, 0), CFG) is None
    s = pb.grok_trigger(_item("break_above", 600.0, earliest="10:30", latest="11:00"), b, et(12, 0), CFG)
    assert s.at == et(10, 31)


def test_grok_break_below_and_lose_below():
    closes = [600.0] * 10 + [599.0 - 0.2 * i for i in range(20)] + [595.0] * 100
    b = day_bars(closes)
    s = pb.grok_trigger(_item("break_below", 598.0, bias="short"), b, et(12, 0), CFG)
    assert s.direction == "short" and s.price < 598.0
    s2 = pb.grok_trigger(_item("lose_below", 598.0, bias="short", confirm=2), b, et(12, 0), CFG)
    assert s2.at == s.at + timedelta(minutes=1)


def test_grok_reclaim_requires_other_side_first():
    up = orb_breakout_day()  # never trades below 599.95
    assert pb.grok_trigger(_item("reclaim_above", 599.5), up, et(12, 0), CFG) is None
    s = pb.grok_trigger(_item("reclaim_above", 599.0, confirm=2), vwap_day(), et(12, 0), CFG)
    assert s and s.at == et(9, 40)  # 09:38 closes 599.5, 09:39 closes 600.5: two closes above, known 09:40


def test_grok_opening_range_break():
    s = pb.grok_trigger(_item("opening_range_break", None), orb_breakout_day(), et(12, 0), CFG)
    assert s and s.at >= et(9, 46)


@pytest.mark.parametrize("kind,level,bias", [("break_above", 601.5, "long"), ("reclaim_above", 599.0, "long"),
                                             ("opening_range_break", None, "long"), ("break_below", 600.0, "short")])
def test_grok_trigger_no_look_ahead(kind, level, bias):
    for full in (orb_breakout_day(), vwap_day()):
        item = _item(kind, level, bias)
        first = None
        t = et(9, 30)
        while t <= et(11, 40):
            a = pb.grok_trigger(item, full, t, CFG)
            b = pb.grok_trigger(item, full[full.index + pd.Timedelta(minutes=1) <= pd.Timestamp(t)], t, CFG)
            assert a == b
            if first is None:
                first = a
            else:
                assert a == first
            t += timedelta(minutes=1)
