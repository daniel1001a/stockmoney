"""Shared helpers for Phase 1 sleeve research.

Research code only. Nothing here talks to a broker or places orders.

Timing model (same as src/stockmoney/desk/broker.py):
- A signal is computed from the CLOSE of day T using only data <= T.
- The resulting position change fills at the OPEN of day T+1, with slippage.
So the position held during day d is the signal from day d-1.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from stockmoney.desk.data import download
from stockmoney.models.metrics import bootstrap_mean_ci

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "research"
RESULTS = Path(__file__).resolve().parent / "results"


# --------------------------------------------------------------------------- data
def fetch(symbols: list[str], start: str = "1985-01-01", name: str | None = None,
          refresh: bool = False) -> pd.DataFrame:
    """Long OHLCV frame (auto-adjusted, via the desk's yfinance helper), cached
    as one parquet per request name under data/research/."""
    CACHE.mkdir(parents=True, exist_ok=True)
    name = name or "_".join(s.replace("^", "").replace("-", "") for s in symbols)
    path = CACHE / f"{name}.parquet"
    if path.exists() and not refresh:
        return pd.read_parquet(path)
    df = download(symbols, date.fromisoformat(start))
    df.to_parquet(path, index=False)
    return df


def wide(long: pd.DataFrame, field: str) -> pd.DataFrame:
    return long.pivot(index="date", columns="symbol", values=field).sort_index()


def cash_returns(index: pd.DatetimeIndex, irx: pd.Series, bil: pd.Series | None,
                 periods_per_year: int = 252) -> pd.Series:
    """Daily return of idle cash. BIL (T-bill ETF, net of its fee) where it
    exists; before that the 13-week T-bill yield (^IRX, percent) / 252, lagged
    one day so today's cash accrual uses yesterday's quoted yield."""
    irx_r = (irx.reindex(index).ffill().shift(1) / 100 / periods_per_year).fillna(0.0)
    out = irx_r.copy()
    if bil is not None:
        b = bil.reindex(index).pct_change()
        first = bil.dropna().index.min()
        use = (index > first) & b.notna()
        out[use] = b[use]
    return out.clip(lower=-0.01, upper=0.01)


# --------------------------------------------------------------------------- indicators
def rsi_wilder(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return rsi.where(dn != 0, 100.0)


def realized_vol(close: pd.Series, n: int = 20, ann: int = 252) -> pd.Series:
    return np.log(close).diff().rolling(n).std() * np.sqrt(ann)


def hysteresis(close: pd.Series, ref: pd.Series, band: float) -> pd.Series:
    """1 after close > ref*(1+band), 0 after close < ref*(1-band), else hold."""
    s = np.zeros(len(close))
    state = 0.0
    c, r = close.to_numpy(), ref.to_numpy()
    for i in range(len(c)):
        if np.isnan(r[i]) or np.isnan(c[i]):
            state = 0.0
        elif c[i] > r[i] * (1 + band):
            state = 1.0
        elif c[i] < r[i] * (1 - band):
            state = 0.0
        s[i] = state
    return pd.Series(s, index=close.index)


# --------------------------------------------------------------------------- simulation
@dataclass
class SimResult:
    ret: pd.Series          # daily sleeve return
    pos: pd.Series          # position held during each day (0/1)
    trades: pd.DataFrame    # one row per round trip


def simulate(signal: pd.Series, open_: pd.Series, close: pd.Series, cash_r: pd.Series,
             slip: float) -> SimResult:
    """Single instrument, all-in / all-out. ``signal[T]`` is decided at close T;
    the trade fills at open T+1 with ``slip`` per side. Idle cash earns cash_r."""
    idx = close.index
    sig = signal.reindex(idx).fillna(0.0).to_numpy()
    o, c, cr = open_.reindex(idx).to_numpy(), close.to_numpy(), cash_r.reindex(idx).fillna(0).to_numpy()
    n = len(idx)
    r = np.zeros(n)
    pos = np.zeros(n)
    trades = []
    entry_px = entry_d = None
    for d in range(1, n):
        held_prev, held = pos[d - 1], sig[d - 1]
        if held and np.isnan(o[d]):  # no bar: keep yesterday's state
            held = held_prev
        pos[d] = held
        if held and held_prev:
            r[d] = c[d] / c[d - 1] - 1
        elif held and not held_prev:
            entry_px, entry_d = o[d] * (1 + slip), idx[d]
            r[d] = c[d] / entry_px - 1
        elif held_prev and not held:
            px = o[d] * (1 - slip)
            r[d] = px / c[d - 1] - 1
            trades.append({"entry_date": entry_d, "exit_date": idx[d], "ret": px / entry_px - 1})
        else:
            r[d] = cr[d]
    if pos[-1] and entry_px is not None:  # open trade marked at last close
        trades.append({"entry_date": entry_d, "exit_date": pd.NaT, "ret": c[-1] / entry_px - 1})
    return SimResult(pd.Series(r, idx), pd.Series(pos, idx), pd.DataFrame(trades))


# --------------------------------------------------------------------------- statistics
def max_dd(eq: pd.Series) -> float:
    return float((eq / eq.cummax() - 1).min())


def stats(ret: pd.Series, ann: int = 252, pos: pd.Series | None = None,
          trades: pd.DataFrame | None = None) -> dict:
    ret = ret.dropna()
    eq = (1 + ret).cumprod()
    years = (ret.index[-1] - ret.index[0]).days / 365.25
    downside = np.sqrt((np.minimum(ret, 0) ** 2).mean())
    yearly = (1 + ret).groupby(ret.index.year).prod() - 1
    out = {
        "CAGR": eq.iloc[-1] ** (1 / years) - 1 if years > 0 else np.nan,
        "MaxDD": max_dd(eq),
        "Sharpe": ret.mean() / ret.std() * np.sqrt(ann) if ret.std() > 0 else np.nan,
        "Sortino": ret.mean() / downside * np.sqrt(ann) if downside > 0 else np.nan,
        "Vol": ret.std() * np.sqrt(ann),
        "WorstYr": yearly.min(),
        "WorstYrLbl": int(yearly.idxmin()),
    }
    if pos is not None:
        out["TimeIn"] = float(pos.reindex(ret.index).mean())
    if trades is not None:
        t = trades["ret"].to_numpy() if len(trades) else np.array([])
        out["Trades"] = len(t)
        out["Win%"] = float((t > 0).mean()) if len(t) else np.nan
        gains, losses = t[t > 0].sum(), -t[t <= 0].sum()
        out["PF"] = gains / losses if losses > 0 else np.nan
        if len(t):
            m, lo, hi = bootstrap_mean_ci(t)
            out["AvgTrade"], out["AvgTrade_lo"], out["AvgTrade_hi"] = m, lo, hi
    return out


def block_bootstrap_sharpe_diff(a: pd.Series, b: pd.Series, ann: int = 252, block: int = 21,
                                n_boot: int = 2000, seed: int = 0) -> tuple[float, float, float]:
    """Circular block bootstrap (paired, same dates) of Sharpe(a) - Sharpe(b).
    Blocks keep the volatility clustering that an iid bootstrap would destroy."""
    df = pd.concat([a, b], axis=1).dropna().to_numpy()
    n = len(df)
    rng = np.random.default_rng(seed)

    def sh(x):
        s = x.std()
        return x.mean() / s * np.sqrt(ann) if s > 0 else 0.0

    point = sh(df[:, 0]) - sh(df[:, 1])
    k = int(np.ceil(n / block))
    diffs = np.empty(n_boot)
    for i in range(n_boot):
        starts = rng.integers(0, n, k)
        ix = (starts[:, None] + np.arange(block)[None, :]).ravel()[:n] % n
        x = df[ix]
        diffs[i] = sh(x[:, 0]) - sh(x[:, 1])
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    return float(point), float(lo), float(hi)


def block_bootstrap_cagr_diff(a: pd.Series, b: pd.Series, ann: int = 252, block: int = 21,
                              n_boot: int = 2000, seed: int = 0) -> tuple[float, float, float]:
    """Same resampling, statistic = annualized log-growth(a) - log-growth(b)."""
    df = np.log1p(pd.concat([a, b], axis=1).dropna().to_numpy())
    n = len(df)
    rng = np.random.default_rng(seed)
    point = (df[:, 0].mean() - df[:, 1].mean()) * ann
    k = int(np.ceil(n / block))
    diffs = np.empty(n_boot)
    for i in range(n_boot):
        starts = rng.integers(0, n, k)
        ix = (starts[:, None] + np.arange(block)[None, :]).ravel()[:n] % n
        x = df[ix]
        diffs[i] = (x[:, 0].mean() - x[:, 1].mean()) * ann
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    return float(point), float(lo), float(hi)


# --------------------------------------------------------------------------- formatting
PCT = ["CAGR", "MaxDD", "Vol", "WorstYr", "TimeIn", "Win%", "AvgTrade", "AvgTrade_lo", "AvgTrade_hi"]


def fmt_table(rows: list[dict], cols: list[str]) -> str:
    def f(k, v):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return "-"
        if k in PCT:
            return f"{v * 100:.1f}%"
        if k in ("Sharpe", "Sortino", "PF"):
            return f"{v:.2f}"
        if isinstance(v, float):
            return f"{v:.2f}"
        return str(v)

    head = "| " + " | ".join(cols) + " |"
    sep = "|" + "|".join("---" for _ in cols) + "|"
    body = ["| " + " | ".join(f(c, r.get(c)) for c in cols) + " |" for r in rows]
    return "\n".join([head, sep, *body])


def period(ret: pd.Series, start: str | None, end: str | None) -> pd.Series:
    return ret.loc[start:end]


def df_md(df: pd.DataFrame, fmt: str = "{:.2f}") -> str:
    """Markdown table without the optional tabulate dependency."""
    cols = [str(c) for c in df.columns]
    lines = ["| | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for i, r in df.iterrows():
        lab = i.date() if isinstance(i, pd.Timestamp) else i
        lines.append(f"| {lab} | " + " | ".join(fmt.format(v) if pd.notna(v) else "-" for v in r) + " |")
    return "\n".join(lines)
