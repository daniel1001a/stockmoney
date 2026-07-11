"""Pure deterministic ingestion step of the overnight scan pipeline: pull
fresh RSS articles and Reddit posts (both via public RSS/Atom feeds, no API
key needed). No LLM involved here -- classification happens in a separate
cron step.

Usage:
    uv run python scripts/nightly_scan_ingest.py
"""
from __future__ import annotations

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations
from stockmoney.data.ingestion.reddit_scanner import ingest_reddit_posts
from stockmoney.data.ingestion.rss_news import ingest_news


def main(db_path: str = DEFAULT_DB_PATH) -> None:
    conn = get_connection(db_path)
    run_migrations(conn)

    try:
        n = ingest_news(conn)
        print(f"news_articles_raw: +{n} rows")
    except Exception as exc:
        print(f"news_articles_raw FAILED: {exc}")

    try:
        n = ingest_reddit_posts(conn)
        print(f"social_posts_raw: +{n} rows")
    except Exception as exc:
        print(f"social_posts_raw FAILED (Reddit credentials configured?): {exc}")

    conn.close()


if __name__ == "__main__":
    main()
