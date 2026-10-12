"""Scoring of social-media author calls (Muse CALLS, Grok GROK_DAILY.calls).

Informational only: nothing here is read by sizing or order code.

Clock: the baseline is the first COMPLETE daily close after OUR ``received_at``
(bridge_messages), never the ``posted_at`` the sender wrote. Received before
16:05 ET on a session day -> that day's close; after 16:05 or on a non-session
day -> the next session's close (same 16:05 convention as job._through).

Scoring at 5/20/60 sessions after the baseline: excess = stock return - SPY
return. Bullish (long) hits when excess > 0, bearish (short) when excess < 0;
"exit" carries no market view and is skipped. Idempotent: one call per
(post_url, ticker), first receipt wins; one score per (call, horizon).

Per-author stats shrink toward the pooled mean of all authors with prior weight
allocator.PRIOR_N (same form as allocator.shrunk). Authors with fewer than
MIN_N scored calls are labelled "insufficient sample".
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from stockmoney.desk import store
from stockmoney.desk.data import Bars
from stockmoney.desk.learn.allocator import PRIOR_N
from stockmoney.desk.universe import MARKET

ET = ZoneInfo("America/New_York")
HORIZONS = (5, 20, 60)
MIN_N = 20
SIDE = {"long": 1, "short": -1}  # anything else (exit, unknown) is not scored
INSUFFICIENT = "insufficient sample"

SCHEMA = """
CREATE TABLE IF NOT EXISTS author_calls (
  call_id TEXT PRIMARY KEY, author TEXT, platform TEXT, ticker TEXT, direction TEXT,
  received_at TEXT, source_msg TEXT, post_url TEXT, posted_at TEXT, followers INTEGER,
  target_price REAL, horizon_text TEXT);
CREATE TABLE IF NOT EXISTS call_scores (
  call_id TEXT, horizon INTEGER, base_date TEXT, scored_date TEXT, excess_ret REAL, hit INTEGER,
  scored_at TEXT, PRIMARY KEY (call_id, horizon));
"""


def ensure_schema(con) -> None:
    con.executescript(SCHEMA)


def call_id(post_url: str, ticker: str) -> str:
    return hashlib.sha1(f"{str(post_url).strip()}|{ticker}".encode()).hexdigest()[:16]


def ingest(con) -> dict:
    """Copy calls out of stored CALLS / GROK_DAILY messages. Read-only on
    bridge_messages; ``received_at`` is our clock. Bad rows are skipped."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='bridge_messages'").fetchone() is None:
        return {"added": 0, "skipped": 0}
    added = skipped = 0
    rows = con.execute("SELECT msg_id, type, received_at, parsed_json FROM bridge_messages "
                       "WHERE type IN ('CALLS','GROK_DAILY') ORDER BY received_at, msg_id").fetchall()
    for r in rows:
        try:
            raw = json.loads(r["parsed_json"]).get("calls", [])
        except (ValueError, AttributeError):
            skipped += 1
            continue
        for c in raw:
            try:
                if c["direction"] not in SIDE:
                    continue  # exit etc.: no market view, not a skip
                before = con.total_changes
                con.execute(
                    "INSERT OR IGNORE INTO author_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (call_id(c["post_url"], c["ticker"]), c["author"], c["platform"], c["ticker"], c["direction"],
                     r["received_at"], r["msg_id"], str(c["post_url"]), str(c.get("posted_at", "")),
                     c.get("followers"), c.get("target_price"), c.get("horizon_text", "")))
                added += con.total_changes > before
            except (KeyError, TypeError):
                skipped += 1
    return {"added": added, "skipped": skipped}


def base_session(received_at: str, sessions: pd.DatetimeIndex) -> pd.Timestamp | None:
    """First complete close after receipt (see module docstring)."""
    t = datetime.fromisoformat(received_at)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    et = t.astimezone(ET)
    day = pd.Timestamp(et.date())
    before_close = (et.hour, et.minute) < (16, 5)
    ok = sessions[sessions >= day] if before_close else sessions[sessions > day]
    return ok[0] if len(ok) else None


def _sym(t: str) -> str:
    return t.replace(".", "-")


