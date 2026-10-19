"""SEC EDGAR and earnings flags for the research veto (free sources only).

Writes ``research_flags`` rows that ``stockmoney.desk.veto`` reads. Sources:

  filings   8-K items (and late-filing notices NT 10-K / NT 10-Q) that mean trouble;
            ordinary 8-K / 10-Q / 10-K filings are stored as severity "info"
            summaries and never count toward a veto
  insider   Form 4 open-market sell cluster: at least CLUSTER_OWNERS distinct
            insiders sold (code S, not under a 10b5-1 plan) in the last
            INSIDER_DAYS days. A buy cluster (code P) is stored as "info" only
  earnings  a known report date inside EARNINGS_DAYS calendar days (from learn/earnings.py)

Rules:
- EDGAR requires a descriptive User-Agent with a contact. It is read from the
  SEC_USER_AGENT environment variable; with none set the EDGAR part is skipped
  (reported, not guessed). Requests are paced under the 10 per second limit.
- Any failure is "unknown": nothing is written for that symbol and nothing is
  inferred. A flag can only ever make the desk more cautious about a BUY.
- Severity lists below are judgment calls, not fitted values; they are meant to
  be rare. Review them before relying on the veto.
- No broker or risk imports: this layer only reads the web and writes its table.
"""
from __future__ import annotations

import json
import logging
import os
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from typing import Callable

import requests

from stockmoney.desk import veto
from stockmoney.desk.learn import earnings
from stockmoney.desk.universe import STOCKS

log = logging.getLogger(__name__)

UA_ENV = "SEC_USER_AGENT"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
TIMEOUT_S = 8.0
PAUSE_S = 0.12
BUDGET_S = 240.0
FILING_DAYS = 5          # only filings this recent (calendar days) can raise a flag
INSIDER_DAYS = 14
CLUSTER_OWNERS = 3
MAX_FORM4 = 15           # Form 4 documents read per symbol per run
EARNINGS_DAYS = 3

# 8-K items. critical: going-concern class events; elevated: material negative corporate events.
CRITICAL_8K = {"1.03": "bankruptcy", "3.01": "delisting notice", "4.01": "auditor change",
               "4.02": "financials non-reliance"}
ELEVATED_8K = {"1.02": "material agreement terminated", "2.04": "debt acceleration", "2.05": "exit/restructuring costs",
               "2.06": "material impairment", "5.02": "officer/director change"}
LATE_FORMS = ("NT 10-K", "NT 10-Q")
SUMMARY_FORMS = ("8-K", "10-Q", "10-K")


def _http_get(url: str, ua: str) -> str | None:
    try:
        r = requests.get(url, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}, timeout=TIMEOUT_S)
        return r.text if r.status_code == 200 else None
    except requests.RequestException:
        return None


def classify_8k(items: str) -> tuple[str, str]:
    """('critical' | 'elevated' | 'info', label) from an 8-K ``items`` string like '2.02,9.01'."""
    codes = [c.strip() for c in (items or "").split(",") if c.strip()]
    for table, sev in ((CRITICAL_8K, "critical"), (ELEVATED_8K, "elevated")):
        hit = [f"{c} {table[c]}" for c in codes if c in table]
        if hit:
            return sev, "8-K " + "; ".join(hit)
    return "info", f"8-K items {','.join(codes) or 'n/a'}"


