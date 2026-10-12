"""Shadow books: every candidate rule set runs forward on the same live bars,
with simulated fills, whether or not it has real money. They are the control
group (random entries, equal-weight universe under the same regime gate, SPY)
and the candidate group (variants waiting for evidence).

Accounts are persisted in the ``accounts`` table under ``shadow:`` names with
kind 'shadow' (the internal simulation accounts keep their own names). A step
is idempotent: each account remembers the last session it processed.
"""
from __future__ import annotations

import hashlib
import json
from typing import Callable

import numpy as np
import pandas as pd

from stockmoney.desk import engine, store
from stockmoney.desk.broker import Account
from stockmoney.desk.data import Bars
from stockmoney.desk.indicators import Ind, market_state
from stockmoney.desk.learn.controls import RandomEntry
from stockmoney.desk.strategies import REGISTRY, Strategy
from stockmoney.desk.universe import MARKET, STOCKS

CAPITAL = 25_000.0
START_KEY = "shadow_start"
RANDOM_SEEDS = 5


class EarningsGuard:
    """Wraps a strategy and drops entry signals whose earnings fall within the
    window (point-in-time). Exits, stops and sizing are the inner strategy's."""

    def __init__(self, inner: Strategy, blocked: Callable):
        self._inner, self._blocked = inner, blocked
        self.name, self.params = inner.name, inner.params

    def candidates(self, ctx):
        return [s for s in self._inner.candidates(ctx) if not self._blocked(s.symbol, ctx.t)]

    def __getattr__(self, item):
        return getattr(self._inner, item)


def _book(**params) -> Callable:
    return lambda day, con=None: {n: REGISTRY[n](**params.get(n, {})) for n in REGISTRY}


def _guarded_book(window_days: int) -> Callable:
    from stockmoney.desk.learn import earnings

    def make(day: str, con=None) -> dict[str, Strategy]:
        blocked = earnings.blocked_fn(con, window_days)
        return {n: EarningsGuard(REGISTRY[n](), blocked) for n in REGISTRY}
    return make


def _single(name: str) -> Callable:
    return lambda day, con=None: {name: REGISTRY[name]()}


def _random(k: int, trend: bool) -> Callable:
    def make(day: str, con=None) -> dict[str, Strategy]:
        seed = int(hashlib.sha1(f"{k}|{trend}|{day}".encode()).hexdigest()[:8], 16)
        return {"random": RandomEntry(seed, 0.8, trend)}
    return make


# name -> strategy factory (day -> strategies). Random controls are re-seeded
# per day from (index, day), so a rerun or a crash gives the identical path.
VARIANTS: dict[str, Callable] = {
    "shadow:combined": _book(),
    # no new entry within 7 calendar days of a known earnings date (EDGE_PLAN S6)
    "shadow:combined_earn7": _guarded_book(7),
    # H1 in docs/desk/EDGE_PLAN.md: wider trailing stop on the two stop-driven strategies
    "shadow:combined_t35": _book(breakout={"trail_atr": 3.5}, pullback={"trail_atr": 3.5}),
    "shadow:breakout": _single("breakout"),
    "shadow:pullback": _single("pullback"),
    "shadow:rotation": _single("rotation"),
}
for _k in range(RANDOM_SEEDS):
    VARIANTS[f"shadow:random_trend_{_k}"] = _random(_k, True)
    VARIANTS[f"shadow:random_any_{_k}"] = _random(_k, False)


def _sessions(bars: Bars, ind: Ind, start: str, through: pd.Timestamp) -> pd.DatetimeIndex:
    d = bars.dates
    ok = d[(d >= pd.Timestamp(start)) & (d <= through)]
    return pd.DatetimeIndex([t for t in ok if not pd.isna(ind.sma200.at[t, MARKET])])


