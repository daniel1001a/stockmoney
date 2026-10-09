"""Backtest every desk strategy (and the combined book) against SPY.

    .venv/bin/python scripts/desk_backtest.py --start 2015-06-01
"""
from __future__ import annotations

import argparse

import pandas as pd

from stockmoney.desk import backtest, data

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-06-01")
    ap.add_argument("--end", default=None)
    args = ap.parse_args()
    pd.set_option("display.width", 200)
    pd.set_option("display.float_format", lambda x: f"{x:,.3f}")
    bars = data.to_bars(data.load())
    res = backtest.run(bars, args.start, args.end)
    curves, trades = backtest.report(res, bars)
    print(curves)
    print(trades)