def parse_form4(xml_text: str) -> list[dict]:
    """Non-derivative transactions of one Form 4: [{owner, code, shares, price, date, planned}].
    Unparseable input gives []."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    owners = [(o.findtext("reportingOwnerId/rptOwnerCik") or o.findtext("reportingOwnerId/rptOwnerName") or "?")
              for o in root.findall("reportingOwner")]
    owner = ",".join(sorted(owners)) or "?"
    planned = (root.findtext("aff10b5One") or "").strip().lower() in ("1", "true")
    out = []
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        try:
            out.append({"owner": owner, "code": (t.findtext("transactionCoding/transactionCode") or "").strip(),
                        "shares": float(t.findtext("transactionAmounts/transactionShares/value") or 0),
                        "price": float(t.findtext("transactionAmounts/transactionPricePerShare/value") or 0),
                        "date": (t.findtext("transactionDate/value") or "")[:10], "planned": planned})
        except ValueError:
            continue
    return out


def insider_cluster(txs: list[dict], min_owners: int = CLUSTER_OWNERS) -> dict:
    """Distinct insiders selling (code S, discretionary) and buying (code P)."""
    sells = {t["owner"] for t in txs if t["code"] == "S" and not t["planned"]}
    buys = {t["owner"] for t in txs if t["code"] == "P"}
    return {"sell_owners": len(sells), "buy_owners": len(buys),
            "sell": len(sells) >= min_owners, "buy": len(buys) >= min_owners,
            "sell_value": sum(t["shares"] * t["price"] for t in txs if t["code"] == "S" and not t["planned"])}


def _rows(recent: dict) -> list[dict]:
    keys = ("accessionNumber", "filingDate", "form", "items", "primaryDocument")
    cols = [recent.get(k) or [] for k in keys]
    return [dict(zip(keys, vals)) for vals in zip(*cols)]


def scan_symbol(sym: str, cik: int, fetch: Callable[[str], str | None], today: date,
                sleep: Callable[[float], None] = time.sleep) -> list[dict] | None:
    """Flag dicts for one symbol (source, kind, severity, headline, url, event_date);
    None when its filing list could not be read."""
    raw = fetch(SUBMISSIONS_URL.format(cik=cik))
    if raw is None:
        return None
    try:
        rows = _rows(json.loads(raw)["filings"]["recent"])
    except (ValueError, KeyError, TypeError):
        return None
    recent_cut = (today - timedelta(days=FILING_DAYS)).isoformat()
    insider_cut = (today - timedelta(days=INSIDER_DAYS)).isoformat()
    flags: list[dict] = []

    def url(r):
        return ARCHIVE_URL.format(cik=cik, acc=r["accessionNumber"].replace("-", ""), doc=r["primaryDocument"].split("/")[-1])

    for r in rows:
        if r["filingDate"] < recent_cut:
            continue
        if r["form"] == "8-K":
            sev, label = classify_8k(r["items"])
            flags.append({"source": "filings", "kind": "8-K", "severity": sev, "headline": label,
                          "url": url(r), "event_date": r["filingDate"]})
        elif r["form"] in LATE_FORMS:
            flags.append({"source": "filings", "kind": r["form"], "severity": "elevated",
                          "headline": f"{r['form']} late-filing notice", "url": url(r), "event_date": r["filingDate"]})
        elif r["form"] in SUMMARY_FORMS:
            flags.append({"source": "filings", "kind": r["form"], "severity": "info",
                          "headline": f"{r['form']} filed", "url": url(r), "event_date": r["filingDate"]})

    f4 = [r for r in rows if r["form"] == "4" and r["filingDate"] >= insider_cut]
    if len(f4) >= CLUSTER_OWNERS:
        txs: list[dict] = []
        for r in f4[:MAX_FORM4]:
            sleep(PAUSE_S)
            doc = fetch(ARCHIVE_URL.format(cik=cik, acc=r["accessionNumber"].replace("-", ""),
                                           doc=r["primaryDocument"].split("/")[-1]))
            if doc is not None:
                txs += parse_form4(doc)
        c = insider_cluster(txs)
        newest = max(r["filingDate"] for r in f4)
        if newest >= recent_cut:
            if c["sell"]:
                flags.append({"source": "insider", "kind": "sell_cluster", "severity": "elevated",
                              "headline": f"{c['sell_owners']} insiders sold ~${c['sell_value']:,.0f} (no 10b5-1 plan) in {INSIDER_DAYS}d",
                              "url": url(f4[0]), "event_date": newest})
            elif c["buy"]:
                flags.append({"source": "insider", "kind": "buy_cluster", "severity": "info",
                              "headline": f"{c['buy_owners']} insiders bought in {INSIDER_DAYS}d",
                              "url": url(f4[0]), "event_date": newest})
    return flags


def _store(con, sym: str, flags: list[dict], stamp: str) -> int:
    n = 0
    for f in flags:
        cur = con.execute("INSERT OR IGNORE INTO research_flags (symbol, source, kind, severity, headline, url, event_date, "
                          "collected_at) VALUES (?,?,?,?,?,?,?,?)",
                          (sym, f["source"], f["kind"], f["severity"], f["headline"], f.get("url") or "", f["event_date"], stamp))
        n += cur.rowcount
    return n


def refresh(con, today: date, fetch: Callable[[str], str | None] | None = None, now: datetime | None = None,
            symbols: list[str] | None = None, budget_s: float = BUDGET_S,
            sleep: Callable[[float], None] = time.sleep) -> dict:
    """Collect earnings and EDGAR flags into research_flags. Never raises on a
    network or parse problem; the summary says what was skipped."""
    veto.ensure_schema(con)
    now = now or datetime.now(timezone.utc)
    stamp = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    syms = [veto.norm(s) for s in (symbols or list(STOCKS))]
    out = {"earnings": 0, "filings": 0, "scanned": 0, "failed": 0, "skipped_edgar": None}

    soon = earnings.upcoming(con, syms, today, EARNINGS_DAYS)
    for s, d in soon.items():
        out["earnings"] += _store(con, veto.norm(s), [{"source": "earnings", "kind": "earnings_soon", "severity": "elevated",
                                                      "headline": f"earnings {d}", "url": "", "event_date": str(d)}], stamp)

    if fetch is None:
        ua = os.environ.get(UA_ENV, "").strip()
        if not ua:
            out["skipped_edgar"] = f"{UA_ENV} not set"
            return out
        def fetch(u, _ua=ua):
            return _http_get(u, _ua)
    raw = fetch(TICKERS_URL)
    try:
        ciks = {veto.norm(v["ticker"]): int(v["cik_str"]) for v in json.loads(raw).values()} if raw else {}
    except (ValueError, KeyError, TypeError, AttributeError):
        ciks = {}
    if not ciks:
        out["skipped_edgar"] = "ticker to CIK map unavailable"
        return out
    start = time.monotonic()
    for s in syms:
        if s not in ciks:
            continue
        if time.monotonic() - start > budget_s:
            out["skipped_edgar"] = f"time budget {budget_s:.0f}s used up"
            break
        sleep(PAUSE_S)
        flags = scan_symbol(s, ciks[s], fetch, today, sleep)
        if flags is None:
            out["failed"] += 1
            continue
        out["scanned"] += 1
        out["filings"] += _store(con, s, flags, stamp)
    return out


def report(con, days: int = 7) -> str:
    """Flags (elevated / critical) collected in the last ``days`` days, for the daily learn report."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = con.execute("SELECT symbol, source, severity, headline, event_date FROM research_flags "
                       "WHERE collected_at>=? AND severity IN ('elevated','critical') ORDER BY collected_at DESC, symbol", (since,)).fetchall()
    if not rows:
        return "## 研究旗標(SEC / 內部人 / 財報)\n\n最近 7 天沒有升高或嚴重的旗標。"
    return "\n".join(["## 研究旗標(SEC / 內部人 / 財報,只會擋新買單)", ""] +
                     [f"- {r['symbol']} [{r['source']}/{r['severity']}] {r['headline']} ({r['event_date']})" for r in rows])