def step(con, bars: Bars, ind: Ind, through: pd.Timestamp, start: str | None = None) -> dict:
    """Advance every shadow book to ``through``. Returns {name: sessions_run}."""
    saved_start = store.get_setting(con, START_KEY, "")
    start = saved_start or start or str(through.date())
    if not saved_start:
        store.set_setting(con, START_KEY, start)  # the first start ever chosen sticks
    sessions = _sessions(bars, ind, start, through)
    saved = store.load_accounts(con)
    ran: dict[str, int] = {}
    ctxs: dict[pd.Timestamp, object] = {}
    for name, make in VARIANTS.items():
        acct, _, last = saved.get(name, (Account(name, CAPITAL, CAPITAL), "shadow", ""))
        todo = [t for t in sessions if not last or str(t.date()) > last]
        n0 = len(acct.equity_curve)
        for t in todo:
            if t not in ctxs:
                ctxs[t] = engine.day_context(ind, t)
            ctx = ctxs[t]
            strats = make(str(t.date()), con)
            engine.morning(acct, bars, t)
            engine.evening(acct, strats, ctx, engine.scan(strats, ctx))
        if todo:
            store.save_account(con, acct, "shadow", str(todo[-1].date()))
            for e in acct.equity_curve[n0:]:
                con.execute("INSERT OR REPLACE INTO shadow_equity VALUES (?,?,?)", (name, e["date"], e["equity"]))
        ran[name] = len(todo)
    _benchmarks(con, bars, ind, sessions)
    return ran


def _benchmarks(con, bars: Bars, ind: Ind, sessions: pd.DatetimeIndex) -> None:
    """Stateless benchmark curves, recomputed from the start each time:
    SPY hold, and the equal-weight universe under the book's own regime gate
    (the gate decided at the close of T-1 sets T's exposure)."""
    if len(sessions) < 2:
        return
    stocks = [s for s in STOCKS if s in bars.close.columns]
    r_spy = bars.close[MARKET].pct_change().reindex(sessions).fillna(0.0)
    r_ew = bars.close[stocks].pct_change().reindex(sessions).mean(axis=1).fillna(0.0)
    cap = pd.Series({t: market_state(ind, t).exposure_cap for t in sessions}).shift(1).fillna(0.0)
    series = {
        "bench:spy": (1 + r_spy.iloc[1:]).cumprod() * CAPITAL,
        "bench:ew_gate": (1 + r_ew.iloc[1:] * cap.iloc[1:]).cumprod() * CAPITAL,
        "bench:ew_hold": (1 + r_ew.iloc[1:]).cumprod() * CAPITAL,
    }
    for name, s in series.items():
        s = pd.concat([pd.Series({sessions[0]: CAPITAL}), s])  # day 0 = starting capital, like the accounts
        con.executemany("INSERT OR REPLACE INTO shadow_equity VALUES (?,?,?)",
                        [(name, str(d.date()), float(v)) for d, v in s.items()])


def equity_table(con) -> pd.DataFrame:
    rows = con.execute("SELECT name, date, equity FROM shadow_equity").fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    return df.pivot(index="date", columns="name", values="equity").sort_index()


