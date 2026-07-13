"""Per-symbol news via Google News RSS (free, no key).

The general finance feeds (rss_news.py) rarely carry more than a couple of
stories about any one watchlist name on a given day, which makes a per-ticker
news list thin. Google News' search-RSS endpoint returns recent, real,
per-query headlines with working source links -- exactly what the ticker
detail page's "相關消息" list and the radar's per-symbol filter need.

Each returned article already knows its symbol (the query is symbol-scoped), so
downstream classification tags it directly instead of guessing from the title.
Same discipline as the rest of the news layer: display-only, no model feature,
honest available_at stamped at upsert time.
"""
from __future__ import annotations

import calendar
import hashlib
from datetime import datetime, timezone

# Single-name watchlist members worth a dedicated query. ETFs (SOXL/SOXS) are
# skipped -- their "news" is really just semiconductor-sector news, already
# covered by the single names.
SYMBOL_QUERIES: dict[str, str] = {
    "NVDA": "Nvidia NVDA stock",
    "AVGO": "Broadcom AVGO stock",
    "AMD": "AMD Advanced Micro Devices stock",
    "TSM": "TSMC Taiwan Semiconductor stock",
    "AAPL": "Apple AAPL stock",
    "MSFT": "Microsoft MSFT stock",
    "GOOGL": "Alphabet Google GOOGL stock",
    "META": "Meta Platforms stock",
    "AMZN": "Amazon AMZN stock",
}

_GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def _published_at(entry) -> datetime | None:
    parsed = entry.get("published_parsed")
    if not parsed:
        return None
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)


def _article_id(entry, symbol: str) -> str:
    guid = entry.get("id") or entry.get("link") or entry.get("title") or ""
    return f"gnews:{symbol}:{hashlib.sha256(guid.encode('utf-8')).hexdigest()[:16]}"


def fetch_symbol_news(
    symbols: dict[str, str] | None = None, *, per_symbol: int = 8, window: str = "7d"
) -> list[dict]:
    """Fetch recent per-symbol headlines. One symbol's feed failing never blocks
    the others (unattended-run resilience, same as rss_news.fetch_news)."""
    import feedparser

    symbols = symbols or SYMBOL_QUERIES
    out: list[dict] = []
    for symbol, query in symbols.items():
        url = _GOOGLE_NEWS.format(q=(query + f" when:{window}").replace(" ", "+"))
        try:
            parsed = feedparser.parse(url)
        except Exception:
            continue
        for entry in parsed.entries[:per_symbol]:
            source = entry.get("source", {})
            out.append({
                "article_id": _article_id(entry, symbol),
                "source_name": (source.get("title") if source else "Google News"),
                "title": entry.get("title"),
                "summary": entry.get("summary") or entry.get("description"),
                "url": entry.get("link"),
                "published_at": _published_at(entry),
                "symbol_hint": symbol,
            })
    return out
