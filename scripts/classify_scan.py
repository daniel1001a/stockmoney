"""Classify freshly-scraped Reddit posts / news articles into per-ticker
sentiment or new-ticker/theme candidates, without going through OpenClaw
(HANDOFF.md: the OpenClaw scanner agent's exec permission is a dead end --
see ~/.claude/plans/frontend-catalyst-rebuild.md). Calls the Anthropic API
directly and writes through `stockmoney.data.scan_classify`, which routes
every result through the same validated write boundary the old skill used
(`stockmoney.data.classification`).

Requires ANTHROPIC_API_KEY in the environment (see .env.example).

Usage:
    uv run python scripts/classify_scan.py [--hours 6] [--limit 200] [--batch-size 20]
"""
from __future__ import annotations

import argparse
import json

from dotenv import load_dotenv

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations
from stockmoney.data.scan_classify import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_HOURS,
    DEFAULT_LIMIT,
    DEFAULT_MODEL,
    run_classification_pass,
)


def main(
    *,
    hours: int = DEFAULT_HOURS,
    limit: int = DEFAULT_LIMIT,
    batch_size: int = DEFAULT_BATCH_SIZE,
    model: str = DEFAULT_MODEL,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    load_dotenv()
    conn = get_connection(db_path)
    run_migrations(conn)
    try:
        counts = run_classification_pass(
            conn, hours=hours, limit=limit, batch_size=batch_size, model=model
        )
    finally:
        conn.close()
    print(json.dumps(counts))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=DEFAULT_HOURS)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()
    main(hours=args.hours, limit=args.limit, batch_size=args.batch_size, model=args.model)
