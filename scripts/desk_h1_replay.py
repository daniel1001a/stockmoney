"""H1 historical replay: wider exits on the combined stock book, over the full
cache and two sub-windows.

    .venv/bin/python scripts/desk_h1_replay.py [--start 2016-01-01]

Arms: current rules; trail 3.5 ATR (what shadow:combined_t35 runs); the full
H1 of EDGE_PLAN 2.1(a) (trail 3.5 and pullback hold cap 40 -> 60); the hold cap
alone. Also prints the wash-out rate of the current rules: losing exits whose
symbol closed back above the entry price within 10 sessions.

Read-only: local OHLCV cache, writes nothing. Survivorship caveat of
desk/universe.py applies equally to every arm. Acceptance mirrors EDGE_PLAN
2.4: per-trade 95% lower bound above the baseline's and max drawdown not
deeper, in both sub-windows; one change at a time.
"""
from __future__ import annotations

import argparse

import pandas as pd

from stockmoney.desk import data, engine
from stockmoney.desk.backtest import curve_stats, trade_stats
from stockmoney.desk.broker import Account
from stockmoney.desk.indicators import compute
from stockmoney.desk.strategies import REGISTRY
from stockmoney.desk.universe import MARKET

ARMS = {
    "baseline": {},
    "trail35": {"breakout": {"trail_atr": 3.5}, "pullback": {"trail_atr": 3.5}},
    "trail35_hold60": {"breakout": {"trail_atr": 3.5}, "pullback": {"trail_atr": 3.5, "max_hold_days": 60}},
    "hold60": {"pullback": {"max_hold_days": 60}},
}
WASHOUT_DAYS = 10


def replay(bars, ind, start: str, end: str | None) -> tuple[dict, dict]:
    books = {k: {n: REGISTRY[n](**p.get(n, {})) for n in REGISTRY} for k, p in ARMS.items()}
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
    return out, {"baseline": accts["baseline"].trades}


def washout(trades: list[dict], bars) -> tuple[int, int]:
    """(losing exits, of which the close regained the entry within WASHOUT_DAYS sessions)."""
    losers = [t for t in trades if t["ret"] < 0]
    back = 0
    for t in losers:
        c = bars.close[t["symbol"]]
        after = c[c.index > pd.Timestamp(t["exit_date"])].head(WASHOUT_DAYS)
        back += bool((after > t["entry_price"]).any())
    return len(losers), back


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2016-01-01")
    args = ap.parse_args()
    bars = data.to_bars(data.load())
    ind = compute(bars)
    cols = ["n", "win%", "avg_ret", "ci_lo", "ci_hi", "PF", "CAGR", "MaxDD", "Sharpe"]
    for s, e in [(args.start, None), (args.start, "2020-12-31"), ("2021-01-01", None)]:
        res, tr = replay(bars, ind, s, e)
        print(f"\n== {s} .. {e or bars.dates[-1].date()} ==")
        print(pd.DataFrame(res).T[cols].to_string(float_format=lambda x: f"{x:.4f}"))
        n, back = washout(tr["baseline"], bars)
        print(f"baseline losing exits {n}, regained entry within {WASHOUT_DAYS} sessions: {back} ({back / max(n, 1):.0%})")


if __name__ == "__main__":
    main()
