"""Sleeve 3: crypto trend (BTC-USD, ETH-USD daily from yfinance).

Pre-declared rules (fixed before any result was looked at):
- `sma100` (primary): hold while close > 100-day SMA, else cash.
- `sma50`  (secondary, reported regardless): same with a 50-day SMA.
- `donch55_20`: enter when close > highest close of the prior 55 days; exit when
                close < lowest close of the prior 20 days (Turtle "system 2" shape; the
                55 mirrors the desk's 55-day breakout).
Crypto trades 7 days a week: bars are calendar days (yfinance UTC daily bars), the
SMA/Donchian windows count calendar days, and Sharpe/Sortino annualize with 365.
Signal at the daily close (00:00 UTC), fill at the next bar's open (~same instant).
Costs: 0.30% per side = assumed 0.25% taker fee + 0.05% half-spread/slippage. This is an
assumption to be checked against the broker's current crypto fee schedule.
Idle cash earns 0% (crypto broker cash typically does not accrue interest; conservative).
"""
from __future__ import annotations

import pandas as pd

import common as C

COST = 0.0030


def signals(close: pd.Series) -> dict[str, pd.Series]:
    hi55 = close.shift(1).rolling(55).max()
    lo20 = close.shift(1).rolling(20).min()
    don = pd.Series(0.0, index=close.index)
    state = 0.0
    for i, (c, h, l) in enumerate(zip(close, hi55, lo20)):
        if pd.notna(h) and c > h:
            state = 1.0
        elif pd.notna(l) and c < l:
            state = 0.0
        don.iloc[i] = state
    out = {}
    for n in (100, 50):
        sma = close.rolling(n).mean()
        out[f"sma{n}"] = (close > sma).astype(float).where(sma.notna(), 0.0)
    out["donch55_20"] = don
    return out


def run(core: pd.DataFrame) -> dict:
    close, open_ = C.wide(core, "close"), C.wide(core, "open")
    out = {"ret": {}, "trades": {}, "pos": {}, "bench": {}}
    for sym in ("BTC-USD", "ETH-USD"):
        c, o = close[sym].dropna(), open_[sym].dropna()
        zero = pd.Series(0.0, index=c.index)
        tag = sym.split("-")[0]
        for v, sig in signals(c).items():
            res = C.simulate(sig, o, c, zero, COST)
            k = f"{tag}_{v}"
            out["ret"][k], out["trades"][k], out["pos"][k] = res.ret, res.trades, res.pos
        out["bench"][f"{tag}_BH"] = c.pct_change().fillna(0)
    return out


def to_trading_days(ret: pd.Series, nyse_index: pd.DatetimeIndex) -> pd.Series:
    """Compound calendar-day returns onto NYSE dates (weekend P&L lands on Monday)."""
    g = (1 + ret).cumprod()
    g = g.reindex(g.index.union(nyse_index)).ffill()
    return g.reindex(nyse_index).pct_change().fillna(0)
