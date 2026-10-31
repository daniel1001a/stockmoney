"""Does the desk's stock book have an edge? Read-only research, no broker.

Questions, each answered against a control that shares the same biases:
1. baselines   Is the book better than holding SPY / the same universe, with
               and without the same market-regime gate?
2. random      Do the entry signals beat random entries drawn from the same
               (survivor-biased) universe, with the same sizing, stops, exits
               and regime gate? This control cancels survivorship bias.
3. costs       How fast does the edge decay with slippage?
4. neighbors   Do neighboring parameters give similar results (robustness,
               not a search: nothing here is picked for deployment)?
5. regimes     Per-trade returns by strategy x market regime at entry.
6. years       Calendar-year returns vs SPY.

    .venv/bin/python research/desk_audit/audit.py all
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from stockmoney.desk import data, engine
from stockmoney.desk.backtest import curve_stats, trade_stats
from stockmoney.desk.broker import Account
from stockmoney.desk.indicators import compute
from stockmoney.desk.learn.controls import RandomEntry
from stockmoney.desk.strategies import REGISTRY, Signal, Strategy
from stockmoney.desk.universe import MARKET, STOCKS

START = "2015-06-01"
CAPITAL = 25_000
OUT = Path(__file__).resolve().parent / "results"


# ------------------------------------------------------------------ plumbing
_BARS = _CTX = None


def _load(drop: tuple[str, ...] = ()):
    bars = data.to_bars(data.load())
    if drop:
        keep = [c for c in bars.close.columns if c not in drop]
        bars = data.Bars(*(getattr(bars, f)[keep] for f in data.FIELDS))
    ind = compute(bars)
    days = [t for t in bars.dates[bars.dates >= START] if not pd.isna(ind.sma200.at[t, MARKET])]
    return bars, [engine.day_context(ind, t) for t in days]


def _init(drop=()):
    global _BARS, _CTX
    _BARS, _CTX = _load(drop)


def simulate(strats: dict[str, Strategy], bars=None, ctxs=None, *, slippage: float = 0.0005) -> dict:
    bars, ctxs = bars or _BARS, ctxs or _CTX
    acct = Account("x", CAPITAL, CAPITAL, slippage=slippage)
    for ctx in ctxs:
        engine.morning(acct, bars, ctx.t)
        sigs = [s for st in strats.values() for s in st.candidates(ctx)]
        engine.evening(acct, strats, ctx, sigs)
    eq = pd.DataFrame(acct.equity_curve).assign(date=lambda d: pd.to_datetime(d.date)).set_index("date")
    trades = pd.DataFrame(acct.trades)
    expo = (1 - eq["cash"] / eq["equity"]).mean()
    return {"curve": eq["equity"], "trades": trades, "stats": {**curve_stats(eq["equity"]),
            "exposure": expo, **trade_stats(trades)}}


def book(**overrides) -> dict[str, Strategy]:
    return {n: REGISTRY[n](**overrides.get(n, {})) for n in REGISTRY}


def _random_job(args):
    seed, lam, trend = args
    r = simulate({"random": RandomEntry(seed, lam, trend)})
    return {"seed": seed, "lam": lam, "trend": trend, **{k: r["stats"][k] for k in
            ("CAGR", "MaxDD", "Sharpe", "exposure", "n", "win%", "avg_ret", "PF")}}


def fmt(df: pd.DataFrame) -> str:
    def cell(x):
        return f"{x:.3f}" if isinstance(x, (float, np.floating)) else str(x)
    head = "| " + " | ".join([str(df.index.name or "")] + [str(c) for c in df.columns]) + " |"
    sep = "|" + "---|" * (len(df.columns) + 1)
    body = ["| " + " | ".join([str(i)] + [cell(v) for v in row]) + " |" for i, row in zip(df.index, df.to_numpy())]
    return "\n".join([head, sep, *body])


# ------------------------------------------------------------------ studies
def baselines() -> str:
    _init()
    real = simulate(book())
    idx = real["curve"].index
    close = _BARS.close
    reg = pd.Series({c.t: c.market.exposure_cap for c in _CTX}).reindex(idx)
    # gate decided at close T applies to day T+1 return (same timing as the book)
    spy_r = close[MARKET].pct_change().reindex(idx).fillna(0)
    stocks = [s for s in STOCKS if s in close]
    ew_r = close[stocks].pct_change().reindex(idx).mean(axis=1).fillna(0)
    gate = reg.shift(1).fillna(0)
    curves = {
        "desk combined": real["curve"],
        "SPY hold": (1 + spy_r).cumprod() * CAPITAL,
        "SPY x regime cap": (1 + spy_r * gate).cumprod() * CAPITAL,
        "universe EW hold": (1 + ew_r).cumprod() * CAPITAL,
        "universe EW x regime cap": (1 + ew_r * gate).cumprod() * CAPITAL,
        f"universe EW x {real['stats']['exposure']:.0%} const": (1 + ew_r * real["stats"]["exposure"]).cumprod() * CAPITAL,
    }
    rows = {k: curve_stats(v) for k, v in curves.items()}
    pd.to_pickle({"curves": curves, "trades": real["trades"], "stats": real["stats"]}, OUT / "real.pkl")
    yearly = pd.DataFrame({k: v.resample("YE").last().pct_change().fillna(v.resample("YE").last().iloc[0] / CAPITAL - 1)
                           for k, v in curves.items()})
    yearly.index = yearly.index.year
    return ("## Baselines\n\n" + fmt(pd.DataFrame(rows).T) + f"\n\nbook avg exposure {real['stats']['exposure']:.1%}, "
            f"trades {real['stats']['n']}\n\n## Calendar years\n\n" + fmt(yearly))


def random_control(n_seeds: int = 120) -> str:
    _init()
    real = simulate(book())["stats"]
    jobs = [(s, lam, trend) for trend in (True, False) for lam in (0.8,) for s in range(n_seeds)]
    with ProcessPoolExecutor(10, initializer=_init) as ex:
        rows = list(ex.map(_random_job, jobs))
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "random.csv", index=False)
    lines = [f"## Random-entry control ({n_seeds} seeds each)\n",
             f"real book: CAGR {real['CAGR']:.3f} Sharpe {real['Sharpe']:.3f} MaxDD {real['MaxDD']:.3f} "
             f"exposure {real['exposure']:.3f} trades {real['n']}\n"]
    for trend, g in df.groupby("trend"):
        q = g[["CAGR", "Sharpe", "MaxDD", "exposure", "n", "avg_ret"]].describe(percentiles=[.05, .5, .95]).T
        lines.append(f"### random, trend_filter={trend}\n\n" + fmt(q))
        lines.append(f"\nreal book percentile: CAGR {(g.CAGR < real['CAGR']).mean():.0%}, "
                     f"Sharpe {(g.Sharpe < real['Sharpe']).mean():.0%}, MaxDD {(g.MaxDD < real['MaxDD']).mean():.0%} "
                     "(share of random runs worse than the book)\n")
    return "\n".join(lines)


def costs() -> str:
    _init()
    rows = {}
    for bps in (5, 15, 30):
        rows[f"{bps} bps/side"] = simulate(book(), slippage=bps / 1e4)["stats"]
    return "## Slippage sensitivity\n\n" + fmt(pd.DataFrame(rows).T[["CAGR", "MaxDD", "Sharpe", "n", "avg_ret", "PF"]])


def neighbors() -> str:
    _init()
    grid = {
        "default": {},
        "trail 2.5": {n: {"trail_atr": 2.5} for n in ("breakout", "pullback")},
        "trail 3.5": {n: {"trail_atr": 3.5} for n in ("breakout", "pullback")},
        "bo stop 2.0": {"breakout": {"stop_atr": 2.0}},
        "bo stop 3.0": {"breakout": {"stop_atr": 3.0}},
        "bo vol 1.0": {"breakout": {"min_vol_ratio": 1.0}},
        "bo vol 1.6": {"breakout": {"min_vol_ratio": 1.6}},
        "pb rs 0.5": {"pullback": {"rs_quantile": 0.5}},
        "pb rs 0.85": {"pullback": {"rs_quantile": 0.85}},
        "rot top 3": {"rotation": {"top_n": 3, "keep_rank": 6}},
        "rot top 8": {"rotation": {"top_n": 8, "keep_rank": 12}},
    }
    rows = {k: simulate(book(**v))["stats"] for k, v in grid.items()}
    return "## Parameter neighbors (robustness only)\n\n" + fmt(pd.DataFrame(rows).T[["CAGR", "MaxDD", "Sharpe", "n", "avg_ret"]])


def survivorship() -> str:
    """Drop the 10 best full-period performers (hindsight winners) and late
    listings, then rerun the book and the EW benchmark."""
    bars = data.to_bars(data.load())
    stocks = [s for s in STOCKS if s in bars.close]
    tot = bars.close[stocks].loc[START:].apply(lambda s: s.dropna().iloc[-1] / s.dropna().iloc[0])
    top = tuple(tot.sort_values().index[-10:])
    b2, c2 = _load(top)
    r = simulate(book(), b2, c2)
    idx = r["curve"].index
    left = [s for s in stocks if s not in top]
    ew = (1 + b2.close[left].pct_change().reindex(idx).mean(axis=1).fillna(0)).cumprod() * CAPITAL
    rows = {"book without top10": r["stats"], "EW without top10": curve_stats(ew)}
    return (f"## Survivorship stress: drop {', '.join(top)}\n\n"
            + fmt(pd.DataFrame(rows).T[["CAGR", "MaxDD", "Sharpe"]]))


def regimes() -> str:
    _init()
    reg = pd.Series({str(c.t.date()): c.market.regime for c in _CTX})
    rows = []
    for name in REGISTRY:
        tr = simulate({name: REGISTRY[name]()})["trades"]
        # regime on the signal day = the session before entry
        days = list(reg.index)
        tr["regime"] = [reg.iloc[max(0, days.index(d) - 1)] if d in reg.index else None for d in tr.entry_date]
        for rg, g in tr.groupby("regime"):
            rows.append({"strategy": name, "regime": rg, **trade_stats(g)})
    return "## Per-trade results by strategy x regime at entry\n\n" + fmt(pd.DataFrame(rows).set_index(["strategy", "regime"]))


STUDIES = {"baselines": baselines, "random": random_control, "costs": costs,
           "neighbors": neighbors, "survivorship": survivorship, "regimes": regimes}

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    names = list(STUDIES) if sys.argv[1:] in ([], ["all"]) else sys.argv[1:]
    for n in names:
        text = STUDIES[n]()
        (OUT / f"{n}.md").write_text(text + "\n")
        print(text, "\n", flush=True)
