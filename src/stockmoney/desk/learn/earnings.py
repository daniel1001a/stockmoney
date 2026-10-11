"""Earnings dates for the universe: point-in-time store + daily refresh.

Source: Nasdaq's free, unauthenticated per-day earnings listing (the same
endpoint ``stockmoney.data.earnings_calendar`` uses; it is an unofficial
endpoint, so every failure is treated as "unknown", never as "no earnings").

Design rules:
- A day that failed to download changes nothing. Only a day that answered
  (even with zero rows) can add or retire dates, so an outage never makes a
  stock look safe.
- Every date remembers when we first saw it (``first_seen``). The shadow guard
  and the post-mortems only use dates that were already known on the decision
  day, so replays cannot look ahead.
- Read-only for trading: nothing here places or changes an order. The only
  consumers are the learning job (reports, post-mortem cards) and the shadow
  books.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

import requests

from stockmoney.desk.universe import STOCKS

URL = "https://api.nasdaq.com/api/calendar/earnings"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
ET = ZoneInfo("America/New_York")
SCAN_DAYS = 30
REQUEST_TIMEOUT_S = 6.0
BUDGET_S = 90.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS earnings (
  symbol TEXT, date TEXT, time_text TEXT, first_seen TEXT, last_seen TEXT, removed_at TEXT,
  PRIMARY KEY (symbol, date));
CREATE TABLE IF NOT EXISTS earnings_scan (
  day TEXT PRIMARY KEY, ok INTEGER, n_rows INTEGER, scanned_at TEXT);
"""


def ensure_schema(con) -> None:
    con.executescript(SCHEMA)


def norm(symbol: str) -> str:
    return symbol.strip().upper().replace("/", "-").replace(".", "-")


def fetch_day(day: date) -> list[dict] | None:
    """Rows reporting on ``day``; [] when nobody does; None on any failure."""
    try:
        r = requests.get(URL, params={"date": day.isoformat()}, headers=HEADERS, timeout=REQUEST_TIMEOUT_S)
        if r.status_code != 200:
            return None
        data = (r.json() or {}).get("data")
        if not isinstance(data, dict):
            return None
        rows = data.get("rows")
        if rows is None:
            return []
        return [x for x in rows if isinstance(x, dict) and x.get("symbol")]
    except Exception:
        return None


def _now(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def refresh(con, today: date, days: int = SCAN_DAYS, budget_s: float = BUDGET_S,
            fetch: Callable[[date], list[dict] | None] = fetch_day, now: datetime | None = None,
            pause_s: float = 0.15) -> dict:
    ensure_schema(con)
    watch = {norm(s) for s in STOCKS}
    stamp = _now(now)
    start = time.monotonic()
    scanned = failed = skipped = 0
    for i in range(days):
        if time.monotonic() - start > budget_s:
            skipped = days - i
            break
        d = today + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        rows = fetch(d)
        if rows is None:
            failed += 1
            con.execute("INSERT INTO earnings_scan VALUES (?,?,?,?) ON CONFLICT(day) DO UPDATE SET scanned_at=? "
                        "WHERE ok=0", (d.isoformat(), 0, 0, stamp, stamp))
            continue
        scanned += 1
        seen = {}
        for r in rows:
            s = norm(r["symbol"])
            if s in watch:
                seen[s] = str(r.get("time") or "")
        for s, t in seen.items():
            con.execute(
                "INSERT INTO earnings VALUES (?,?,?,?,?,NULL) ON CONFLICT(symbol, date) DO UPDATE SET "
                "last_seen=excluded.last_seen, time_text=excluded.time_text, removed_at=NULL",
                (s, d.isoformat(), t, stamp, stamp))
        # a date we had listed for this day that is no longer listed was moved or cancelled
        for (s,) in con.execute("SELECT symbol FROM earnings WHERE date=? AND removed_at IS NULL",
                                (d.isoformat(),)).fetchall():
            if s not in seen:
                con.execute("UPDATE earnings SET removed_at=? WHERE symbol=? AND date=?", (stamp, s, d.isoformat()))
        con.execute("INSERT INTO earnings_scan VALUES (?,?,?,?) ON CONFLICT(day) DO UPDATE SET ok=1, n_rows=?, "
                    "scanned_at=?", (d.isoformat(), 1, len(rows), stamp, len(rows), stamp))
        time.sleep(pause_s)
    return {"scanned": scanned, "failed": failed, "skipped": skipped}


def upcoming(con, symbols, today: date, within_days: int = 7) -> dict[str, str]:
    """{symbol: earliest earnings date in [today, today+within_days]} from the active rows."""
    ensure_schema(con)
    out: dict[str, str] = {}
    want = {norm(s) for s in symbols}
    end = (today + timedelta(days=within_days)).isoformat()
    for r in con.execute("SELECT symbol, date FROM earnings WHERE removed_at IS NULL AND date>=? AND date<=? "
                         "ORDER BY date", (today.isoformat(), end)).fetchall():
        if r["symbol"] in want and r["symbol"] not in out:
            out[r["symbol"]] = r["date"]
    return out


def covered(con, start: date, end: date) -> bool:
    """True when every weekday in [start, end] has a successful scan, i.e. an
    absence of earnings in that span is a fact rather than a gap."""
    ensure_schema(con)
    d = start
    while d <= end:
        if d.weekday() < 5:
            r = con.execute("SELECT ok FROM earnings_scan WHERE day=?", (d.isoformat(),)).fetchone()
            if not r or not r["ok"]:
                return False
        d += timedelta(days=1)
    return True


def in_span(con, symbol: str, start: date, end: date) -> list[str]:
    """Earnings dates of ``symbol`` inside [start, end] (active rows only)."""
    ensure_schema(con)
    return [r["date"] for r in con.execute(
        "SELECT date FROM earnings WHERE symbol=? AND date>=? AND date<=? AND removed_at IS NULL ORDER BY date",
        (norm(symbol), start.isoformat(), end.isoformat())).fetchall()]


def blocked_fn(con, window_days: int = 7) -> Callable[[str, "object"], bool]:
    """blocked(symbol, session) -> True when earnings fall within
    ``window_days`` calendar days from that session AND we already knew the
    date by the end of that session (point-in-time)."""
    ensure_schema(con)

    def blocked(symbol: str, session) -> bool:
        day = session.date() if hasattr(session, "date") else session
        cutoff = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=ET).astimezone(timezone.utc).isoformat()
        end = (day + timedelta(days=window_days)).isoformat()
        return con.execute(
            "SELECT 1 FROM earnings WHERE symbol=? AND date>=? AND date<=? AND first_seen<=? "
            "AND (removed_at IS NULL OR removed_at>?) LIMIT 1",
            (norm(symbol), day.isoformat(), end, cutoff, cutoff)).fetchone() is not None

    return blocked
