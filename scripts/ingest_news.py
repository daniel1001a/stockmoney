"""Live news ingestion for the 消息雷達.

Pulls fresh free-feed RSS headlines (general finance feeds + per-symbol Google
News) into the raw store, then classifies + upserts the watchlist-relevant ones
into the unified `news_items` feed the frontend reads. Safe to run on a schedule
(OpenClaw cron / APScheduler) -- idempotent per article, honest `available_at`
look-ahead stamp, no paid API. The nightly job calls the same
`refresh_news_items` helper.

Usage:
    .venv/bin/python scripts/ingest_news.py
    .venv/bin/python scripts/ingest_news.py --db data/stockmoney.duckdb
"""
from __future__ import annotations

import argparse
import json

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations
from stockmoney.data.news_synthesis import refresh_news_items


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    conn = get_connection(args.db)
    run_migrations(conn)
    summary = refresh_news_items(conn)
    summary["news_items_total_in_db"] = conn.execute("SELECT count(*) FROM news_items").fetchone()[0]
    conn.close()
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
