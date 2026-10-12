"""Independence check for Muse and Grok flagging the same ticker.

Two agents that both cite the same article, or the same X account, are one
source repeated, not two confirmations. For every Grok chain and Grok event we
look for Muse flags (ALERT, RESEARCH_REPLY, IDEA) on the same ticker received
within WINDOW_DAYS, compare their sources, and store one labelled row per
pair in ``corroboration``:

  independent = 0  at least one normalized URL (domain + path) or one author
                   is shared: "not independent", worth ONE confirmation
  independent = 1  no overlap found

There was no corroboration counter anywhere in the desk before this module;
``counts()`` is the only one, and it never counts a non-independent pair as a
second confirmation. Nothing here feeds policy, risk or orders.

Authors come from the URL itself (the first path segment of an x.com,
twitter.com or threads.net post). Muse's ALERT carries no author field, so a
handle in the link is the only author evidence available.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from stockmoney.desk import store

WINDOW_DAYS = 3
MUSE_FLAG_TYPES = ("ALERT", "RESEARCH_REPLY", "IDEA")
_HOST_ALIAS = {"twitter.com": "x.com", "mobile.twitter.com": "x.com", "m.twitter.com": "x.com",
               "mobile.x.com": "x.com", "m.x.com": "x.com", "www.threads.com": "threads.net",
               "threads.com": "threads.net"}
_NOT_HANDLES = {"i", "home", "search", "hashtag", "intent", "share", "explore", "messages", "settings"}
_CASE_INSENSITIVE_PATH = {"x.com", "threads.net"}


def _parts(url: str):
    s = str(url).strip()
    if "://" not in s:
        s = "https://" + s
    p = urlsplit(s)
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    host = _HOST_ALIAS.get(host, host)
    return host, p.path.rstrip("/")


def normalize_url(url: str) -> str | None:
    """domain + path, lower-cased host, no scheme, query, fragment, www or
    trailing slash. None when the string has no host."""
    host, path = _parts(url)
    if not host:
        return None
    if host in _CASE_INSENSITIVE_PATH:
        path = path.lower()
    return host + path


def url_author(url: str) -> str | None:
    """Account handle in an x.com / twitter.com / threads.net post link."""
    host, path = _parts(url)
    if host not in _CASE_INSENSITIVE_PATH:
        return None
    seg = path.lstrip("/").split("/")[0].lstrip("@").lower()
    return seg if seg and seg not in _NOT_HANDLES else None


def compare(muse_urls, grok_urls, muse_authors=(), grok_authors=()) -> dict:
    """Overlap of two source sets. ``independent`` is False on any shared
    normalized URL or shared author."""
    mu = {n for n in map(normalize_url, muse_urls) if n}
    gu = {n for n in map(normalize_url, grok_urls) if n}
    ma = {a.lstrip("@").lower() for a in muse_authors if a} | {a for a in map(url_author, muse_urls) if a}
    ga = {a.lstrip("@").lower() for a in grok_authors if a} | {a for a in map(url_author, grok_urls) if a}
    shared_urls, shared_authors = sorted(mu & gu), sorted(ma & ga)
    return {"independent": not (shared_urls or shared_authors),
            "shared_urls": shared_urls, "shared_authors": shared_authors}


def _tick(t: str) -> str:
    return str(t).replace(".", "-").upper()


def _when(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _grok_items(con) -> list[dict]:
    """Chains (by impact ticker) and events from stored Grok data."""
    items = []
    for r in con.execute("SELECT chain_id, created_at, urls_json, impacts_json FROM chains "
                         "WHERE source='grok'").fetchall():
        try:
            urls, imps = json.loads(r["urls_json"]), json.loads(r["impacts_json"])
        except ValueError:
            continue
        at = _when(r["created_at"])
        for t in {_tick(i["ticker"]) for i in imps}:
            items.append({"ref": f"chain:{r['chain_id']}", "kind": "chain", "ticker": t, "urls": urls, "at": at})
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='bridge_messages'").fetchone():
        for r in con.execute("SELECT msg_id, received_at, parsed_json FROM bridge_messages "
                             "WHERE type='GROK_DAILY'").fetchall():
            try:
                events = json.loads(r["parsed_json"]).get("events", [])
            except (ValueError, AttributeError):
                continue
            for i, e in enumerate(events):
                try:
                    items.append({"ref": f"event:{r['msg_id']}:{i}", "kind": "event", "ticker": _tick(e["ticker"]),
                                  "urls": [str(u) for u in e["urls"]], "at": _when(r["received_at"])})
                except (KeyError, TypeError):
                    continue
    return items


def _muse_flags(con) -> list[dict]:
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='bridge_messages'").fetchone() is None:
        return []
    q = ",".join("?" for _ in MUSE_FLAG_TYPES)
    out = []
    for r in con.execute(f"SELECT msg_id, type, received_at, parsed_json FROM bridge_messages WHERE type IN ({q}) "
                         "AND msg_id NOT LIKE 'grok-%'", MUSE_FLAG_TYPES).fetchall():
        try:
            m = json.loads(r["parsed_json"])
            urls = [str(u) for u in m.get("source_urls", [])]
            ticker = _tick(m["ticker"])
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if urls:  # no source, nothing to compare, nothing to corroborate with
            out.append({"msg_id": r["msg_id"], "type": r["type"], "ticker": ticker, "urls": urls,
                        "at": _when(r["received_at"])})
    return out


def refresh(con) -> dict:
    """Label every (Grok item, Muse flag) pair on the same ticker within
    WINDOW_DAYS. Idempotent: a pair is written once."""
    muse = _muse_flags(con)
    by_ticker: dict[str, list[dict]] = {}
    for m in muse:
        by_ticker.setdefault(m["ticker"], []).append(m)
    added = 0
    win = timedelta(days=WINDOW_DAYS)
    for g in _grok_items(con):
        for m in by_ticker.get(g["ticker"], []):
            if g["at"] is None or m["at"] is None or abs(g["at"] - m["at"]) > win:
                continue
            c = compare(m["urls"], g["urls"])
            before = con.total_changes
            con.execute("INSERT OR IGNORE INTO corroboration VALUES (?,?,?,?,?,?,?,?,?)",
                        (g["ref"], g["kind"], g["ticker"], m["msg_id"], m["type"],
                         json.dumps(c["shared_urls"]), json.dumps(c["shared_authors"]),
                         int(c["independent"]), store.now()))
            added += con.total_changes > before
    return {"added": added}


def counts(con) -> dict:
    """Per (Grok item, ticker): 2 confirmations only when some Muse flag is
    independent of it, otherwise 1. ``not_independent`` pairs never add one."""
    rows = con.execute("SELECT ref, ticker, MAX(independent) AS ind, COUNT(*) AS pairs, "
                       "SUM(independent) AS ind_pairs FROM corroboration GROUP BY ref, ticker").fetchall()
    two = sum(1 for r in rows if r["ind"])
    return {"items": len(rows), "confirmed_independent": two, "single_source": len(rows) - two,
            "not_independent_pairs": sum(r["pairs"] - r["ind_pairs"] for r in rows),
            "confirmations": sum(2 if r["ind"] else 1 for r in rows)}


def report(con) -> str:
    c = counts(con)
    if not c["items"]:
        return ""
    return (f"Muse 與 Grok 同標的:{c['items']} 組,來源互相獨立 {c['confirmed_independent']} 組(算兩次確認),"
            f"共用連結或帳號 {c['single_source']} 組(標為 not independent,只算一次確認)。")
