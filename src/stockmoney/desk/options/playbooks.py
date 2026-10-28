"""Intraday playbooks (OPTIONS_PLAN.md section 5, filtered versions decided by
the owner on 2026-10-12) as pure functions of the 1-minute bars known at
``now``.

Look-ahead rule: a bar is known only once it has CLOSED, i.e. at its start
time + 1 minute. Every function first drops bars that have not closed by
``now``, then scans forward and returns the FIRST qualifying bar, using only
bars up to that bar (including the benchmark's), so adding later bars can
never change an earlier decision (tests/desk/options assert this by
truncating the day at every minute). The volume profile in ``ctx`` is built
from previous sessions only, before the open.

Bars: DataFrame indexed by tz-aware bar START time, columns open/high/low/
close/volume (vwap optional), premarket included (04:00 ET onward).
``prev_close`` is the previous regular session close.

Volume: the live feed is IEX, a fraction of the consolidated tape, so every
volume rule is a RATIO on one feed (RVOL against the same feed's history,
a bar against other bars). No absolute volume threshold exists here.

Each playbook returns at most one Signal per symbol per day, and only for a
bar that starts inside its own window and closes by the window end.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time
from typing import Callable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from stockmoney.desk.options.config import OptionsConfig

ET = ZoneInfo("America/New_York")
ONE_MIN = pd.Timedelta(minutes=1)
PROFILE_MINUTES = 120  # 09:30 to 11:30: the longest any setup can trigger


@dataclass(frozen=True)
class Signal:
    source: str
    symbol: str
    direction: str  # long (buy calls) | short (buy puts)
    at: datetime    # close time of the triggering bar (when the decision is knowable)
    price: float    # close of the triggering bar
    invalidation: float | None  # underlying PRICE level: the quote crossing it exits
    reason: str
    inv_close: float | None = None  # a completed 1-minute CLOSE beyond this level exits
    inv_vwap: bool = False          # a completed 1-minute close back across session VWAP exits
    meta: dict = field(default_factory=dict)  # rvol5, day_rvol, gap, risk reference ...

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
    """(premarket, regular) parts of the LAST day in ``b``."""
    if b.empty:
        return b, b
    day = b.index[-1].date()
    open_ = _at(day, cfg.open_time)
    b = b[b.index.date == day]
    return b[b.index < open_], b[b.index >= open_]


def _in(start: pd.Timestamp, window: tuple[tuple[int, int], tuple[int, int]], cfg: OptionsConfig) -> bool:
    """A bar may trigger when it starts at/after the window start (and never
    before the global entry start) and closes by the window end."""
    day = start.date()
    lo = max(_at(day, window[0]), _at(day, cfg.entry_window_start))
    hi = min(_at(day, window[1]), _at(day, cfg.entry_window_end))
    return lo <= start and start + ONE_MIN <= hi


def _past(start: pd.Timestamp, window, cfg: OptionsConfig) -> bool:
    day = start.date()
    return start + ONE_MIN > min(_at(day, window[1]), _at(day, cfg.entry_window_end))


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


def atr(rth: pd.DataFrame, n: int) -> float | None:
    """Simple mean of the last ``n`` true ranges of these bars (None with
    fewer than n + 1 bars)."""
    if len(rth) < n + 1:
        return None
    h, l, c = rth["high"].astype(float), rth["low"].astype(float), rth["close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1).iloc[1:]
    v = float(tr.iloc[-n:].mean())
    return v if math.isfinite(v) and v > 0 else None


def _sig(source, symbol, direction, start: pd.Timestamp, price, inv, reason, **kw) -> Signal:
    return Signal(source, symbol, direction, (start + ONE_MIN).to_pydatetime(), float(price),
                  None if inv is None else float(inv), reason, **kw)


# ------------------------------------------------------------------ relative volume

@dataclass(frozen=True)
class VolProfile:
    """Volume of the previous sessions (same feed), built before the open.
    ``cum[k-1]`` = mean cumulative regular-session volume after k minutes
    (k = 1..120); ``pm_mean`` = mean premarket (04:00 to 09:30) volume;
    ``atr_daily`` = simple mean of the last 14 daily true ranges."""
    sessions: int
    cum: tuple[float, ...]
    pm_mean: float | None
    atr_daily: float | None

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict | None) -> "VolProfile | None":
        if not d:
            return None
        return cls(int(d["sessions"]), tuple(float(x) for x in d["cum"]), d.get("pm_mean"), d.get("atr_daily"))


def build_profile(hist: pd.DataFrame, day: date, cfg: OptionsConfig) -> VolProfile:
    """From 1-minute bars of sessions BEFORE ``day`` (anything on or after
    ``day`` is ignored). Uses the last ``rvol_lookback`` sessions that have
    regular-session bars."""
    if hist is None or hist.empty:
        return VolProfile(0, (), None, None)
    b = hist.copy()
    b.index = pd.DatetimeIndex(b.index).tz_convert(ET)
    b = b[b.index.date < day].sort_index()
    days = sorted({d for d in b.index.date})
    rows, pms, ohlc = [], [], []
    for d in days:
        bd = b[b.index.date == d]
        open_ = _at(d, cfg.open_time)
        rth = bd[(bd.index >= open_) & (bd.index < _at(d, (16, 0)))]
        if rth.empty:
            continue
        mins = ((rth.index - open_) / ONE_MIN).astype(int)
        per = np.zeros(PROFILE_MINUTES)
        for m, v in zip(mins, rth["volume"].astype(float)):
            if 0 <= m < PROFILE_MINUTES:
                per[m] += v
        rows.append(np.cumsum(per))
        pms.append(float(bd[bd.index < open_]["volume"].astype(float).sum()))
        ohlc.append((float(rth["high"].max()), float(rth["low"].min()), float(rth["close"].iloc[-1])))
    rows, pms, ohlc = rows[-cfg.rvol_lookback:], pms[-cfg.rvol_lookback:], ohlc[-(cfg.rvol_lookback + 1):]
    if not rows:
        return VolProfile(0, (), None, None)
    cum = tuple(float(x) for x in np.mean(np.vstack(rows), axis=0))
    trs = [max(h - l, abs(h - ohlc[i - 1][2]), abs(l - ohlc[i - 1][2])) for i, (h, l, _) in enumerate(ohlc) if i]
    atr_d = float(np.mean(trs[-14:])) if len(trs) >= 14 else None
    return VolProfile(len(rows), cum, float(np.mean(pms)), atr_d)


def day_rvol(rth: pd.DataFrame, upto: pd.Timestamp, profile: VolProfile | None, cfg: OptionsConfig) -> float | None:
    """Volume of today's bars that START before ``upto`` / the profile's mean
    over the same minutes. None when the profile is unusable."""
    if profile is None or profile.sessions < cfg.rvol_min_sessions or rth.empty:
        return None
    open_ = _at(upto.date(), cfg.open_time)
    k = int((upto - open_) / ONE_MIN)
    if not 1 <= k <= len(profile.cum):
        return None
    mean = profile.cum[k - 1]
    if not mean or mean <= 0:
        return None
    vol = float(rth[(rth.index >= open_) & (rth.index < upto)]["volume"].astype(float).sum())
    return vol / mean


def premarket_rvol(pm: pd.DataFrame, profile: VolProfile | None, cfg: OptionsConfig) -> float | None:
    if profile is None or profile.sessions < cfg.rvol_min_sessions or not profile.pm_mean or profile.pm_mean <= 0:
        return None
    return float(pm["volume"].astype(float).sum()) / profile.pm_mean


def _r(x: float | None, n: int = 3) -> float | None:
    return None if x is None else round(x, n)


# ------------------------------------------------------------------ rule playbooks

def gap_go(bars: pd.DataFrame, now: datetime, prev_close: float | None, symbol: str,
           cfg: OptionsConfig, ctx: dict | None = None) -> Signal | None:
    """Gap >= 1.5% (index ETFs 0.5%) with RVOL of the first 5 minutes >= 2.
    Trigger: the first 1-minute close beyond the opening-5-minute high (gap
    up) / low (gap down) whose volume is at least the mean of those first 5
    bars, inside 09:35 to 10:15. Invalidation: back through today's opening
    price or through the trigger bar's low (high), whichever comes first.
    Without a usable volume profile rvol5 is None and the signal still
    fires, flagged: the engine keeps it shadow-only."""
    b = completed(bars, now)
    _, rth = split_session(b, cfg)
    gap = _gap(rth, prev_close, cfg)
    if gap is None or abs(gap) < cfg.gap_min(symbol):
        return None
    range_end = rth.index[0] + pd.Timedelta(minutes=cfg.gap_go_range_minutes)
    first = rth[rth.index < range_end]
    later = rth[rth.index >= range_end]
    if later.empty:
        return None  # the opening range is not complete yet
    rvol5 = day_rvol(rth, range_end, (ctx or {}).get("profile"), cfg)
    if rvol5 is not None and rvol5 < cfg.gap_go_min_rvol:
        return None
    hi, lo = float(first["high"].max()), float(first["low"].min())
    mean5 = float(first["volume"].astype(float).mean())
    open_px = float(rth["open"].iloc[0])
    meta = {"gap": round(gap, 4), "rvol5": _r(rvol5), "range_hi": hi, "range_lo": lo}
    for ts, r in later.iterrows():
        if _past(ts, cfg.gap_go_window, cfg):
            break
        if not _in(ts, cfg.gap_go_window, cfg) or float(r["volume"]) < mean5:
            continue
        if gap > 0 and r["close"] > hi:
            return _sig("gap_go", symbol, "long", ts, r["close"], max(open_px, float(r["low"])),
                        f"gap {gap:+.2%}, rvol5 {meta['rvol5']}, close above 5m high {hi:.2f}", meta=meta)
        if gap < 0 and r["close"] < lo:
            return _sig("gap_go", symbol, "short", ts, r["close"], min(open_px, float(r["high"])),
                        f"gap {gap:+.2%}, rvol5 {meta['rvol5']}, close below 5m low {lo:.2f}", meta=meta)
    return None


def gap_fade(bars: pd.DataFrame, now: datetime, prev_close: float | None, symbol: str,
             cfg: OptionsConfig, ctx: dict | None = None) -> Signal | None:
    """SHADOW ONLY. Overdone gap: price trades beyond the premarket high (gap
    up) / low (gap down) and then CLOSES back inside the premarket range.
    Direction is against the gap; invalidation is the extreme reached since
    the open."""
    b = completed(bars, now)
    pm, rth = split_session(b, cfg)
    gap = _gap(rth, prev_close, cfg)
    if gap is None or abs(gap) < cfg.gap_min(symbol) or pm.empty:
        return None
    pm_hi, pm_lo = float(pm["high"].max()), float(pm["low"].min())
    broke, ext = False, None
    w = cfg.gap_fade_window
    for ts, r in rth.iterrows():
        if _past(ts, w, cfg):
            break
        if gap > 0:
            if r["high"] > pm_hi:
                broke, ext = True, max(ext or r["high"], r["high"])
            if broke and r["close"] < pm_hi and _in(ts, w, cfg):
                return _sig("gap_fade", symbol, "short", ts, r["close"], ext,
                            f"gap {gap:+.2%}, failed above premarket high {pm_hi:.2f}", meta={"gap": round(gap, 4)})
        else:
            if r["low"] < pm_lo:
                broke, ext = True, min(ext or r["low"], r["low"])
            if broke and r["close"] > pm_lo and _in(ts, w, cfg):
                return _sig("gap_fade", symbol, "long", ts, r["close"], ext,
                            f"gap {gap:+.2%}, failed below premarket low {pm_lo:.2f}", meta={"gap": round(gap, 4)})
    return None


def _ret_since_open(rth: pd.DataFrame, upto: pd.Timestamp) -> float | None:
    """Close of the last bar starting at/before ``upto`` vs the 09:30 open."""
    if rth.empty:
        return None
    sub = rth[rth.index <= upto]
    if sub.empty:
        return None
    o = float(rth["open"].iloc[0])
    return float(sub["close"].iloc[-1]) / o - 1 if o > 0 else None


def orb15(bars: pd.DataFrame, now: datetime, prev_close: float | None, symbol: str,
          cfg: OptionsConfig, ctx: dict | None = None) -> Signal | None:
    """Opening-range breakout. Range = bars 09:30 to 09:45. Trigger: the
    first bar inside 09:45 to 11:00 that closes beyond the range on volume
    > 1.5 x the mean of the previous 5 bars, while the stock's return since
    the open points the same way as its benchmark's (QQQ for tech / growth,
    else SPY), does not lag it, and day RVOL >= 1.5 (None = unknown: the
    signal fires flagged and the engine keeps it shadow-only).
    Invalidation: a 1-minute close back inside the range, or a trade through
    the opposite side. Initial risk reference 0.5 to 0.75 x daily ATR(14)
    is recorded (not used for exits)."""
    ctx = ctx or {}
    b = completed(bars, now)
    _, rth = split_session(b, cfg)
    n, k = cfg.orb_minutes, cfg.orb_volume_lookback
    if rth.empty or rth.index[0] != _at(rth.index[0].date(), cfg.open_time):
        return None
    rng = rth[rth.index < rth.index[0] + pd.Timedelta(minutes=n)]
    if len(rng) == len(rth):
        return None
    hi, lo = float(rng["high"].max()), float(rng["low"].min())
    bench_sym = cfg.benchmark(symbol)
    if bench_sym == symbol:
        bench_rth = rth
    else:
        bench_rth = split_session(completed(ctx.get("bench"), now), cfg)[1]
        if not bench_rth.empty and bench_rth.index[-1].date() != rth.index[0].date():
            bench_rth = bench_rth.iloc[0:0]
    profile = ctx.get("profile")
    atr_d = profile.atr_daily if profile is not None else None
    vol = rth["volume"].astype(float)
    for i in range(len(rng), len(rth)):
        ts, r = rth.index[i], rth.iloc[i]
        if _past(ts, cfg.orb_window, cfg):
            break
        if not _in(ts, cfg.orb_window, cfg) or i < k:
            continue
        if not float(r["volume"]) > cfg.orb_break_volume_mult * float(vol.iloc[i - k:i].mean()):
            continue
        direction = "long" if r["close"] > hi else "short" if r["close"] < lo else None
        if direction is None:
            continue
        rs, rb = _ret_since_open(rth, ts), _ret_since_open(bench_rth, ts)
        if rs is None or rb is None:
            continue
        sign = 1 if direction == "long" else -1
        if not (sign * rs > 0 and sign * rb > 0 and sign * rs >= sign * rb):
            continue
        rv = day_rvol(rth, ts + ONE_MIN, profile, cfg)
        if rv is not None and rv < cfg.orb_min_day_rvol:
            continue
        meta = {"day_rvol": _r(rv), "range_hi": hi, "range_lo": lo, "benchmark": bench_sym,
                "ret_open": round(rs, 4), "bench_ret_open": round(rb, 4),
                "risk_ref": [round(atr_d * m, 4) for m in cfg.orb_risk_atr] if atr_d else None}
        if direction == "long":
            return _sig("orb15", symbol, "long", ts, r["close"], lo, f"close above {n}m range high {hi:.2f}, "
                        f"rvol {meta['day_rvol']}", inv_close=hi, meta=meta)
        return _sig("orb15", symbol, "short", ts, r["close"], hi, f"close below {n}m range low {lo:.2f}, "
                    f"rvol {meta['day_rvol']}", inv_close=lo, meta=meta)
    return None


def vwap_reclaim(bars: pd.DataFrame, now: datetime, prev_close: float | None, symbol: str,
                 cfg: OptionsConfig, ctx: dict | None = None) -> Signal | None:
    """SHADOW ONLY. A close on one side of session VWAP, then
    ``vwap_hold_bars`` consecutive closes on the other side, while VWAP is
    not flat (|VWAP change over the last 15 bars| / VWAP >= vwap_slope_min),
    inside 09:45 to 11:30. Crossing up = long, down = short. Invalidation: a
    1-minute close back across VWAP."""
    b = completed(bars, now)
    _, rth = split_session(b, cfg)
    if rth.empty or rth.index[0] != _at(rth.index[0].date(), cfg.open_time):
        return None
    vw = session_vwap(rth)
    h, sb = cfg.vwap_hold_bars, cfg.vwap_slope_bars
    for i in range(len(rth)):
        ts, r = rth.index[i], rth.iloc[i]
        if _past(ts, cfg.vwap_window, cfg):
            break
        j = i - h  # the bar before the run
        if j < 0 or i < sb or not _in(ts, cfg.vwap_window, cfg):
            continue
        v, v0 = vw.iloc[i], vw.iloc[i - sb]
        if not (math.isfinite(v) and math.isfinite(v0) and v0 > 0) or abs(v - v0) / v0 < cfg.vwap_slope_min:
            continue
        run, run_vw = rth["close"].iloc[j + 1:i + 1], vw.iloc[j + 1:i + 1]
        cj, vj = rth["close"].iloc[j], vw.iloc[j]
        meta = {"vwap": round(float(v), 4), "vwap_slope": round(float((v - v0) / v0), 5)}
        if cj <= vj and (run > run_vw).all():
            return _sig("vwap_reclaim", symbol, "long", ts, r["close"], None,
                        f"reclaimed VWAP {v:.2f} for {h} closes", inv_vwap=True, meta=meta)
        if cj >= vj and (run < run_vw).all():
            return _sig("vwap_reclaim", symbol, "short", ts, r["close"], None,
                        f"lost VWAP {v:.2f} for {h} closes", inv_vwap=True, meta=meta)
    return None


def orb15_5m(bars: pd.DataFrame, now: datetime, prev_close: float | None, symbol: str,
             cfg: OptionsConfig, ctx: dict | None = None) -> Signal | None:
    """SHADOW ONLY. orb15's trigger, unchanged; only the close invalidation
    differs: a completed 5-minute bar (config close_inv_minutes) closing
    back inside the range, not a 1-minute close (engine._close_invalidated)."""
    sig = orb15(bars, now, prev_close, symbol, cfg, ctx)
    return None if sig is None else replace(sig, source="orb15_5m")


RULES: dict[str, Callable[..., Signal | None]] = {
    "gap_go": gap_go, "gap_fade": gap_fade, "orb15": orb15, "orb15_5m": orb15_5m, "vwap_reclaim": vwap_reclaim,
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
    ``earliest`` and close by ``latest`` may confirm; the global entry window
    (09:35 to 11:30) still applies on top."""
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
    window = (_hm(t.earliest), _hm(t.latest))
    seen_other = False
    run = 0
    for i in range(len(rth)):
        ts, r = rth.index[i], rth.iloc[i]
        if _past(ts, window, cfg):
            break
        beyond = r["close"] > level if above else r["close"] < level
        if (r["low"] < level) if above else (r["high"] > level):
            seen_other = True  # traded on the other side (a wick through and back counts)
        ok = i >= start_i and beyond and _in(ts, window, cfg) and (seen_other or not need_other_side)
        run = run + 1 if ok else 0
        if run >= t.confirm_closes:
            return _sig(source, item.underlying, direction, ts, r["close"], inv,
                        f"{t.kind} {level:.2f} x{t.confirm_closes} closes", meta={"playbook": item.playbook})
    return None
