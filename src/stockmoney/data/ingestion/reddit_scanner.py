"""Reddit content via public Atom/RSS feeds (no API key, no OAuth).

Reddit closed self-service Data API access in late 2025 (the "Responsible
Builder Policy" -- new OAuth apps now require a support-ticket approval that
can take weeks and is frequently rejected for non-commercial projects).
Reddit's per-subreddit RSS/Atom syndication feeds are a separate, older
mechanism that still works unauthenticated -- verified live 2026-07-10.

Trade-off: RSS entries don't include score/num_comments (those columns stay
NULL), only id/title/author/body/timestamp/link. Sufficient for text-based
sentiment/theme classification, which is all this project needs.

Rate limiting: unauthenticated requests hit 429s quickly if fired back-to-
back (empirically confirmed), so requests are paced with a delay between
subreddits.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import duckdb
import polars as pl

from stockmoney.data.ingestion.base import parse_feed_with_timeout, run_ingestion

DEFAULT_SUBREDDITS = ["wallstreetbets", "stocks", "investing", "semiconductors"]
DEFAULT_USER_AGENT = "stockmoney-scanner/1.0 (personal research project)"
REQUEST_DELAY_SECONDS = 8.0

_EMPTY_SCHEMA = {
    "post_id": pl.Utf8,
    "subreddit": pl.Utf8,
    "title": pl.Utf8,
    "body": pl.Utf8,
    "author": pl.Utf8,
    "posted_at": pl.Datetime(time_zone="UTC"),
    "score": pl.Int64,
    "num_comments": pl.Int64,
    "url": pl.Utf8,
    "source": pl.Utf8,
}


def _feed_url(subreddit: str) -> str:
    return f"https://www.reddit.com/r/{subreddit}/new/.rss"


def _clean_post_id(raw_id: str) -> str:
    # feedparser's id for Reddit entries looks like "t3_1usbddk"; keep as-is,
    # it's already a stable unique identifier.
    return raw_id


def fetch_reddit_posts(
    subreddits: list[str] | None = None,
    *,
    limit: int = 25,
    delay_seconds: float = REQUEST_DELAY_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    fetch_fn=None,
) -> pl.DataFrame:
    """Fetch the newest posts from each subreddit's public RSS feed. A single
    subreddit failing (private/banned/renamed/rate-limited) doesn't block the
    others. `fetch_fn(url, user_agent)` is injectable for testing."""
    subreddits = subreddits or DEFAULT_SUBREDDITS
    fetch_fn = fetch_fn or (lambda url, ua: parse_feed_with_timeout(url, agent=ua))

    rows = []
    errors: list[Exception] = []
    for i, sub in enumerate(subreddits):
        if i > 0:
            time.sleep(delay_seconds)
        try:
            parsed = fetch_fn(_feed_url(sub), user_agent)
            for entry in parsed.entries[:limit]:
                rows.append(
                    {
                        "post_id": _clean_post_id(entry.get("id", "")),
                        "subreddit": sub,
                        "title": entry.get("title"),
                        "body": entry.get("summary"),
                        "author": entry.get("author"),
                        "posted_at": _published_at(entry),
                        "score": None,
                        "num_comments": None,
                        "url": entry.get("link"),
                        "source": "reddit",
                    }
                )
        except Exception as exc:
            errors.append(exc)

    if errors and not rows:
        raise errors[0]
    if not rows:
        return pl.DataFrame(schema=_EMPTY_SCHEMA)
    return pl.DataFrame(rows, schema=_EMPTY_SCHEMA)


def _published_at(entry) -> datetime | None:
    parsed = entry.get("published_parsed")
    if not parsed:
        return None
    import calendar

    return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)


def ingest_reddit_posts(
    conn: duckdb.DuckDBPyConnection,
    subreddits: list[str] | None = None,
    *,
    limit: int = 25,
    delay_seconds: float = REQUEST_DELAY_SECONDS,
    fetch_fn=None,
) -> int:
    result = run_ingestion(
        conn,
        source="reddit",
        target_table="social_posts_raw",
        fetch_fn=lambda: fetch_reddit_posts(
            subreddits, limit=limit, delay_seconds=delay_seconds, fetch_fn=fetch_fn
        ),
    )
    return result.rows_written
