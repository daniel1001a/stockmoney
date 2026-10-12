"""Link checker: are the source URLs Grok and Muse send real?

EDGE_PLAN section 4 asks for a 90% real-link rate in the Grok trial. This
module samples URLs out of the stored bridge messages (read only), asks the
network whether they resolve, and records one row per URL.

Outcomes
  ok            the server answered 2xx/3xx
  dead          HTTP 404/410, or the host name does not resolve
  unverifiable  everything we cannot honestly call real or fake: 403/429 and
                other 4xx, 5xx, timeouts, connection errors, and domains known
                to block bots (a refusal there says nothing about the post).

Real-link rate = ok / (ok + dead). Unverifiable links are excluded from both
sides, so a login wall never counts as a fake link and never as a real one.

Safety: the URLs are untrusted text from a message. Only http/https is
fetched, and hosts that are IP literals, localhost or private-looking names
are recorded as unverifiable without any request. Network access goes through
an injectable ``fetch(url, timeout) -> int`` so tests run offline; it raises
``DnsFailure`` for a name that does not resolve and anything else for other
network trouble. This module only reads bridge_messages and writes link_checks.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
from typing import Callable, Iterable
from urllib.parse import urlsplit

from stockmoney.desk import store
from stockmoney.desk.learn import views

OK, DEAD, UNVERIFIABLE = "ok", "dead", "unverifiable"
THRESHOLD = 0.90
DEAD_CODES = frozenset({404, 410})
BOT_BLOCKING_DOMAINS = ("threads.net", "x.com", "twitter.com", "instagram.com", "facebook.com")
DEFAULT_SAMPLE = 20
TIMEOUT = 8.0


class DnsFailure(Exception):
    """The host name does not resolve (as opposed to a timeout or refusal)."""


Fetch = Callable[[str, float], int]


# ------------------------------------------------------------------ extraction

def _urls_of(type_: str, m: dict) -> list[str]:
    """Every source URL a stored message carries, by message type."""
    out: list[str] = []
    if type_ == "GROK_DAILY":
        for e in m.get("events", []):
            out += e.get("urls", [])
        for c in m.get("chains", []):
            out += c.get("urls", [])
        out += [c["post_url"] for c in m.get("calls", []) if c.get("post_url")]
    elif type_ == "CALLS":
        out += [c["post_url"] for c in m.get("calls", []) if c.get("post_url")]
    elif type_ == "SOCIAL_DAILY":
        for it in m.get("items", []):
            out += it.get("sample_urls", [])
    elif type_ in ("ALERT", "RESEARCH_REPLY", "IDEA"):
        out += m.get("source_urls", [])
    return [str(u) for u in out if isinstance(u, str)]


def collect(con) -> dict[str, tuple[str, str]]:
    """{url: (msg_id, agent)} over all stored messages; the earliest message
    that carried a URL owns it. Malformed rows are skipped."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='bridge_messages'").fetchone() is None:
        return {}
    found: dict[str, tuple[str, str]] = {}
    for r in con.execute("SELECT msg_id, type, parsed_json FROM bridge_messages ORDER BY received_at").fetchall():
        try:
            m = json.loads(r["parsed_json"])
            urls = _urls_of(r["type"], m)
        except (ValueError, TypeError, AttributeError):
            continue
        agent = views.agent_of_message(r["type"], r["msg_id"])
        for u in urls:
            found.setdefault(u, (r["msg_id"], agent))
    return found


# ------------------------------------------------------------------ classification

def domain_of(url: str) -> str:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _is_bot_blocking(domain: str) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in BOT_BLOCKING_DOMAINS)


def _is_unsafe_host(domain: str) -> bool:
    """Hosts we refuse to contact: empty, localhost / internal-looking names and
    IP literals (a message must not be able to aim this at the local network)."""
    if not domain or domain == "localhost" or "." not in domain:
        return True
    if domain.endswith((".local", ".localhost", ".internal", ".lan", ".home", ".corp")):
        return True
    try:
        ipaddress.ip_address(domain)
        return True
    except ValueError:
        return False


