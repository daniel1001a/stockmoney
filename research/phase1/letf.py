"""Sleeve 1: leveraged-ETF trend timing (TQQQ on QQQ, SOXL on SOXX).

Pre-declared rules (fixed before any result was looked at):
- V1 `sma200`      : hold the 3x ETF while the underlying closes above its 200d SMA, else cash.
- V2 `sma200_b2`   : same with a +/-2% hysteresis band (enter above SMA*1.02, exit below
                     SMA*0.98). 2% is ~1.4 daily sigmas of QQQ, enough to ignore most
                     single-day noise crosses; chosen from the common 1-3% range, not tuned.
- V3 `sma200_b2_vol`: V2, but also flat while the underlying's 20d realized vol is above
                     the 90th percentile of its OWN history up to that day (expanding window,
                     min 2y), i.e. "top decile" measured point-in-time, no look-ahead.
Cash earns BIL (or ^IRX before BIL existed). Slippage per side: 5 bps TQQQ, 10 bps SOXL.

Real-ETF period: signal on QQQ/SOXX close T, fill at the 3x ETF's open T+1.

Synthetic 3x series (to reach 1986/1995 and include 1987, 2000-02, 2008):
    S_t = S_{t-1} * (1 + 3 * r_t - drag_t),  r_t = index close-to-close return,
    drag_t = (0.95% expense + 2 * (T-bill_{t-1} + 0.50% swap spread)) / 252.
Index = ^NDX (from 1985) and ^SOX (from 1994), price indices. Pre-2000 index opens in
the Yahoo data are just the prior close, so the synthetic tests use a different, stated
timing: signal at close T, trade at close T+1 (one full day of delay). The overlap with
the real ETFs (2010+) is reported so the effect of that timing + drag model is visible.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import common as C

ER, SPREAD, LEV = 0.0095, 0.0050, 3.0
BAND = 0.02
VOL_Q, VOL_MIN = 0.90, 504


def synthetic(index_close: pd.Series, irx: pd.Series) -> pd.Series:
    r = index_close.pct_change()
    rf = irx.reindex(index_close.index).ffill().shift(1) / 100
    drag = (ER + (LEV - 1) * (rf + SPREAD)) / 252
    daily = (LEV * r - drag).fillna(0.0)
    return (1 + daily).cumprod()


def signals(under_close: pd.Series) -> dict[str, pd.Series]:
    sma = under_close.rolling(200).mean()
    rv = C.realized_vol(under_close, 20)
    thr = rv.expanding(min_periods=VOL_MIN).quantile(VOL_Q)
    hot = (rv > thr).fillna(False)
    v1 = (under_close > sma).astype(float).where(sma.notna(), 0.0)
    v2 = C.hysteresis(under_close, sma, BAND)
    v3 = v2.where(~hot, 0.0)
    return {"sma200": v1, "sma200_b2": v2, "sma200_b2_vol": v3}


def simulate_close_fill(signal: pd.Series, close: pd.Series, cash_r: pd.Series, slip: float) -> C.SimResult:
    """Signal at close T, trade at close T+1: position during day d = signal[d-2]."""
    # Shift the signal one extra day and treat "open" as the previous close, so the
    # generic simulator books the entry/exit at the close of T+1.
    sig = signal.shift(1).fillna(0.0)
    fake_open = close.shift(1)
    return C.simulate(sig, fake_open, close, cash_r, slip)


def run(core: pd.DataFrame) -> dict:
    close, open_ = C.wide(core, "close"), C.wide(core, "open")
    irx, bil = close["^IRX"], close["BIL"]
    out = {"real": {}, "synth": {}, "bench": {}, "trades": {}, "pos": {}}
    pairs = {
        "TQQQ": dict(under="QQQ", etf="TQQQ", index="^NDX", slip=0.0005),
        "SOXL": dict(under="SOXX", etf="SOXL", index="^SOX", slip=0.0010),
    }
    for name, p in pairs.items():
        # ---------------- real ETF period
        etf_c, etf_o = close[p["etf"]].dropna(), open_[p["etf"]].dropna()
        idx = etf_c.index
        under = close[p["under"]].dropna()
        cash = C.cash_returns(idx, irx, bil)
        for v, sig in signals(under).items():
            res = C.simulate(sig.reindex(idx).fillna(0), etf_o, etf_c, cash, p["slip"])
            key = f"{name}_{v}"
            out["real"][key], out["trades"][key], out["pos"][key] = res.ret, res.trades, res.pos
        out["bench"][f"{p['etf']}_BH"] = etf_c.pct_change().fillna(0)
        out["bench"][f"{p['under']}_BH"] = under.reindex(idx).pct_change().fillna(0)

        # ---------------- synthetic long period
        ic = close[p["index"]].dropna()
        syn = synthetic(ic, irx)
        sidx = syn.index
        scash = C.cash_returns(sidx, irx, bil)
        for v, sig in signals(ic).items():
            res = simulate_close_fill(sig, syn, scash, p["slip"])
            key = f"{name}syn_{v}"
            out["synth"][key], out["trades"][key], out["pos"][key] = res.ret, res.trades, res.pos
        out["bench"][f"{name}syn_BH"] = syn.pct_change().fillna(0)
        out["bench"][f"{p['index']}_BH"] = ic.pct_change().fillna(0)
        # tracking check: synthetic vs real over the overlap
        both = pd.concat([syn.pct_change(), etf_c.pct_change()], axis=1).dropna()
        out.setdefault("tracking", {})[name] = {
            "real_CAGR": C.stats(both.iloc[:, 1])["CAGR"],
            "syn_CAGR": C.stats(both.iloc[:, 0])["CAGR"],
            "daily_corr": both.corr().iloc[0, 1],
            "start": both.index[0].date(),
        }
    return out
