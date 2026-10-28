"""Score GROK_DAILY ``market_risk`` calls against what SPY did next.

Store and score only. This reads stored GROK_DAILY messages and the price
cache and writes only ``grok_market_risk``. Nothing in veto, risk, bridge
policy or execution code reads it: a market_risk level never blocks an entry,
sizes a position or changes a parameter. It exists so the owner can see, over
time, whether Grok's level carries information.

Convention:
- base day = the last SPY session close Grok could have seen: the sent_at ET
  date when the mail was sent at or after 16:00 ET, else the session before it
- ret_N = SPY close N sessions after the base day / base close - 1
- dd_N = lowest SPY close in those N sessions / base close - 1 (0 if it never
  went below the base)
Fields stay NULL until enough bars exist: status 'pending' (< 5 sessions),
'partial' (5..19), 'done' (20).
"""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from stockmoney.desk import store
from stockmoney.desk.data import Bars
from stockmoney.desk.universe import MARKET

HORIZONS = (5, 20)
LEVELS = ("low", "elevated", "critical")
CLOSE_HOUR_ET = 16
ET = ZoneInfo("America/New_York")
SCHEMA = """
CREATE TABLE IF NOT EXISTS grok_market_risk (
  msg_id TEXT PRIMARY KEY, sent_at TEXT, received_at TEXT, source TEXT, level TEXT, summary TEXT,
  urls_json TEXT, base_day TEXT, ret_5 REAL, ret_20 REAL, dd_5 REAL, dd_20 REAL, status TEXT,
  updated_at TEXT);
"""
COLS = ("msg_id", "sent_at", "received_at", "source", "level", "summary", "urls_json", "base_day",
        "ret_5", "ret_20", "dd_5", "dd_20", "status", "updated_at")


def ensure_schema(con) -> None:
    con.executescript(SCHEMA)


def _num(x) -> float | None:
    return None if x is None or pd.isna(x) else float(x)


def base_day(dates: pd.DatetimeIndex, sent_at: str) -> pd.Timestamp | None:
    """Last session whose close was public when the mail was sent. sent_at is
    stored in UTC by the inbox, so it is converted to ET first."""
    sent = datetime.fromisoformat(sent_at)
    sent = (sent if sent.tzinfo else sent.replace(tzinfo=ET)).astimezone(ET)
    day = pd.Timestamp(sent.date())
    seen = dates[dates <= day] if sent.hour >= CLOSE_HOUR_ET else dates[dates < day]
    return seen[-1] if len(seen) else None


def score(bars: Bars, sent_at: str) -> dict:
    out = {"base_day": None, "status": "pending", **{f"{k}_{h}": None for k in ("ret", "dd") for h in HORIZONS}}
    if MARKET not in bars.close.columns:
        return out
    b = base_day(bars.dates, sent_at)
    if b is None:
        return out
    c0 = _num(bars.close.at[b, MARKET])
    if c0 is None:
        return out
    out["base_day"] = str(b.date())
    after = bars.dates[bars.dates > b]
    reached = 0
    for h in HORIZONS:
        if h > len(after):
            break
        closes = [_num(bars.close.at[t, MARKET]) for t in after[:h]]
        if any(c is None for c in closes):
            break
        out[f"ret_{h}"] = closes[-1] / c0 - 1
        out[f"dd_{h}"] = min(0.0, min(closes) / c0 - 1)
        reached = h
    out["status"] = "done" if reached == max(HORIZONS) else "partial" if reached else "pending"
    return out


def refresh(con, bars: Bars, through: pd.Timestamp) -> dict:
    """Upsert one row per GROK_DAILY that carries market_risk. Idempotent: an
    unchanged row is not rewritten. Returns {'added', 'updated', 'rows'}."""
    ensure_schema(con)
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='bridge_messages'").fetchone() is None:
        return {"added": 0, "updated": 0, "rows": 0}
    from stockmoney.desk.bridge.inbox import has_source_column, source_of
    bars = bars.upto(through)
    src = "source" if has_source_column(con) else "NULL"
    rows = con.execute(f"SELECT msg_id, sent_at, received_at, parsed_json, {src} AS source FROM bridge_messages "
                       "WHERE type='GROK_DAILY' ORDER BY received_at").fetchall()
    have = {r["msg_id"]: tuple(r[c] for c in COLS[:-1])
            for r in con.execute("SELECT * FROM grok_market_risk").fetchall()}
    added = updated = n = 0
    for r in rows:
        try:
            mr = (json.loads(r["parsed_json"]) or {}).get("market_risk")
        except (ValueError, AttributeError):
            continue
        if not isinstance(mr, dict) or mr.get("level") not in LEVELS:
            continue
        n += 1
        rec = {"msg_id": r["msg_id"], "sent_at": r["sent_at"], "received_at": r["received_at"],
               "source": source_of(r["source"], "GROK_DAILY"), "level": mr["level"],
               "summary": mr.get("summary") or "", "urls_json": json.dumps([str(u) for u in mr.get("source_urls") or []]),
               **score(bars, r["sent_at"])}
        vals = tuple(rec[c] for c in COLS[:-1])
        if have.get(r["msg_id"]) == vals:
            continue
        con.execute(f"INSERT OR REPLACE INTO grok_market_risk ({','.join(COLS)}) "
                    f"VALUES ({','.join('?' * len(COLS))})", (*vals, store.now()))
        if r["msg_id"] in have:
            updated += 1
        else:
            added += 1
    return {"added": added, "updated": updated, "rows": n}


def _pct(v) -> str:
    return "n/a" if v is None or pd.isna(v) else f"{v:+.2%}"


def report(con) -> str:
    ensure_schema(con)
    head = ["## Grok 大盤風險判讀的事後對照(只記錄,不影響下單)", ""]
    df = pd.read_sql_query("SELECT * FROM grok_market_risk", con)
    if df.empty:
        return "\n".join(head + ["尚無帶 market_risk 的 GROK_DAILY。"])
    lines = head + ["以 Grok 寄信當下已收盤的最後一天為基準,看 SPY 之後 5 / 20 個交易日的收盤報酬與期間最大跌幅。", ""]
    for lv in LEVELS:
        d = df[df.level == lv]
        if d.empty:
            continue
        lines.append(f"- {lv}:{len(d)} 則;5 日平均 {_pct(d.ret_5.mean())}(最大跌幅平均 {_pct(d.dd_5.mean())},"
                     f"{d.ret_5.count()} 則),20 日平均 {_pct(d.ret_20.mean())}(最大跌幅平均 {_pct(d.dd_20.mean())},"
                     f"{d.ret_20.count()} 則)。")
    return "\n".join(lines)