def report(con) -> str:
    eq = equity_table(con)
    if eq.empty or len(eq) < 2:
        return "## 影子帳戶\n\n資料不足(剛開始累積)。"
    first = eq.dropna(how="all").index[0]
    last = eq.index[-1]
    ret = (eq.iloc[-1] / eq.loc[first] - 1)
    dd = (eq / eq.cummax() - 1).min()
    rnd_t = [c for c in eq.columns if c.startswith("shadow:random_trend_")]
    rnd_a = [c for c in eq.columns if c.startswith("shadow:random_any_")]
    lines = [f"## 影子帳戶({first} 到 {last},{len(eq)} 個交易日)", "",
             "| 帳戶 | 累計報酬 | 最大回撤 |", "|---|---|---|"]
    for c in [c for c in eq.columns if c.startswith("bench:") or c in
              ("shadow:combined", "shadow:combined_t35", "shadow:combined_earn7", "shadow:breakout", "shadow:pullback", "shadow:rotation")]:
        lines.append(f"| {c} | {ret[c]:+.2%} | {dd[c]:.2%} |")
    for label, cols in (("隨機進場(多頭排列)", rnd_t), ("隨機進場(不限)", rnd_a)):
        if cols:
            lines.append(f"| {label} 中位數 / 範圍 | {ret[cols].median():+.2%} ({ret[cols].min():+.2%} 到 {ret[cols].max():+.2%}) "
                         f"| {dd[cols].median():.2%} |")
    if rnd_t and "shadow:combined" in ret:
        beat = float((ret[rnd_t] < ret["shadow:combined"]).mean())
        lines += ["", f"現行規則贏過 {beat:.0%} 的隨機組(樣本只有 {len(rnd_t)} 條路徑、期間很短,只當方向參考)。"]
    return "\n".join(lines)


def _live_equity(con) -> pd.Series:
    """The live (paper) book's system equity by date; empty when never recorded."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='equity_history'").fetchone() is None:
        return pd.Series(dtype=float)
    rows = con.execute("SELECT date, system_equity FROM equity_history WHERE system_equity > 0 ORDER BY date").fetchall()
    return pd.Series({r["date"]: float(r["system_equity"]) for r in rows}, dtype=float)


def period_returns(con, end: str, days: int = 7) -> dict:
    """Live (paper) book vs SPY vs the median of the random-entry (trend) paths
    over the same dates, read from the same stored curves the shadow report
    uses (``equity_history`` and ``shadow_equity``); nothing is re-simulated.

    All series are measured from one common start: the latest, over series, of
    that series' last observation on or before ``end - days`` (its first
    observation when it began later). A series with no usable pair of dates is
    None, never an estimate. Returns {"start", "end", "live", "spy", "random"}.
    """
    eq = equity_table(con)
    paths: dict[str, pd.Series] = {}
    live = _live_equity(con)
    if len(live):
        paths["live"] = live
    if not eq.empty:
        for c in eq.columns:
            if c == "bench:spy" or c.startswith("shadow:random_trend_"):
                paths[c] = eq[c].dropna()
    paths = {k: v[v.index <= end] for k, v in paths.items()}
    paths = {k: v for k, v in paths.items() if len(v)}
    out: dict = {"start": None, "end": end, "live": None, "spy": None, "random": None}
    if not paths:
        return out
    target = str((pd.Timestamp(end) - pd.Timedelta(days=days)).date())

    def base_date(s: pd.Series) -> str:
        before = s.index[s.index <= target]
        return before[-1] if len(before) else s.index[0]

    start = max(base_date(s) for s in paths.values())
    out["start"] = start

    def ret(s: pd.Series) -> float | None:
        base = s[s.index <= start]
        if not len(base) or base.iloc[-1] <= 0 or s.index[-1] <= start:
            return None
        return float(s.iloc[-1] / base.iloc[-1] - 1)

    if "live" in paths:
        out["live"] = ret(paths["live"])
    if "bench:spy" in paths:
        out["spy"] = ret(paths["bench:spy"])
    rr = [r for k, s in paths.items() if k.startswith("shadow:random_trend_") and (r := ret(s)) is not None]
    out["random"] = float(np.median(rr)) if rr else None
    return out


def weekly_line(con, end: str, days: int = 7) -> str:
    """One line for the Friday push: live vs SPY vs median random control."""
    p = period_returns(con, end, days)

    def f(v):
        return "no data" if v is None else f"{v:+.2%}"
    span = f"{p['start']} 到 {p['end']}" if p["start"] else f"近 {days} 天"
    return f"本期({span})實盤(紙上) {f(p['live'])} / SPY {f(p['spy'])} / 隨機對照中位數 {f(p['random'])}"
