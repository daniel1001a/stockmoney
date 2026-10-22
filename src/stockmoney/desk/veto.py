"""Cross-source research veto: a pure reader that can only say "do not buy".

A new entry is vetoed when at least ``min_sources`` INDEPENDENT sources flagged
the same symbol as elevated or critical within the last ``days`` trading days.
Sources (one vote each, however many items they contributed):

  muse      ALERT with severity elevated / critical, or a RESEARCH_REPLY that lists risk_flags
  grok      a GROK_DAILY event with severity elevated / critical and confirmed: true,
            from a grok_verified message (Pika's own address and token) and citing at
            least one URL. grok_legacy messages (Grok types that came over the
            Muse channel) never vote. A confirmed: false event (a single unverified
            account) never votes either; when counting it would have blocked a buy,
            check() sets VetoResult.unconfirmed (never a block) and the risk engine
            writes veto_unconfirmed only if the rest of the buy check allows the order.
  filings   research_flags rows (8-K items, late-filing notices) written by learn/filings.py
  insider   research_flags rows (Form 4 discretionary sell cluster)
  earnings  research_flags rows (earnings inside the next days). Beyond the four
            sources named in the brief; kept because "reports in 2 days" plus
            one other warning is exactly the case worth skipping.

Muse and Grok count as two votes only when ``corroboration.compare`` finds no
shared URL or author between some Muse item and some Grok item; otherwise they
are one source repeated and count once. Filings, insider and earnings come from
different systems and are independent by construction.

Timing uses OUR receipt / collection clock (``received_at`` / ``collected_at``),
never the sender's timestamps. The window is counted in weekdays; exchange
holidays are ignored, which only makes the window slightly longer.

Direction of authority: this module is read by the risk layer's BUY check only.
Sells and stops never call it. Missing tables, unreadable rows or any read error
mean "no veto" (logged); a veto is never inferred from absence of data.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np

from stockmoney.desk.learn import corroboration

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
DAYS = 4
MIN_SOURCES = 2
FLAGGED = ("elevated", "critical")
SOURCES = ("muse", "grok", "filings", "insider", "earnings")

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS research_flags (
         id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, source TEXT NOT NULL,
         kind TEXT NOT NULL, severity TEXT NOT NULL, headline TEXT, url TEXT,
         event_date TEXT, collected_at TEXT NOT NULL,
         UNIQUE (symbol, source, kind, event_date, url))""",
    """CREATE TABLE IF NOT EXISTS veto_log (
         id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, blocked_at TEXT NOT NULL,
         reason TEXT, sources_json TEXT)""",
    # buys that passed risk but WOULD have been blocked had unconfirmed Grok events voted; one
    # row per client_order_id, so a write-ahead replay does not add another. Whether the broker
    # took the order is in submitted (join on client_order_id), not here.
    """CREATE TABLE IF NOT EXISTS veto_unconfirmed (
         id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, seen_at TEXT NOT NULL,
         reason TEXT, sources_json TEXT, client_order_id TEXT NOT NULL UNIQUE)""",
)


def ensure_schema(con) -> None:
    for stmt in SCHEMA:
        con.execute(stmt)


@dataclass
class VetoResult:
    blocked: bool = False
    reason: str = ""
    sources: dict[str, list[dict]] = field(default_factory=dict)  # source -> its flagged items
    # set only when not blocked: the block that counting unconfirmed Grok events would have made
    unconfirmed: str = ""
    unconfirmed_sources: dict[str, list[dict]] = field(default_factory=dict)  # the what-if, unconfirmed events included


def norm(symbol: str) -> str:
    return corroboration._tick(symbol)


def window_start(now: datetime, days: int) -> datetime:
    """UTC instant ``days`` weekdays before ``now``'s ET date (start of that day)."""
    today = now.astimezone(ET).date()
    start = np.busday_offset(today.isoformat(), -days, roll="backward")
    return datetime.fromisoformat(str(start)).replace(tzinfo=ET).astimezone(timezone.utc)


