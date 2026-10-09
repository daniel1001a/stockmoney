"""Existing desk stock-trend book ('combined' = breakout + pullback + rotation),
replayed with the unchanged src/stockmoney/desk/backtest.run. Read-only use of src/.

Two data sources:
- `standard`: a copy of the desk's own cache (data/desk/ohlcv.parquet, 2014+, fetched
  2026-10-09), start 2015-06-01, the same setup OPERATING_PLAN.md reports.
- `long`: the same 92 symbols re-downloaded from 2003 into data/research/, start
  2005-01-03, to see 2008. Even stronger survivorship bias (names that did not exist
  are simply absent; names that died were never in the list) and sector ETFs XLRE/XLC
  only appear in 2015/2018. Treat it as a stress illustration, not an estimate.
"""
from __future__ import annotations

import pandas as pd

from stockmoney.desk import backtest, data
from stockmoney.desk.strategies import REGISTRY

import common as C


def run(which: str = "standard") -> dict:
    cache = C.CACHE / f"desk_book_{which}.parquet"
    tcache = C.CACHE / f"desk_book_{which}_trades.parquet"
    if cache.exists():
        df = pd.read_parquet(cache)
        return {"ret": df["equity"].pct_change().fillna(0), "equity": df["equity"],
                "n_pos": df["n_pos"], "trades": pd.read_parquet(tcache)}
    if which == "standard":
        long = data.load(C.ROOT / "data" / "desk")
        start = "2015-06-01"
    else:
        long = C.fetch([], name="desk_universe_long")
        start = "2005-01-03"
    bars = data.to_bars(long)
    res = backtest.run(bars, start, strategy_sets={"combined": list(REGISTRY)})
    acct = res["accounts"]["combined"]
    eq = pd.DataFrame(acct.equity_curve).assign(date=lambda d: pd.to_datetime(d.date)).set_index("date")
    eq[["equity", "n_pos"]].to_parquet(cache)
    trades = pd.DataFrame(acct.trades)
    trades["ret"] = trades["ret"].astype(float)
    trades.to_parquet(tcache)
    return run(which)
