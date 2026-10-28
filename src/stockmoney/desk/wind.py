"""Wind layer (風向): one daily read of whether the tape helps or hurts new
longs, built only from the causal indicator frames (row t uses rows <= t).

Record-only (EDGE_PLAN H5): nothing here changes an order. It feeds the daily
``wind_daily`` log, the learn report, and the ``shadow:combined_wind`` /
``shadow:combined_nohead`` books, which are the evidence H5 is judged on.

Four components, each -1 / 0 / +1:
  trend    market_state regime (SPY vs 50/200d, breadth downgrade)
  breadth  share of universe stocks above their 50d (>=60% / <40%)
  vol      SPY 20d realised vol percentile over the last 252 sessions (<=50% / >=80%)
  momentum SPY 21d return (>= +1% / <= -2%)
score = sum. tailwind: score >= 2 and regime not risk_off. headwind: score <= -2
or regime risk_off. Thresholds are fixed a priori, not fitted.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from stockmoney.desk.indicators import Ind, market_state, sector_ranking
from stockmoney.desk.universe import MARKET

VOL_WINDOW = 20
VOL_LOOKBACK = 252
MAX_LEADERS = 4
STATE_LABEL = {"tailwind": "順風", "neutral": "中性", "headwind": "逆風"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS wind_daily (
  date TEXT PRIMARY KEY, state TEXT, score INTEGER, confidence TEXT, regime TEXT,
  breadth REAL, vol20 REAL, vol_pct REAL, spy_ret21 REAL, leaders_json TEXT,
  components_json TEXT, recorded_at TEXT);
"""


@dataclass
class Wind:
    date: str
    state: str  # tailwind | neutral | headwind
    score: int  # -4..4
    confidence: str  # low | medium | high
    regime: str
    breadth: float
    vol20: float  # annualised SPY 20d realised vol
    vol_pct: float  # percentile of vol20 within the last 252 sessions (0..1)
    spy_ret21: float
    leaders: list[str] = field(default_factory=list)  # sector ETFs beating SPY on 21d and 63d, above 50d
    components: dict[str, int] = field(default_factory=dict)

    def line(self) -> str:
        lead = ", ".join(self.leaders) or "無"
        return (f"{STATE_LABEL[self.state]}(分數 {self.score:+d},信心 {self.confidence});"
                f"大盤 {self.regime},站上50MA {self.breadth:.0%},SPY 20日波動 {self.vol20:.0%}"
                f"(一年百分位 {self.vol_pct:.0%}),SPY 21日 {self.spy_ret21:+.1%};領先板塊 {lead}")


def _vol_frames(ind: Ind) -> tuple[pd.Series, pd.Series]:
    """(vol20, its trailing percentile). Cached on the Ind instance."""
    cached = getattr(ind, "_wind_vol", None)
    if cached is not None:
        return cached
    lr = np.log(ind.close[MARKET]).diff()
    vol = lr.rolling(VOL_WINDOW, min_periods=VOL_WINDOW).std() * np.sqrt(252)
    # share of the trailing window (today included) at or below today's value
    pct = vol.rolling(VOL_LOOKBACK, min_periods=60).apply(lambda w: float((w <= w[-1]).mean()), raw=True)
    object.__setattr__(ind, "_wind_vol", (vol, pct))
    return vol, pct


def _sign(x: float, up: float, down: float) -> int:
    if pd.isna(x):
        return 0
    return 1 if x >= up else -1 if x <= down else 0


def leaders(ind: Ind, t: pd.Timestamp) -> list[str]:
    r = sector_ranking(ind, t)
    spy21 = ind.ret21.at[t, MARKET]
    r = r.assign(rel21=r["ret21"] - spy21)
    ok = r[(r["rel21"] > 0) & (r["rs63"] > 0) & r["above50"]]
    return list(ok.sort_values("rel21", ascending=False)["etf"].head(MAX_LEADERS))


def wind(ind: Ind, t: pd.Timestamp) -> Wind:
    memo = getattr(ind, "_wind_memo", None)
    if memo is None:
        memo = {}
        object.__setattr__(ind, "_wind_memo", memo)
    if t not in memo:
        memo[t] = _wind(ind, t)
    return memo[t]


def _wind(ind: Ind, t: pd.Timestamp) -> Wind:
    ms = market_state(ind, t)
    vol, pct = _vol_frames(ind)
    v, p = float(vol.at[t]), float(pct.at[t])
    spy21 = float(ind.ret21.at[t, MARKET])
    comp = {
        "trend": {"risk_on": 1, "neutral": 0, "risk_off": -1}[ms.regime],
        "breadth": _sign(ms.breadth, 0.60, 0.40 - 1e-12),
        "vol": 0 if pd.isna(p) else (1 if p <= 0.50 else -1 if p >= 0.80 else 0),
        "momentum": _sign(spy21, 0.01, -0.02),
    }
    score = sum(comp.values())
    if ms.regime == "risk_off" or score <= -2:
        state = "headwind"
    elif score >= 2:
        state = "tailwind"
    else:
        state = "neutral"
    conf = "high" if abs(score) >= 3 else "medium" if abs(score) == 2 else "low"
    return Wind(str(t.date()), state, int(score), conf, ms.regime, float(ms.breadth), v, p, spy21,
                leaders(ind, t), comp)