def _has(con, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone() is not None


def _bridge_items(con, sym: str, since: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {"muse": [], "grok": [], "grok_unconfirmed": []}
    if not _has(con, "bridge_messages"):
        return out
    from stockmoney.desk.bridge.inbox import SOURCE_GROK_VERIFIED, has_source_column, source_of
    src = "source" if has_source_column(con) else "NULL"
    for r in con.execute(f"SELECT msg_id, type, received_at, parsed_json, {src} AS source FROM bridge_messages "
                         "WHERE type IN ('ALERT','RESEARCH_REPLY','GROK_DAILY') AND received_at>=?", (since,)).fetchall():
        try:
            m = json.loads(r["parsed_json"])
        except (ValueError, TypeError):
            continue
        if not isinstance(m, dict):
            continue
        if r["type"] == "GROK_DAILY":
            if source_of(r["source"], r["type"]) != SOURCE_GROK_VERIFIED:
                continue
            for i, e in enumerate(m.get("events") or []):
                try:
                    if norm(e["ticker"]) == sym and e.get("severity") in FLAGGED and e.get("urls"):
                        out["grok" if e.get("confirmed") is True else "grok_unconfirmed"].append({"ref": f"{r['msg_id']}:{i}", "type": "GROK_DAILY", "urls": [str(u) for u in e.get("urls", [])],
                                            "detail": f"{e['severity']}: {str(e.get('headline', ''))[:60]}",
                                            "at": r["received_at"]})
                except (KeyError, TypeError, AttributeError):
                    continue
            continue
        if str(r["msg_id"]).startswith("grok-"):
            continue
        try:
            if norm(m["ticker"]) != sym:
                continue
        except (KeyError, TypeError, AttributeError):
            continue
        if r["type"] == "ALERT" and m.get("severity") in FLAGGED:
            detail = f"{m['severity']}: {str(m.get('headline', ''))[:60]}"
        elif r["type"] == "RESEARCH_REPLY" and m.get("risk_flags"):
            detail = f"risk_flags: {'; '.join(str(f) for f in m['risk_flags'])[:60]}"
        else:
            continue
        out["muse"].append({"ref": r["msg_id"], "type": r["type"], "urls": [str(u) for u in m.get("source_urls", [])],
                            "detail": detail, "at": r["received_at"]})
    return out


def _filing_items(con, sym: str, since: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {"filings": [], "insider": [], "earnings": []}
    if not _has(con, "research_flags"):
        return out
    for r in con.execute("SELECT source, kind, severity, headline, url, collected_at FROM research_flags "
                         "WHERE symbol=? AND collected_at>=? AND severity IN ('elevated','critical')", (sym, since)).fetchall():
        if r["source"] in out:
            out[r["source"]].append({"ref": r["kind"], "type": r["kind"], "urls": [r["url"]] if r["url"] else [],
                                     "detail": f"{r['severity']}: {r['headline'] or r['kind']}"[:80], "at": r["collected_at"]})
    return out


def _collect_all(con, symbol: str, now: datetime | None, days: int) -> dict[str, list[dict]]:
    now = now or datetime.now(timezone.utc)
    since = window_start(now, days).isoformat()
    sym = norm(symbol)
    items = {**_bridge_items(con, sym, since), **_filing_items(con, sym, since)}
    return {k: v for k, v in items.items() if v}


def collect(con, symbol: str, now: datetime | None = None, days: int = DAYS) -> dict[str, list[dict]]:
    """Flagged items per voting source for ``symbol`` inside the window; empty sources omitted."""
    items = _collect_all(con, symbol, now, days)
    items.pop("grok_unconfirmed", None)
    return items


def _muse_grok_independent(muse: list[dict], grok: list[dict]) -> bool:
    """A Muse/Grok pair is independent only when both cite sources and share none: two unsourced
    items prove nothing about independence, so an empty URL list never counts as distinct."""
    return any(m["urls"] and g["urls"] and corroboration.compare(m["urls"], g["urls"])["independent"]
               for m in muse for g in grok)


def vote_count(items: dict[str, list[dict]]) -> int:
    """Independent sources among the flagged ones: Muse and Grok merge into one
    vote unless some Muse item and some Grok item share no URL or author."""
    n = len(items)
    if "muse" in items and "grok" in items and not _muse_grok_independent(items["muse"], items["grok"]):
        n -= 1
    return n


def _parts(items: dict[str, list[dict]]) -> str:
    return "; ".join(f"{s}({it[0]['detail']})" for s, it in items.items())


def check(con, symbol: str, now: datetime | None = None, days: int = DAYS, min_sources: int = MIN_SOURCES) -> VetoResult:
    """Never raises: any read problem is logged and means no veto."""
    try:
        items = _collect_all(con, symbol, now, days)
    except (sqlite3.Error, ValueError, TypeError) as e:
        log.warning("research veto read failed for %s: %s: %s", symbol, type(e).__name__, e)
        return VetoResult()
    unconfirmed = items.pop("grok_unconfirmed", [])
    n = vote_count(items)
    if n < min_sources:
        res = VetoResult(False, "", items)
        if unconfirmed:
            what_if = {**items, "grok": items.get("grok", []) + unconfirmed}
            m = vote_count(what_if)
            if m >= min_sources:
                res.unconfirmed = (f"unconfirmed: counting unconfirmed Grok events gives {m} sources on "
                                   f"{norm(symbol)} within {days} trading days, not blocked ({_parts(what_if)})")
                res.unconfirmed_sources = what_if
        return res
    merged = "muse+grok share a source and count once; " if ("muse" in items and "grok" in items and
                                                             not _muse_grok_independent(items["muse"], items["grok"])) else ""
    return VetoResult(True, f"research veto: {n} independent sources flagged {norm(symbol)} within {days} trading days "
                            f"({merged}{_parts(items)})", items)


def record(con, symbol: str, res: VetoResult) -> None:
    """Persist a block so the post-mortem and the daily report can show it. Does not commit."""
    try:
        ensure_schema(con)
        con.execute("INSERT INTO veto_log (symbol, blocked_at, reason, sources_json) VALUES (?,?,?,?)",
                    (norm(symbol), datetime.now(timezone.utc).isoformat(), res.reason, json.dumps(res.sources, default=str)))
    except sqlite3.Error as e:
        log.warning("veto_log write failed for %s: %s", symbol, e)


def record_unconfirmed(con, symbol: str, res: VetoResult, client_order_id: str) -> None:
    """Persist a would-have-blocked buy for the learning layer. Does not commit; never blocks.
    Call only once the whole buy check allowed the order. Every row is a buy risk let through;
    it may still not have reached the broker (see submitted). Idempotent per client_order_id."""
    try:
        ensure_schema(con)
        con.execute("INSERT OR IGNORE INTO veto_unconfirmed (symbol, seen_at, reason, sources_json, "
                    "client_order_id) VALUES (?,?,?,?,?)",
                    (norm(symbol), datetime.now(timezone.utc).isoformat(), res.unconfirmed,
                     json.dumps(res.unconfirmed_sources, default=str), client_order_id))
    except sqlite3.Error as e:
        log.warning("veto_unconfirmed write failed for %s: %s", symbol, e)


def blocks_between(con, symbol: str, start: str, end: str) -> list[str]:
    """Reasons of vetoes recorded for ``symbol`` with blocked_at in [start, end] (ISO strings)."""
    if not _has(con, "veto_log"):
        return []
    return [r["reason"] for r in con.execute("SELECT reason FROM veto_log WHERE symbol=? AND blocked_at>=? AND blocked_at<=? "
                                              "ORDER BY id", (norm(symbol), start, end)).fetchall()]


def flags_between(con, symbol: str, start: str, end: str) -> list[str]:
    """Elevated / critical research_flags rows for ``symbol`` collected in [start, end] (ISO strings),
    as 'source/severity: headline'. For the post-mortem card; empty when the table is missing."""
    if not _has(con, "research_flags"):
        return []
    return [f"{r['source']}/{r['severity']}: {r['headline'] or r['kind']}" for r in con.execute(
        "SELECT source, severity, headline, kind FROM research_flags WHERE symbol=? AND collected_at>=? AND collected_at<=? "
        "AND severity IN ('elevated','critical') ORDER BY id", (norm(symbol), start, end)).fetchall()]


def report(con, since: str) -> str:
    if not _has(con, "veto_log"):
        return ""
    rows = con.execute("SELECT symbol, blocked_at, reason FROM veto_log WHERE blocked_at>=? ORDER BY id", (since,)).fetchall()
    if not rows:
        return ""
    return "\n".join(["## 研究否決(買單被擋)", ""] + [f"- {r['symbol']} {r['blocked_at'][:16]}:{r['reason']}" for r in rows])
