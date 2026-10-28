"""H5 historical replay: the combined stock book with and without the wind
entry gate (desk/wind.py), over the full cache and two sub-windows.

    .venv/bin/python scripts/desk_wind_replay.py [--start 2016-01-01]

Read-only: uses the local OHLCV cache, writes nothing. Survivorship caveat of
desk/universe.py applies (today's large caps), equally to every arm.
Acceptance mirrors EDGE_PLAN 2.4: candidate's per-trade 95% lower bound above
the baseline's and max drawdown not deeper, in both sub-windows.
"""
from __future__ import annotations

import argparse

import pandas as pd

from stockmoney.desk import data, engine
from stockmoney.desk.backtest import curve_stats, trade_stats
from stockmoney.desk.broker import Account
from stockmoney.desk.indicators import compute
from stockmoney.desk.learn.shadow import WindGuard
from stockmoney.desk.strategies import REGISTRY
from stockmoney.desk.universe import MARKET

ARMS = {
    "baseline": None,
    "wind_tailwind_only": frozenset({"tailwind"}),
    "wind_no_headwind": frozenset({"tailwind", "neutral"}),
}


def replay(bars, ind, start: str, end: str | None) -> dict:
    books = {k: ({n: REGISTRY[n]() for n in REGISTRY} if a is None
                 else {n: WindGuard(REGISTRY[n](), a) for n in REGISTRY}) for k, a in ARMS.items()}
    accts = {k: Account(k, 25_000, 25_000) for k in ARMS}
    d = bars.dates[(bars.dates >= start) & (bars.dates <= (end or bars.dates[-1]))]
    for t in d:
        if pd.isna(ind.sma200.at[t, MARKET]):
            continue
        ctx = engine.day_context(ind, t)
        for k, acct in accts.items():
            engine.morning(acct, bars, t)
            engine.evening(acct, books[k], ctx, engine.scan(books[k], ctx))
    out = {}
    for k, acct in accts.items():
        eq = pd.DataFrame(acct.equity_curve).assign(date=lambda x: pd.to_datetime(x.date)).set_index("date")["equity"]
        out[k] = {**curve_stats(eq), **trade_stats(pd.DataFrame(acct.trades))}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2016-01-01")
    args = ap.parse_args()
    bars = data.to_bars(data.load())
    ind = compute(bars)
    windows = [(args.start, None), (args.start, "2020-12-31"), ("2021-01-01", None)]
    for s, e in windows:
        res = pd.DataFrame(replay(bars, ind, s, e)).T
        cols = ["n", "win%", "avg_ret", "ci_lo", "ci_hi", "PF", "CAGR", "MaxDD", "Sharpe"]
        print(f"\n== {s} .. {e or bars.dates[-1].date()} ==")
        print(res[cols].to_string(float_format=lambda x: f"{x:.4f}"))


if __name__ == "__main__":
    main()
