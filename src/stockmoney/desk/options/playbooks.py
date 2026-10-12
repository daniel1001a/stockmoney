"""Intraday playbooks (OPTIONS_PLAN.md section 5) as pure functions of the
1-minute bars known at ``now``.

Look-ahead rule: a bar is known only once it has CLOSED, i.e. at its start
time + 1 minute. Every function first drops bars that have not closed by
``now``, then scans forward and returns the FIRST qualifying bar, so adding
later bars can never change an earlier decision (tests/desk/options assert
this by truncating the day at every minute).

Bars: DataFrame indexed by tz-aware bar START time, columns open/high/low/
close/volume (vwap optional), premarket included (04:00 ET onward).
``prev_close`` is the previous regular session close.

Each playbook returns at most one Signal per symbol per day. Entries are
only considered for bars that close inside the entry window
(09:30 to 11:30 ET by default); a first trigger outside it is no trigger.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from stockmoney.desk.options.config import OptionsConfig

ET = ZoneInfo("America/New_York")
ONE_MIN = pd.Timedelta(minutes=1)


@dataclass(frozen=True)
class Signal:
    source: str
    symbol: str
    direction: str  # long (buy calls) | short (buy puts)
    at: datetime    # close time of the triggering bar (when the decision is knowable)
    price: float    # close of the triggering bar
    invalidation: float
    reason: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["at"] = self.at.isoformat()
        return d


# ------------------------------------------------------------------ bar helpers

def completed(bars: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Only bars that have closed by ``now``, indexed in ET."""
    if bars is None or bars.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    b = bars.copy()
    b.index = pd.DatetimeIndex(b.index).tz_convert(ET)
    b = b.sort_index()
    return b[b.index + ONE_MIN <= pd.Timestamp(now).tz_convert(ET)]


def _at(day, hm: tuple[int, int]) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(day, time(*hm)), tz=ET)


