"""Backtest = replay engine.morning/evening over history. No fitting happens
here (rules are fixed a priori), so there is no train/test split to leak; the
leak risk is indicator look-ahead, covered by tests/desk/test_leak.py."""
from __future__ import annotations

import numpy as np
import pandas as pd

from stockmoney.desk import engine
from stockmoney.desk.broker import Account
from stockmoney.desk.data import Bars
from stockmoney.desk.indicators import compute
from stockmoney.desk.strategies import REGISTRY, Strategy
from stockmoney.desk.universe import MARKET, STOCKS
from stockmoney.models.metrics import bootstrap_mean_ci


def run(bars: Bars, start: str, end: str | None = None, capital: float = 25_000,
        strategy_sets: dict[str, list[str]] | None = None) -> dict:
    ind = compute(bars)
    strategy_sets = strategy_sets or {**{n: [n] for n in REGISTRY}, "combined": list(REGISTRY)}
    strats = {n: REGISTRY[n]() for n in REGISTRY}
    accts = {k: Account(k, capital, capital) for k in strategy_sets}
    dates = bars.dates[(bars.dates >= start) & (bars.dates <= (end or bars.dates[-1]))]
    regimes = []
    for t in dates:
        if pd.isna(ind.sma200.at[t, MARKET]):
            continue
        ctx = engine.day_context(ind, t)
        regimes.append({"date": t, "regime": ctx.market.regime})
        signals = engine.scan(strats, ctx)
        for k, acct in accts.items():
            engine.morning(acct, bars, t)
            sub = {n: strats[n] for n in strategy_sets[k]}
            engine.evening(acct, sub, ctx, [s for s in signals if s.strategy in sub])
    return {"accounts": accts, "regimes": pd.DataFrame(regimes).set_index("date")}


def curve_stats(eq: pd.Series) -> dict:
    eq = eq.dropna()
    r = eq.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    dd = eq / eq.cummax() - 1
    return {
        "CAGR": (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 if years > 0 else np.nan,
        "MaxDD": dd.min(),
        "Sharpe": r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else np.nan,
        "Vol": r.std() * np.sqrt(252),
        "Final": eq.iloc[-1],
    }


def trade_stats(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"n": 0}
    rets = trades["ret"].to_numpy()
    m, lo, hi = bootstrap_mean_ci(rets)
    wins, losses = trades.loc[trades.pnl > 0, "pnl"], trades.loc[trades.pnl <= 0, "pnl"]
    return {
        "n": len(trades),
        "win%": (trades.pnl > 0).mean(),
        "avg_ret": m,
        "ci_lo": lo,
        "ci_hi": hi,
        "avg_R": trades["r_multiple"].mean(),
        "PF": wins.sum() / -losses.sum() if len(losses) and losses.sum() < 0 else np.nan,
        "avg_hold": (pd.to_datetime(trades.exit_date) - pd.to_datetime(trades.entry_date)).dt.days.mean(),
    }


def benchmarks(bars: Bars, index: pd.DatetimeIndex, capital: float) -> dict[str, pd.Series]:
    spy = bars.close[MARKET].loc[index]
    stocks = [s for s in STOCKS if s in bars.close]
    rets = bars.close[stocks].loc[index].pct_change().mean(axis=1).fillna(0)
    return {
        "SPY 買進持有": spy / spy.iloc[0] * capital,
        "全清單等權持有": (1 + rets).cumprod() * capital,
    }


def report(result: dict, bars: Bars, capital: float = 25_000) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows, trade_rows = [], []
    curves = {}
    for k, acct in result["accounts"].items():
        eq = pd.DataFrame(acct.equity_curve).assign(date=lambda d: pd.to_datetime(d.date)).set_index("date")["equity"]
        curves[k] = eq
        rows.append({"name": k, **curve_stats(eq)})
        trade_rows.append({"name": k, **trade_stats(pd.DataFrame(acct.trades))})
    idx = next(iter(curves.values())).index
    for k, eq in benchmarks(bars, idx, capital).items():
        rows.append({"name": k, **curve_stats(eq)})
    return pd.DataFrame(rows).set_index("name"), pd.DataFrame(trade_rows).set_index("name")


def run_single(bars: Bars, strategy: Strategy, start: str = "2015-06-01", capital: float = 25_000) -> dict:
    """One strategy, one account: curve + trade stats. Used by the console's lab."""
    ind = compute(bars)
    acct = Account(strategy.name, capital, capital)
    for t in bars.dates[bars.dates >= start]:
        if pd.isna(ind.sma200.at[t, MARKET]):
            continue
        ctx = engine.day_context(ind, t)
        engine.morning(acct, bars, t)
        engine.evening(acct, {strategy.name: strategy}, ctx, strategy.candidates(ctx))
    eq = pd.DataFrame(acct.equity_curve).assign(date=lambda d: pd.to_datetime(d.date)).set_index("date")["equity"]
    return {**curve_stats(eq), **trade_stats(pd.DataFrame(acct.trades))}
