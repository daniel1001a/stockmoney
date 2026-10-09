"""Sleeve 2: short-term mean reversion (long only).

Pre-declared rules (fixed before any result was looked at):
- Filter       : close > 200d SMA (only buy dips inside an uptrend).
- Entry A `rsi2`: RSI(2) (Wilder) < 10 at close T  -> buy at open T+1.
- Entry B `down3`: three consecutive lower closes at close T -> buy at open T+1.
- Exit         : close > 5d SMA at close T -> sell at open T+1; or a time stop after
                 10 trading days in the trade. No price stop (the setup is a bet that
                 the dip reverts; a tight stop is known to cut its expectancy, and the
                 time stop caps how long a failed trade can sit).
- Costs        : 5 bps per side (desk broker default). 10 bps reported as sensitivity.
- Index version: SPY and QQQ separately, all-in when a trade is on, idle cash earns BIL/^IRX.
- Basket       : stocks in src/stockmoney/desk/universe.py (today's large caps -> strong
                 survivorship bias). Up to 10 concurrent positions, each sized at 10% of
                 sleeve equity at the signal close; if more candidates than free slots, take
                 the most oversold (lowest RSI2 for A, worst 3-day return for B).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import common as C

RSI_ENTRY, MAX_DAYS, SLOTS = 10.0, 10, 10


def _setups(close: pd.Series) -> dict[str, tuple[pd.Series, pd.Series]]:
    sma200, sma5 = close.rolling(200).mean(), close.rolling(5).mean()
    trend = close > sma200
    rsi2 = C.rsi_wilder(close, 2)
    down = close < close.shift(1)
    down3 = down & down.shift(1, fill_value=False) & down.shift(2, fill_value=False)
    return {
        "rsi2": ((trend & (rsi2 < RSI_ENTRY)).fillna(False), rsi2),
        "down3": ((trend & down3).fillna(False), close / close.shift(3) - 1),
    }


def index_signal(close: pd.Series, entry: pd.Series) -> pd.Series:
    """Path-dependent state machine over closes. signal[T] = want to hold after T."""
    sma5 = close.rolling(5).mean().to_numpy()
    c, e = close.to_numpy(), entry.to_numpy()
    sig = np.zeros(len(c))
    held_days = 0
    for t in range(len(c)):
        holding = t > 0 and sig[t - 1] == 1  # filled at open t (or earlier), held at close t
        if holding:
            held_days += 1
            exit_now = (c[t] > sma5[t]) or held_days >= MAX_DAYS
            sig[t] = 0.0 if exit_now else 1.0
        else:
            held_days = 0
            sig[t] = 1.0 if e[t] else 0.0
    return pd.Series(sig, index=close.index)


def run_index(core: pd.DataFrame, slip: float = 0.0005) -> dict:
    close, open_ = C.wide(core, "close"), C.wide(core, "open")
    out = {"ret": {}, "trades": {}, "pos": {}, "bench": {}}
    for sym in ("SPY", "QQQ"):
        c, o = close[sym].dropna(), open_[sym].dropna()
        cash = C.cash_returns(c.index, close["^IRX"], close["BIL"])
        for v, (entry, _) in _setups(c).items():
            res = C.simulate(index_signal(c, entry), o, c, cash, slip)
            k = f"{sym}_{v}"
            out["ret"][k], out["trades"][k], out["pos"][k] = res.ret, res.trades, res.pos
        out["bench"][f"{sym}_BH"] = c.pct_change().fillna(0)
    return out


def run_basket(stocks: pd.DataFrame, cash_r: pd.Series, variant: str, slip: float = 0.0005) -> C.SimResult:
    """Slot portfolio. Cash weight earns cash_r. Positions: shares-free, tracked as
    dollar value marked to close."""
    close, open_ = C.wide(stocks, "close"), C.wide(stocks, "open")
    idx = close.index
    entries, ranks, sma5 = {}, {}, close.rolling(5).mean()
    for s in close.columns:
        e, rk = _setups(close[s])[variant]
        entries[s], ranks[s] = e, rk
    E, R = pd.DataFrame(entries), pd.DataFrame(ranks)
    Cn, On, S5, En, Rn = close.to_numpy(), open_.to_numpy(), sma5.to_numpy(), E.to_numpy(), R.to_numpy()
    cols = list(close.columns)
    cr = cash_r.reindex(idx).fillna(0).to_numpy()

    cash, eq = 1.0, 1.0
    pos: dict[int, dict] = {}       # col -> {val, entry_px, entry_d, days}
    pend_buy: dict[int, float] = {}  # col -> dollars
    pend_sell: set[int] = set()
    rets, inmkt, trades = np.zeros(len(idx)), np.zeros(len(idx)), []
    prev_eq = 1.0
    for t in range(len(idx)):
        # ---- open: fill yesterday's orders
        for j in list(pend_sell):
            if j in pos and not np.isnan(On[t, j]):
                p = pos.pop(j)
                px = On[t, j] * (1 - slip)
                val = p["val"] * On[t, j] / p["last"]  # mark to open, then slip
                cash += val * (1 - slip)
                trades.append({"entry_date": p["d"], "exit_date": idx[t], "ret": px / p["px"] - 1})
                pend_sell.discard(j)
        for j, dollars in list(pend_buy.items()):
            if np.isnan(On[t, j]):
                continue
            dollars = min(dollars, cash)
            if dollars > 0:
                fill = On[t, j] * (1 + slip)
                cash -= dollars
                # value at close-ready reference: position worth dollars/(1+slip) at open price
                pos[j] = {"val": dollars / (1 + slip), "last": On[t, j], "px": fill, "d": idx[t], "days": 0}
            del pend_buy[j]
        pend_buy.clear()
        # ---- close: mark
        cash *= 1 + cr[t]
        for j, p in pos.items():
            if not np.isnan(Cn[t, j]):
                p["val"] *= Cn[t, j] / p["last"]
                p["last"] = Cn[t, j]
        eq = cash + sum(p["val"] for p in pos.values())
        rets[t] = eq / prev_eq - 1
        prev_eq = eq
        inmkt[t] = (eq - cash) / eq
        # ---- signals at close T
        for j, p in pos.items():
            p["days"] += 1
            if (Cn[t, j] > S5[t, j]) or p["days"] >= MAX_DAYS:
                pend_sell.add(j)
        free = SLOTS - (len(pos) - len(pend_sell))
        if free > 0:
            cand = [j for j in range(len(cols)) if En[t, j] and j not in pos]
            cand.sort(key=lambda j: Rn[t, j])
            for j in cand[:free]:
                pend_buy[j] = eq / SLOTS
    for j, p in pos.items():
        trades.append({"entry_date": p["d"], "exit_date": pd.NaT, "ret": p["last"] / p["px"] - 1})
    return C.SimResult(pd.Series(rets, idx), pd.Series(inmkt, idx), pd.DataFrame(trades))