def score_due(con, bars: Bars, through: pd.Timestamp) -> int:
    """Score every call/horizon whose window has fully elapsed. Idempotent."""
    n = 0
    sessions = bars.dates[bars.dates <= through]
    spy = bars.close[MARKET]
    for r in con.execute("SELECT * FROM author_calls").fetchall():
        sym = _sym(r["ticker"])
        if sym not in bars.close.columns:
            continue
        base = base_session(r["received_at"], sessions)
        if base is None:
            continue
        b = sessions.get_loc(base)
        for h in HORIZONS:
            if b + h >= len(sessions):
                continue
            if con.execute("SELECT 1 FROM call_scores WHERE call_id=? AND horizon=?", (r["call_id"], h)).fetchone():
                continue
            t = sessions[b + h]
            px0, px1, s0, s1 = bars.close.at[base, sym], bars.close.at[t, sym], spy.at[base], spy.at[t]
            if any(pd.isna(x) for x in (px0, px1, s0, s1)):
                continue
            ex = float(px1 / px0 - 1) - float(s1 / s0 - 1)
            hit = int(ex * SIDE[r["direction"]] > 0)
            con.execute("INSERT INTO call_scores VALUES (?,?,?,?,?,?,?)",
                        (r["call_id"], h, str(base.date()), str(t.date()), ex, hit, store.now()))
            n += 1
    return n


def author_stats(con) -> pd.DataFrame:
    """One row per (author, platform, horizon). ``signed`` is the excess return
    of following the call (sign flipped for bearish calls)."""
    cols = ["author", "platform", "horizon", "n", "hit_rate", "mean_signed", "shrunk_hit", "shrunk_signed", "label"]
    rows = con.execute("SELECT c.author, c.platform, c.direction, s.horizon, s.excess_ret, s.hit "
                       "FROM call_scores s JOIN author_calls c USING (call_id)").fetchall()
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame([dict(r) for r in rows])
    df["signed"] = df["excess_ret"] * df["direction"].map(SIDE)
    out = []
    for h, hd in df.groupby("horizon"):
        pool_signed, pool_hit = float(hd["signed"].mean()), float(hd["hit"].mean())
        for (a, p), g in hd.groupby(["author", "platform"]):
            n = len(g)
            out.append({"author": a, "platform": p, "horizon": int(h), "n": n,
                        "hit_rate": float(g["hit"].mean()), "mean_signed": float(g["signed"].mean()),
                        "shrunk_hit": (n * g["hit"].mean() + PRIOR_N * pool_hit) / (n + PRIOR_N),
                        "shrunk_signed": (n * g["signed"].mean() + PRIOR_N * pool_signed) / (n + PRIOR_N),
                        "label": INSUFFICIENT if n < MIN_N else "ok"})
    return pd.DataFrame(out, columns=cols).sort_values(["horizon", "n"], ascending=[True, False]).reset_index(drop=True)


def report(con, top: int = 10) -> str:
    total = con.execute("SELECT COUNT(*) FROM author_calls").fetchone()[0]
    scored = con.execute("SELECT COUNT(DISTINCT call_id) FROM call_scores").fetchone()[0]
    lines = ["## 喊單評分(僅供參考,不影響任何交易)", "",
             f"已記錄方向性喊單 {total} 則,至少有一個時間窗已評分 {scored} 則。"
             f"基準價為我們收到訊息後的第一個完整收盤;報酬為相對 SPY 的超額報酬。"]
    st = author_stats(con)
    if st.empty:
        return "\n".join(lines)
    for h in HORIZONS:
        g = st[st.horizon == h].head(top)
        if g.empty:
            continue
        lines += ["", f"### {h} 個交易日", "", "| 帳號 | 平台 | 樣本 | 命中率 | 平均超額(跟單方向) | 收縮後平均 | 備註 |",
                  "|---|---|---|---|---|---|---|"]
        for r in g.itertuples():
            note = INSUFFICIENT if r.label == INSUFFICIENT else ""
            lines.append(f"| {r.author} | {r.platform} | {r.n} | {r.hit_rate:.0%} | {r.mean_signed:+.2%} "
                         f"| {r.shrunk_signed:+.2%} | {note} |")
    return "\n".join(lines)
