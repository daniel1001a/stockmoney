"""Regime allocator: which strategy gets how much, given today's situation.

Situation = 6 cells: market regime (risk_on / neutral / risk_off) x volatility
(SPY 20-day realized vol above / below its own trailing 252-day median).
Each cell's expected trade return per strategy is shrunk toward that
strategy's overall mean (prior weight PRIOR_N trades), then
    score = shrunk_mean - Z * standard_error
    weight = max(score, 0) / sum, each capped at MAX_W.
Everything for date T uses only trades that EXITED before T (walk_forward
enforces this and tests/desk/test_learn.py checks it).

The allocator is advisory: it logs recommended weights and a walk-forward
evaluation. Nothing reads it to size an order. It may only be wired into
sizing after the walk-forward beats equal weights with a confidence interval
that excludes zero (docs/desk/EDGE_PLAN.md 2.1).
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from stockmoney.desk import engine
from stockmoney.desk.broker import Account
from stockmoney.desk.data import Bars
from stockmoney.desk.indicators import Ind, compute
from stockmoney.desk.strategies import REGISTRY
from stockmoney.desk.universe import MARKET

PRIOR_N = 30
Z = 1.0
MAX_W = 0.5
STRATEGIES = tuple(REGISTRY)
CAPITAL = 25_000.0


def cells(ind: Ind, dates: pd.DatetimeIndex) -> pd.Series:
    """Cell label per date, from information at that date's close only."""
    from stockmoney.desk.indicators import market_state
    r = np.log(ind.close[MARKET]).diff()
    vol = r.rolling(20, min_periods=20).std() * np.sqrt(252)
    med = vol.rolling(252, min_periods=126).median()
    hi = vol > med
    out = {}
    for t in dates:
        reg = market_state(ind, t).regime
        v = "高波動" if bool(hi.get(t, False)) else "低波動"
        out[t] = f"{reg}|{v}"
    return pd.Series(out)


def sleeve_runs(bars: Bars, ind: Ind, start: str = "2015-06-01") -> dict[str, dict]:
    """Each strategy alone in its own account over history: trades + daily returns."""
    days = [t for t in bars.dates[bars.dates >= pd.Timestamp(start)] if not pd.isna(ind.sma200.at[t, MARKET])]
    ctxs = [engine.day_context(ind, t) for t in days]
    out = {}
    for name in STRATEGIES:
        strat = {name: REGISTRY[name]()}
        acct = Account(name, CAPITAL, CAPITAL)
        for ctx in ctxs:
            engine.morning(acct, bars, ctx.t)
            engine.evening(acct, strat, ctx, engine.scan(strat, ctx))
        eq = pd.DataFrame(acct.equity_curve).assign(date=lambda d: pd.to_datetime(d.date)).set_index("date")["equity"]
        out[name] = {"ret": eq.pct_change().fillna(0.0), "trades": pd.DataFrame(acct.trades)}
    return out


def shrunk(trades: pd.DataFrame, strategy: str, cell: str, cell_of_entry: pd.Series) -> tuple[float, float, int]:
    """(shrunk mean, standard error, n in cell) for one strategy and cell."""
    t = trades[trades.strategy == strategy] if "strategy" in trades else trades
    if t.empty:
        return 0.0, 1.0, 0
    all_m, all_sd = float(t["ret"].mean()), float(t["ret"].std(ddof=1)) if len(t) > 1 else 0.1
    c = t[cell_of_entry.reindex(pd.to_datetime(t["entry_date"])).to_numpy() == cell]
    n = len(c)
    m = float(c["ret"].mean()) if n else 0.0
    est = (n * m + PRIOR_N * all_m) / (n + PRIOR_N)
    se = max(all_sd, 1e-6) / np.sqrt(n + PRIOR_N)
    return est, se, n


def weights_from(trades_by_strategy: dict[str, pd.DataFrame], cell: str, cell_of_entry: pd.Series) -> tuple[dict, dict]:
    scores, detail = {}, {}
    for s, tr in trades_by_strategy.items():
        est, se, n = shrunk(tr, s, cell, cell_of_entry)
        scores[s] = est - Z * se
        detail[s] = {"est": est, "se": se, "n": n, "score": scores[s]}
    pos = {s: max(v, 0.0) for s, v in scores.items()}
    tot = sum(pos.values())
    if tot <= 0:
        return {s: 0.0 for s in scores}, detail
    w = {s: min(v / tot, MAX_W) for s, v in pos.items()}
    return w, detail


def recommend(runs: dict[str, dict], ind: Ind, today: pd.Timestamp) -> dict:
    """Weights for ``today`` from trades that exited strictly before it."""
    all_days = ind.close.index[ind.close.index <= today]
    cell_series = cells(ind, all_days)
    past = {s: r["trades"][pd.to_datetime(r["trades"]["exit_date"]) < today] for s, r in runs.items()}
    # entry cell = cell at the signal close, i.e. the session before the entry fill
    entry_cell = cell_series.shift(1)
    cur = cell_series.iloc[-1]
    w, detail = weights_from(past, cur, entry_cell)
    return {"date": str(today.date()), "cell": cur, "weights": w, "detail": detail}