def classify(url: str, fetch: Fetch, timeout: float = TIMEOUT) -> tuple[str, int | None, str]:
    """(status, http_code, detail). Never raises."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return UNVERIFIABLE, None, "unparseable url"
    domain = domain_of(url)
    if parts.scheme not in ("http", "https"):
        return UNVERIFIABLE, None, "not http(s)"
    if _is_unsafe_host(domain):
        return UNVERIFIABLE, None, "host not fetched"
    if _is_bot_blocking(domain):
        return UNVERIFIABLE, None, "bot-blocking domain"
    try:
        code = int(fetch(url, timeout))
    except DnsFailure:
        return DEAD, None, "dns"
    except Exception as e:  # timeouts, resets, TLS errors: no verdict
        return UNVERIFIABLE, None, f"{type(e).__name__}"[:80]
    if 200 <= code < 400:
        return OK, code, ""
    if code in DEAD_CODES:
        return DEAD, code, ""
    return UNVERIFIABLE, code, ""


def default_fetch(url: str, timeout: float = TIMEOUT) -> int:
    """HEAD, falling back to a streamed GET when the server refuses HEAD.
    Raises DnsFailure for an unresolvable host; other errors propagate."""
    import requests

    headers = {"User-Agent": "Mozilla/5.0 (compatible; stockmoney-linkcheck/1.0)"}
    s = requests.Session()
    s.max_redirects = 5
    try:
        r = s.head(url, timeout=timeout, allow_redirects=True, headers=headers)
        if r.status_code in (403, 405, 501):
            r = s.get(url, timeout=timeout, allow_redirects=True, headers=headers, stream=True)
            r.close()
        return r.status_code
    except requests.exceptions.ConnectionError as e:
        # urllib3 wraps the resolver failure; walk the chain for it
        err: BaseException | None = e
        while err is not None:
            if isinstance(err, socket.gaierror) or type(err).__name__ == "NameResolutionError":
                raise DnsFailure(str(err)[:120]) from e
            err = err.__cause__ or err.__context__
        raise
    finally:
        s.close()


# ------------------------------------------------------------------ job step

def _pick(urls: Iterable[str], day: str, n: int) -> list[str]:
    """Deterministic pseudo-random sample: ordered by hash(day|url), so a rerun
    of the same day checks the same URLs."""
    return sorted(urls, key=lambda u: hashlib.sha1(f"{day}|{u}".encode()).hexdigest())[:n]


def step(con, day: str, n: int = DEFAULT_SAMPLE, fetch: Fetch | None = None) -> dict:
    """Check up to ``n`` not-yet-checked URLs and store the verdicts. A URL is
    checked once; a failed fetch is a stored 'unverifiable', not an exception."""
    fetch = fetch or default_fetch
    known = {r["url"] for r in con.execute("SELECT url FROM link_checks").fetchall()}
    pool = {u: v for u, v in collect(con).items() if u not in known}
    counts = {OK: 0, DEAD: 0, UNVERIFIABLE: 0}
    for url in _pick(pool, day, n):
        status, code, detail = classify(url, fetch)
        msg_id, agent = pool[url]
        con.execute("INSERT OR IGNORE INTO link_checks VALUES (?,?,?,?,?,?,?,?,?)",
                    (url, msg_id, agent, domain_of(url), status, code, detail, day, store.now()))
        counts[status] += 1
    return {"checked": sum(counts.values()), "pool": len(pool), **counts}


# ------------------------------------------------------------------ measurement

def real_link_rate(con, agent: str | None = None) -> dict:
    """{ok, dead, unverifiable, rate, meets_threshold}. ``rate`` is None until
    at least one link is verifiable; unverifiable links are excluded."""
    q, args = "SELECT status, COUNT(*) AS n FROM link_checks", ()
    if agent:
        q, args = q + " WHERE agent=?", (agent,)
    c = {OK: 0, DEAD: 0, UNVERIFIABLE: 0}
    for r in con.execute(q + " GROUP BY status", args).fetchall():
        c[r["status"]] = r["n"]
    ok, dead = c[OK], c[DEAD]
    rate = ok / (ok + dead) if ok + dead else None
    return {**c, "rate": rate, "meets_threshold": None if rate is None else rate >= THRESHOLD}


def report(con) -> str:
    lines = ["## 來源連結真實率", ""]
    shown = False
    for label, agent in (("全部", None), (views.MUSE, views.MUSE), (views.GROK, views.GROK)):
        r = real_link_rate(con, agent)
        total = r[OK] + r[DEAD] + r[UNVERIFIABLE]
        if not total:
            continue
        shown = True
        rate = "無法計算(沒有可驗證的連結)" if r["rate"] is None else \
            f"{r['rate']:.0%}(門檻 {THRESHOLD:.0%},{'達標' if r['meets_threshold'] else '未達標'})"
        lines.append(f"- {label}:真實 {r[OK]}、失效 {r[DEAD]}、無法驗證 {r[UNVERIFIABLE]};真實率 {rate}")
    if not shown:
        return "## 來源連結真實率\n\n尚無抽查紀錄。"
    lines += ["", "真實率 = 真實 / (真實 + 失效),無法驗證的不計入(X、Threads 等會擋機器人的網域一律歸為無法驗證)。"]
    return "\n".join(lines)
