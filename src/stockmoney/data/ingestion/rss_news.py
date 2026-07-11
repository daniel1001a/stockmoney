from __future__ import annotations

import calendar
import hashlib
from datetime import datetime, timezone

import duckdb
import polars as pl

from stockmoney.data.ingestion.base import run_ingestion

# Verified working (2026-07-10) free finance RSS feeds. Reuters/Yahoo/FT were
# tried and rejected (403/401/429 on direct fetch). The "wsj" feed
# (feeds.a.dj.com/rss/RSSMarketsMain.xml) was dropped 2026-07-10: it returns
# HTTP 200 but serves a frozen snapshot dated 2025-01-27 -- confirmed stale by
# comparing two nightly ingestion runs a day apart that pulled the exact same
# 80 articles. Replaced with CNBC's public search-RSS endpoint, verified fresh
# (entries dated the day of the check).
DEFAULT_FEEDS = {
    "cnbc": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "marketwatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "investing": "https://www.investing.com/rss/news.rss",
    "seeking_alpha": "https://seekingalpha.com/market_currents.xml",
}

_EMPTY_SCHEMA = {
    "article_id": pl.Utf8,
    "source_name": pl.Utf8,
    "title": pl.Utf8,
    "summary": pl.Utf8,
    "url": pl.Utf8,
    "published_at": pl.Datetime(time_zone="UTC"),
    "source": pl.Utf8,
}


def _article_id(entry, source_name: str) -> str:
    guid = entry.get("id")
    if guid:
        return guid
    link = entry.get("link") or ""
    return f"{source_name}:{hashlib.sha256(link.encode('utf-8')).hexdigest()[:16]}"


def _published_at(entry) -> datetime | None:
    parsed = entry.get("published_parsed")
    if not parsed:
        return None
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)


def fetch_news(feeds: dict[str, str] | None = None) -> pl.DataFrame:
    """Fetch entries from each RSS feed and reshape into news_articles_raw's
    schema. A single feed failing to parse doesn't block the others -- for an
    unattended nightly run, one transient network hiccup on one feed
    shouldn't discard the other three. Only raises if every feed fails."""
    import feedparser

    feeds = feeds or DEFAULT_FEEDS
    rows = []
    errors: list[Exception] = []
    for source_name, url in feeds.items():
        try:
            parsed = feedparser.parse(url)
            for entry in parsed.entries:
                rows.append(
                    {
                        "article_id": _article_id(entry, source_name),
                        "source_name": source_name,
                        "title": entry.get("title"),
                        "summary": entry.get("summary") or entry.get("description"),
                        "url": entry.get("link"),
                        "published_at": _published_at(entry),
                        "source": "rss",
                    }
                )
        except Exception as exc:
            errors.append(exc)

    if errors and not rows:
        raise errors[0]
    if not rows:
        return pl.DataFrame(schema=_EMPTY_SCHEMA)
    return pl.DataFrame(rows, schema=_EMPTY_SCHEMA)


def ingest_news(conn: duckdb.DuckDBPyConnection, feeds: dict[str, str] | None = None) -> int:
    result = run_ingestion(
        conn,
        source="rss",
        target_table="news_articles_raw",
        fetch_fn=lambda: fetch_news(feeds),
    )
    return result.rows_written