def _stats(r: pd.Series) -> dict:
    r = r.dropna()
    eq = (1 + r).cumprod()
    yrs = len(r) / 252
    m = r.resample("ME").apply(lambda x: (1 + x).prod() - 1)
    return {"CAGR": eq.iloc[-1] ** (1 / yrs) - 1, "MaxDD": float((eq / eq.cummax() - 1).min()),
            "Sharpe": float(r.mean() / r.std() * np.sqrt(252)) if r.std() > 0 else 0.0,
            "pos_months": float((m > 0).mean()), "worst_month": float(m.min())}


def _block_boot_sharpe_diff(a: pd.Series, b: pd.Series, block: int = 21, n_boot: int = 2000, seed: int = 0):
    df = pd.concat([a, b], axis=1).dropna().to_numpy()
    n = len(df)
    rng = np.random.default_rng(seed)

    def sh(x):
        s = x.std()
        return x.mean() / s * np.sqrt(252) if s > 0 else 0.0

    k = int(np.ceil(n / block))
    d = np.empty(n_boot)
    for i in range(n_boot):
        ix = (rng.integers(0, n, k)[:, None] + np.arange(block)[None, :]).ravel()[:n] % n
        d[i] = sh(df[ix, 0]) - sh(df[ix, 1])
    return sh(df[:, 0]) - sh(df[:, 1]), *np.percentile(d, [2.5, 97.5])


def walk_forward(runs: dict[str, dict], ind: Ind, eval_start: str = "2018-01-01") -> dict:
    """Month by month: weights come only from trades exited before that month."""
    rets = pd.DataFrame({s: r["ret"] for s, r in runs.items()})
    dates = rets.index[rets.index >= pd.Timestamp(eval_start)]
    cell_series = cells(ind, ind.close.index)
    entry_cell = cell_series.shift(1)
    cell_prev = cell_series.shift(1)  # the cell known at yesterday's close drives today's weights
    port, eq_w = [], []
    month_cache: dict[str, dict] = {}
    for t in dates:
        mkey = f"{t.year}-{t.month:02d}"
        if mkey not in month_cache:
            first = pd.Timestamp(t.year, t.month, 1)
            past = {s: r["trades"][pd.to_datetime(r["trades"]["exit_date"]) < first] for s, r in runs.items()}
            month_cache[mkey] = {c: weights_from(past, c, entry_cell)[0] for c in sorted(set(cell_series.dropna()))}
        w = month_cache[mkey].get(cell_prev.get(t), {s: 0.0 for s in rets.columns})
        port.append(sum(w[s] * rets.at[t, s] for s in rets.columns))
        eq_w.append(float(rets.loc[t].mean()))
    port = pd.Series(port, index=dates)
    eqw = pd.Series(eq_w, index=dates)
    d, lo, hi = _block_boot_sharpe_diff(port, eqw)
    return {"allocator": _stats(port), "equal_weight": _stats(eqw),
            "sharpe_diff": d, "sharpe_diff_lo": lo, "sharpe_diff_hi": hi,
            "avg_weights": pd.DataFrame(
                [month_cache[f"{t.year}-{t.month:02d}"].get(cell_prev.get(t), {}) for t in dates]).mean().to_dict()}


def report_text(wf: dict, rec: dict | None = None) -> str:
    a, e = wf["allocator"], wf["equal_weight"]
    lines = ["## 情境分配器 walk-forward(每月只用該月以前已出場的交易)", "",
             "| | 年化 | 最大回撤 | Sharpe | 正報酬月份 | 最差單月 |", "|---|---|---|---|---|---|",
             f"| 分配器 | {a['CAGR']:.1%} | {a['MaxDD']:.1%} | {a['Sharpe']:.2f} | {a['pos_months']:.0%} | {a['worst_month']:.1%} |",
             f"| 三策略等權 | {e['CAGR']:.1%} | {e['MaxDD']:.1%} | {e['Sharpe']:.2f} | {e['pos_months']:.0%} | {e['worst_month']:.1%} |",
             "", f"Sharpe 差(分配器 - 等權):{wf['sharpe_diff']:+.2f},95% 區間 [{wf['sharpe_diff_lo']:+.2f}, {wf['sharpe_diff_hi']:+.2f}]"]
    verdict = ("信賴區間下緣大於 0:分配器可以進入影子帳戶階段。" if wf["sharpe_diff_lo"] > 0 else
               "信賴區間包含 0:沒有證據顯示分配器贏過等權,**不上線**(維持固定配比)。")
    lines += [verdict, "", "平均權重:" + ", ".join(f"{k} {v:.0%}" for k, v in wf["avg_weights"].items())]
    if rec:
        lines += ["", f"今日情境 `{rec['cell']}` 的建議權重(僅供參考):"
                  + ", ".join(f"{k} {v:.0%}" for k, v in rec["weights"].items())]
        for s, d in rec["detail"].items():
            lines.append(f"- {s}:收縮後平均 {d['est']:+.2%},標準誤 {d['se']:.2%},該格樣本 {d['n']} 筆")
    return "\n".join(lines)


def log_recommendation(con, rec: dict) -> None:
    from stockmoney.desk import store
    con.execute("INSERT OR REPLACE INTO alloc_log VALUES (?,?,?,?,?)",
                (rec["date"], rec["cell"], json.dumps(rec["weights"]), json.dumps(rec["detail"]), store.now()))
