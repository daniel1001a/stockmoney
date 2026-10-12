"""Playbooks are pure functions of the bars closed by ``now`` (no look-ahead)."""
import json
from datetime import timedelta

import pandas as pd
import pytest

from stockmoney.desk.options import playbooks as pb
from stockmoney.desk.options.config import OptionsConfig
from stockmoney.desk.options.plan import PlanItem

from opt_fakes import DAY, day_bars, et, orb_breakout_day

CFG = OptionsConfig()


def gap_up_day():
    """Gap +1.5% (prev 600, open 609), first 5 minutes 608.5..609.5, then up."""
    closes = [609.0, 609.2, 608.8, 609.1, 609.0] + [609.0 + 0.1 * i for i in range(1, 60)] + [614.9] * 200
    return day_bars(closes, prev_close=600.0, pm_close=608.0, pm_volume=50_000, opens=[609.0] + closes[:-1])


def gap_fade_day():
    """Gap up to 609 (premarket high ~608.05), pokes above, closes back inside."""
    closes = [609.5, 610.0, 609.0, 607.5, 607.0] + [606.0] * 200
    return day_bars(closes, prev_close=600.0, pm_close=608.0, opens=[609.0] + closes[:-1])


def vwap_day():
    """Sharp drop in the first minutes (well below VWAP), then a reclaim."""
    closes = [600.0, 598.0, 596.0, 595.0, 595.5, 596.5, 597.5, 598.5, 599.5, 600.5, 601.0, 601.5, 602.0] + [602.0] * 200
    return day_bars(closes, prev_close=600.0, opens=[600.0] + closes[:-1])


def test_orb15_long_at_breakout_close():
    s = pb.orb15(orb_breakout_day(), et(12, 0), 600.0, "SPY", CFG)
    # range 599.95..600.95; 09:50 bar closes 600.98 on rising volume -> known at 09:51
    assert s and s.direction == "long" and s.at == et(9, 51)
    assert s.invalidation == pytest.approx((600.95 + 599.95) / 2)


def test_gap_go_long():
    s = pb.gap_go(gap_up_day(), et(12, 0), 600.0, "SPY", CFG, {"pm_dollar_vol": 1e9})
    assert s and s.direction == "long" and s.at > et(9, 35)
    assert pb.gap_go(gap_up_day(), et(12, 0), 600.0, "SPY", CFG, {"pm_dollar_vol": 1.0}) is None  # no premarket volume


def test_gap_go_needs_gap():
    assert pb.gap_go(orb_breakout_day(), et(12, 0), 600.0, "SPY", CFG, {"pm_dollar_vol": 1e9}) is None


def test_gap_fade_short():
    s = pb.gap_fade(gap_fade_day(), et(12, 0), 600.0, "SPY", CFG)
    assert s and s.direction == "short" and s.invalidation >= 610.0


def test_vwap_reclaim_long():
    s = pb.vwap_reclaim(vwap_day(), et(12, 0), 600.0, "SPY", CFG)
    assert s and s.direction == "long" and s.invalidation == pytest.approx(594.95)


def test_no_trigger_after_entry_window():
    """The same breakout shifted past 11:30 is no trigger."""
    closes = [600.5 + (0.4 if i % 2 else -0.4) for i in range(125)] + [602.0 + 0.1 * i for i in range(30)]
    b = day_bars(closes, rth_volume=lambda i: 100_000 if i < 125 else 500_000)
    assert pb.orb15(b, et(13, 0), 600.0, "SPY", CFG) is None


def test_missing_open_bar_means_no_gap():
    b = gap_up_day()
    b = b[b.index != pd.Timestamp(et(9, 30))]
    assert pb.gap_go(b, et(12, 0), 600.0, "SPY", CFG, {"pm_dollar_vol": 1e9}) is None


DAYS = {"orb": orb_breakout_day, "gap_up": gap_up_day, "gap_fade": gap_fade_day, "vwap": vwap_day}


@pytest.mark.parametrize("name", sorted(pb.RULES))
@pytest.mark.parametrize("day", sorted(DAYS))
def test_no_look_ahead(name, day):
    """At every minute: (a) the decision with the full day's bars equals the
    decision with the bars truncated at that minute, and (b) once a signal
    exists it never changes as later bars arrive."""
    full = DAYS[day]()
    fn = pb.RULES[name]
    ctx = {"pm_dollar_vol": 1e9}
    first = None
    t = et(9, 30)
    while t <= et(11, 45):
        a = fn(full, t, 600.0, "SPY", CFG, ctx)
        trunc = full[full.index + pd.Timedelta(minutes=1) <= pd.Timestamp(t)]
        b = fn(trunc, t, 600.0, "SPY", CFG, ctx)
        assert a == b, (name, day, t)
        if first is None and a is not None:
            first = a
            assert a.at <= t
        elif first is not None:
            assert a == first, (name, day, t)
        t += timedelta(seconds=30)


def _item(kind, level, bias="long", inv=None, confirm=1, earliest="09:30", latest="11:30", sym="SPY"):
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
    assert pb.grok_trigger(_item("break_above", 601.5, earliest="09:30", latest="09:50"), b, et(12, 0), CFG) is None
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