def series(ind: Ind, start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """Wind for every session with a valid 200d SMA in [start, end]."""
    d = ind.close.index
    d = d[(d >= pd.Timestamp(start or d[0])) & (d <= pd.Timestamp(end or d[-1]))]
    rows = [asdict(wind(ind, t)) for t in d if not pd.isna(ind.sma200.at[t, MARKET])]
    return pd.DataFrame(rows).set_index("date") if rows else pd.DataFrame()


# ---------------------------------------------------------------- persistence

def ensure_schema(con) -> None:
    con.executescript(SCHEMA)


def record(con, ind: Ind, t: pd.Timestamp, recorded_at: str) -> Wind:
    """Log the wind for session ``t``. The first write for a date sticks
    (INSERT OR IGNORE), so the log shows what was known when it was written."""
    ensure_schema(con)
    w = wind(ind, t)
    con.execute(
        "INSERT OR IGNORE INTO wind_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (w.date, w.state, w.score, w.confidence, w.regime, w.breadth, w.vol20, w.vol_pct, w.spy_ret21,
         json.dumps(w.leaders), json.dumps(w.components), recorded_at))
    return w


def state_before(con, day: str) -> str | None:
    """Logged wind state of the last session strictly before ``day`` (what an
    intraday decision on ``day`` could have known), or None."""
    row = con.execute("SELECT state FROM wind_daily WHERE date < ? ORDER BY date DESC LIMIT 1", (day,)).fetchone()
    return row[0] if row else None


BACKFILL_FROM = "2026-10-01"  # first options sleeve session was 2026-10-14; a little slack before it


def backfill(con, ind: Ind, through: pd.Timestamp, recorded_at: str) -> Wind:
    """Record every session from the last logged date (or BACKFILL_FROM) up to
    ``through``; returns the wind of ``through``. Rows written late carry the
    time they were written in ``recorded_at`` (EOD bars only, so the values are
    what that session's close showed)."""
    ensure_schema(con)
    last = con.execute("SELECT MAX(date) FROM wind_daily").fetchone()[0] or BACKFILL_FROM
    d = ind.close.index
    for t in d[(d >= pd.Timestamp(last)) & (d < through)]:
        if not pd.isna(ind.sma200.at[t, MARKET]):
            record(con, ind, t, recorded_at)
    return record(con, ind, through, recorded_at)


def options_by_wind(con) -> pd.DataFrame:
    """Closed options positions (live and shadow) with the wind state of the
    session before their trade day, i.e. what a 09:30 decision could know."""
    ensure_schema(con)
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='options_positions'").fetchone():
        return pd.DataFrame()
    rows = con.execute(
        "SELECT p.day, p.mode, p.source, p.exit_variant, p.direction, p.realized_usd - p.fees_usd AS pnl,"
        " (SELECT w.state FROM wind_daily w WHERE w.date < p.day ORDER BY w.date DESC LIMIT 1) AS wind"
        " FROM options_positions p WHERE p.exit_time IS NOT NULL").fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def report(con) -> str:
    """Wind log tail plus options outcomes per prior-day wind (H5 evidence;
    under 30 trades in a cell nothing is concluded)."""
    ensure_schema(con)
    tail = con.execute("SELECT date, state, score, confidence, leaders_json FROM wind_daily"
                       " ORDER BY date DESC LIMIT 5").fetchall()
    if not tail:
        return ""
    lines = ["## 風向層(H5,只記錄)", "", "| 日期 | 風向 | 分數 | 信心 | 領先板塊 |", "|---|---|---|---|---|"]
    for r in tail:
        lines.append(f"| {r[0]} | {STATE_LABEL.get(r[1], r[1])} | {r[2]:+d} | {r[3]} | {', '.join(json.loads(r[4])) or '無'} |")
    df = options_by_wind(con)
    if not df.empty and df["wind"].notna().any():
        g = df.dropna(subset=["wind"]).groupby(["source", "wind"])["pnl"]
        lines += ["", "期權已平倉單依前一日風向(每筆淨損益,美元):", "",
                  "| 來源 | 風向 | 筆數 | 平均 | 合計 |", "|---|---|---|---|---|"]
        for (src, w), s in g:
            lines.append(f"| {src} | {STATE_LABEL.get(w, w)} | {len(s)} | {s.mean():+.0f} | {s.sum():+.0f} |")
        lines.append("任一格少於 30 筆不下結論;同一天多筆高度相關,實際獨立樣本更少。")
    return "\n".join(lines)
