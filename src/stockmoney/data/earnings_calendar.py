"""Best-effort next-earnings-date lookup for the cockpit cards (CLAUDE.md
Task D, part 2 -- "descriptive facts", never a direction call).

Lookup order, cheapest/most-trustworthy first:
1. `event_calendar` (DB-first -- currently empty in the live DB, but this is
   the intended source once an ingester populates it; see queries.events).
2. This module: a network fallback against Nasdaq's free, unauthenticated
   earnings-calendar API (`api.nasdaq.com/api/calendar/earnings?date=...`).
   That endpoint is a per-DAY listing, not a per-symbol lookup, so finding
   "next earnings date" for a watchlist means scanning forward day-by-day
   until every symbol has been seen or the scan budget runs out.

Best-effort by design: any network failure, non-200 response, timeout, or
unexpected JSON shape for a given day is swallowed and that day is simply
treated as "no data" -- this must NEVER raise into the cockpit request path,
and it must NEVER fabricate a date (unfound symbols get None, which the
frontend renders as "財報日未知"). A process-local cache with a TTL keeps a
homepage auto-refresh (every ~30s-2min, see refreshCadence.ts) from re-
scanning Nasdaq on every request -- only the first request after the cache
goes stale pays the scan cost.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import requests

NASDAQ_EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"
SCAN_WINDOW_DAYS = 45          # ~1 quarter's worth of forward lookup
REQUEST_TIMEOUT_S = 3.0
MAX_SCAN_SECONDS = 15.0        # hard wall-clock budget so a slow/degraded
                                # Nasdaq endpoint can never hang a request for
                                # long -- symbols not found within budget just
                                # come back None (honest "unknown"), not stale
_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

CACHE_TTL_S = 6 * 3600  # refetch at most every 6 hours

_cache: dict[str, date | None] = {}
_cache_symbols: frozenset[str] = frozenset()
_cache_at: float = 0.0


def _fetch_day_symbols(day: date) -> set[str]:
    """Symbols reporting earnings on `day`, or empty set on any failure --
    never raises."""
    try:
        resp = requests.get(
            NASDAQ_EARNINGS_URL,
            params={"date": day.isoformat()},
            headers=_HEADERS,
            timeout=REQUEST_TIMEOUT_S,
        )
        if resp.status_code != 200:
            return set()
        payload = resp.json() or {}
        rows = ((payload.get("data") or {}).get("rows")) or []
        return {r["symbol"] for r in rows if r.get("symbol")}
    except Exception:
        return set()


def fetch_next_earnings_dates(
    symbols: list[str], *, today: date | None = None
) -> dict[str, date | None]:
    """Scan forward from `today` (default: today) up to SCAN_WINDOW_DAYS or
    MAX_SCAN_SECONDS, whichever comes first, recording the first date each
    symbol appears on Nasdaq's earnings calendar. Symbols never seen in the
    window/budget map to None -- an honest "not found", not a guess."""
    today = today or date.today()
    remaining = set(symbols)
    out: dict[str, date | None] = {}
    start = time.monotonic()
    for i in range(SCAN_WINDOW_DAYS):
        if not remaining:
            break
        if (time.monotonic() - start) >= MAX_SCAN_SECONDS:
            break
        day = today + timedelta(days=i)
        found = _fetch_day_symbols(day)
        hit = remaining & found
        for s in hit:
            out[s] = day
        remaining -= hit
    for s in remaining:
        out[s] = None
    return out


def get_next_earnings_dates(symbols: list[str]) -> dict[str, date | None]:
    """Process-local cached wrapper around fetch_next_earnings_dates. Reuses
    the cached scan as long as it's fresh (CACHE_TTL_S) and covers the
    requested symbol set; otherwise re-scans. Any unexpected error degrades
    to "unknown for all requested symbols" rather than propagating -- this
    must never break the cockpit endpoint."""
    global _cache, _cache_symbols, _cache_at
    symset = frozenset(symbols)
    now = time.monotonic()
    if _cache_symbols and symset <= _cache_symbols and (now - _cache_at) < CACHE_TTL_S:
        return {s: _cache.get(s) for s in symbols}
    try:
        result = fetch_next_earnings_dates(sorted(symset))
    except Exception:
        result = {s: None for s in symbols}
    _cache = result
    _cache_symbols = symset
    _cache_at = now
    return result