def split_session(b: pd.DataFrame, cfg: OptionsConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(premarket, regular) parts of one day's completed bars."""
    if b.empty:
        return b, b
    day = b.index[-1].date()
    open_ = _at(day, cfg.open_time)
    b = b[b.index.date == day]
    return b[b.index < open_], b[b.index >= open_]


def _in_window(start: pd.Timestamp, cfg: OptionsConfig, lo: tuple[int, int] | None = None,
               hi: tuple[int, int] | None = None) -> bool:
    """A bar may trigger when it starts at/after ``lo`` and closes by ``hi``."""
    day = start.date()
    return _at(day, lo or cfg.entry_window_start) <= start and start + ONE_MIN <= _at(day, hi or cfg.entry_window_end)


def _gap(rth: pd.DataFrame, prev_close: float | None, cfg: OptionsConfig) -> float | None:
    if rth.empty or not prev_close or not math.isfinite(prev_close) or prev_close <= 0:
        return None
    first = rth.index[0]
    if first != _at(first.date(), cfg.open_time):
        return None  # no 09:30 bar: the opening print is unknown, do not guess
    return float(rth["open"].iloc[0]) / prev_close - 1


def session_vwap(rth: pd.DataFrame) -> pd.Series:
    """Cumulative VWAP from the open, using each bar's own vwap when the
    provider gives it, else the typical price."""
    px = rth["vwap"] if "vwap" in rth and rth["vwap"].notna().all() else (rth["high"] + rth["low"] + rth["close"]) / 3
    vol = rth["volume"].astype(float)
    cum_v = vol.cumsum().replace(0, np.nan)
    return (px * vol).cumsum() / cum_v


def _sig(source, symbol, direction, start: pd.Timestamp, price, inv, reason) -> Signal:
    return Signal(source, symbol, direction, (start + ONE_MIN).to_pydatetime(), float(price), float(inv), reason)


# ------------------------------------------------------------------ rule playbooks

def gap_go(bars: pd.DataFrame, now: datetime, prev_close: float | None, symbol: str,
           cfg: OptionsConfig, ctx: dict | None = None) -> Signal | None:
    """Large gap + premarket volume, with the trend: the first 1-minute close
    beyond the first-5-minute high (gap up) / low (gap down). Invalidation:
    the other side of that range. ``ctx["pm_dollar_vol"]`` is the premarket
    dollar volume known before the open (the live IEX feed carries almost no
    premarket volume, so the engine measures it on delayed SIP data); without
    it, the premarket part of ``bars`` is used."""
    b = completed(bars, now)
    pm, rth = split_session(b, cfg)
    gap = _gap(rth, prev_close, cfg)
    if gap is None or abs(gap) < cfg.gap_min(symbol):
        return None
    pm_dv = (ctx or {}).get("pm_dollar_vol")
    if pm_dv is None:
        pm_dv = float((pm["close"] * pm["volume"]).sum())
    if pm_dv < cfg.premarket_min_dollar_vol:
        return None
    n = cfg.gap_go_range_minutes
    if len(rth) <= n:
        return None
    rng = rth.iloc[:n]
    hi, lo = float(rng["high"].max()), float(rng["low"].min())
    for ts, r in rth.iloc[n:].iterrows():
        if not _in_window(ts, cfg):
            if ts + ONE_MIN > _at(ts.date(), cfg.entry_window_end):
                break
            continue
        if gap > 0 and r["close"] > hi:
            return _sig("gap_go", symbol, "long", ts, r["close"], lo, f"gap {gap:+.2%}, close above 5m high {hi:.2f}")
        if gap < 0 and r["close"] < lo:
            return _sig("gap_go", symbol, "short", ts, r["close"], hi, f"gap {gap:+.2%}, close below 5m low {lo:.2f}")
    return None


def gap_fade(bars: pd.DataFrame, now: datetime, prev_close: float | None, symbol: str,
             cfg: OptionsConfig, ctx: dict | None = None) -> Signal | None:
    """Overdone gap: price trades beyond the premarket high (gap up) / low
    (gap down) and then CLOSES back inside the premarket range. Direction is
    against the gap; invalidation is the extreme reached since the open."""
    b = completed(bars, now)
    pm, rth = split_session(b, cfg)
    gap = _gap(rth, prev_close, cfg)
    if gap is None or abs(gap) < cfg.gap_min(symbol) or pm.empty:
        return None
    pm_hi, pm_lo = float(pm["high"].max()), float(pm["low"].min())
    broke, ext = False, None
    for ts, r in rth.iterrows():
        if ts + ONE_MIN > _at(ts.date(), cfg.entry_window_end):
            break
        if gap > 0:
            if r["high"] > pm_hi:
                broke, ext = True, max(ext or r["high"], r["high"])
            if broke and r["close"] < pm_hi and _in_window(ts, cfg):
                return _sig("gap_fade", symbol, "short", ts, r["close"], ext,
                            f"gap {gap:+.2%}, failed above premarket high {pm_hi:.2f}")
        else:
            if r["low"] < pm_lo:
                broke, ext = True, min(ext or r["low"], r["low"])
            if broke and r["close"] > pm_lo and _in_window(ts, cfg):
                return _sig("gap_fade", symbol, "long", ts, r["close"], ext,
                            f"gap {gap:+.2%}, failed below premarket low {pm_lo:.2f}")
    return None


def orb15(bars: pd.DataFrame, now: datetime, prev_close: float | None, symbol: str,
          cfg: OptionsConfig, ctx: dict | None = None) -> Signal | None:
    """Opening-range breakout: the first close beyond the first-15-minute
    range on a bar whose volume beats the mean of the previous 5 bars.
    Invalidation: the middle of the range."""
    b = completed(bars, now)
    _, rth = split_session(b, cfg)
    n, k = cfg.orb_minutes, cfg.orb_volume_lookback
    if len(rth) <= n or rth.index[0] != _at(rth.index[0].date(), cfg.open_time):
        return None
    rng = rth.iloc[:n]
    hi, lo = float(rng["high"].max()), float(rng["low"].min())
    mid = (hi + lo) / 2
    vol = rth["volume"].astype(float)
    for i in range(n, len(rth)):
        ts, r = rth.index[i], rth.iloc[i]
        if ts + ONE_MIN > _at(ts.date(), cfg.entry_window_end):
            break
        if not _in_window(ts, cfg) or r["volume"] <= vol.iloc[i - k:i].mean():
            continue
        if r["close"] > hi:
            return _sig("orb15", symbol, "long", ts, r["close"], mid, f"close above {n}m range high {hi:.2f}")
        if r["close"] < lo:
            return _sig("orb15", symbol, "short", ts, r["close"], mid, f"close below {n}m range low {lo:.2f}")
    return None


def vwap_reclaim(bars: pd.DataFrame, now: datetime, prev_close: float | None, symbol: str,
                 cfg: OptionsConfig, ctx: dict | None = None) -> Signal | None:
    """Sharp opening move away from VWAP (within the first 30 minutes, by at
    least the extension threshold), then price crosses back and HOLDS: the
    bar before the run closed on the move side and ``vwap_hold_bars``
    consecutive closes are on the other side. Down move + reclaim = long,
    up move + loss of VWAP = short. Invalidation: the move's extreme."""
    b = completed(bars, now)
    _, rth = split_session(b, cfg)
    if rth.empty or rth.index[0] != _at(rth.index[0].date(), cfg.open_time):
        return None
    vw = session_vwap(rth)
    ext = cfg.vwap_extension(symbol)
    move_end = rth.index[0] + pd.Timedelta(minutes=cfg.vwap_move_window_minutes)
    down_at = up_at = None
    h = cfg.vwap_hold_bars
    for i in range(len(rth)):
        ts, r, v = rth.index[i], rth.iloc[i], vw.iloc[i]
        if ts + ONE_MIN > _at(ts.date(), cfg.entry_window_end):
            break
        if not math.isfinite(v):
            continue
        if ts < move_end:
            if down_at is None and r["low"] <= v * (1 - ext):
                down_at = i
            if up_at is None and r["high"] >= v * (1 + ext):
                up_at = i
        j = i - h  # the bar before the run
        if j < 0 or not _in_window(ts, cfg):
            continue
        run = rth.iloc[j + 1:i + 1]
        run_vw = vw.iloc[j + 1:i + 1]
        if down_at is not None and j >= down_at and rth["close"].iloc[j] <= vw.iloc[j] \
                and (run["close"] > run_vw).all():
            inv = float(rth["low"].iloc[:i + 1].min())
            return _sig("vwap_reclaim", symbol, "long", ts, r["close"], inv,
                        f"opening drop, reclaimed VWAP {v:.2f} for {h} closes")
        if up_at is not None and j >= up_at and rth["close"].iloc[j] >= vw.iloc[j] \
                and (run["close"] < run_vw).all():
            inv = float(rth["high"].iloc[:i + 1].max())
            return _sig("vwap_reclaim", symbol, "short", ts, r["close"], inv,
                        f"opening pop, lost VWAP {v:.2f} for {h} closes")
    return None


RULES: dict[str, Callable[..., Signal | None]] = {
    "gap_go": gap_go, "gap_fade": gap_fade, "orb15": orb15, "vwap_reclaim": vwap_reclaim,
}


# ------------------------------------------------------------------ Grok plan trigger

def _hm(s: str) -> tuple[int, int]:
    return int(s[:2]), int(s[3:])


def grok_trigger(item, bars: pd.DataFrame, now: datetime, cfg: OptionsConfig, source: str = "grok_plan") -> Signal | None:
    """Evaluate one validated plan item's declared trigger (plan.PlanItem).

    break_above / break_below: ``confirm_closes`` consecutive closes above /
    below ``level``. reclaim_above / lose_below: the same, but price must have
    traded on the other side of ``level`` after the open before the run
    starts. opening_range_break: closes beyond the first-15-minute range in
    the bias direction. Only regular-session bars that start at/after
    ``earliest`` and close by ``latest`` may confirm; the entry window still
    applies on top."""
    b = completed(bars, now)
    _, rth = split_session(b, cfg)
    if rth.empty:
        return None
    t = item.trigger
    direction = "long" if item.bias == "long" else "short"
    inv = float(item.invalidation.underlying_level)
    level = t.level
    start_i = 0
    if t.kind == "opening_range_break":
        n = cfg.orb_minutes
        if len(rth) <= n or rth.index[0] != _at(rth.index[0].date(), cfg.open_time):
            return None
        rng = rth.iloc[:n]
        level = float(rng["high"].max()) if direction == "long" else float(rng["low"].min())
        start_i = n
    above = t.kind in ("break_above", "reclaim_above") or (t.kind == "opening_range_break" and direction == "long")
    need_other_side = t.kind in ("reclaim_above", "lose_below")
    seen_other = False
    run = 0
    for i in range(len(rth)):
        ts, r = rth.index[i], rth.iloc[i]
        if ts + ONE_MIN > _at(ts.date(), _hm(t.latest)) or ts + ONE_MIN > _at(ts.date(), cfg.entry_window_end):
            break
        beyond = r["close"] > level if above else r["close"] < level
        if (r["low"] < level) if above else (r["high"] > level):
            seen_other = True  # traded on the other side (a wick through and back counts)
        ok = (i >= start_i and beyond and _in_window(ts, cfg, lo=_hm(t.earliest), hi=_hm(t.latest))
              and (seen_other or not need_other_side))
        run = run + 1 if ok else 0
        if run >= t.confirm_closes:
            return _sig(source, item.underlying, direction, ts, r["close"], inv,
                        f"{t.kind} {level:.2f} x{t.confirm_closes} closes")
    return None
