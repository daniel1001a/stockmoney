"""Classify freshly-scraped Reddit posts / news articles into per-ticker
sentiment or new-ticker/theme candidates, without going through OpenClaw
(HANDOFF.md: the OpenClaw scanner agent's exec permission is a dead end --
see ~/.claude/plans/frontend-catalyst-rebuild.md). Runs on the Claude Code
subscription via the `claude` CLI (no paid ANTHROPIC_API_KEY -- the user's
standing rule) and writes through `stockmoney.data.scan_classify`, which routes
every result through the same validated write boundary the old skill used
(`stockmoney.data.classification`). This is the pure-Python command the
OpenClaw `stockmoney-scan-classify` cron runs (replacing the agent turn that
kept stalling past the no-output watchdog).

Usage:
    uv run python scripts/classify_scan.py [--hours 6] [--limit 200] [--batch-size 20] [--model haiku]
"""
from __future__ import annotations

import argparse
import json

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations
from stockmoney.data.scan_classify import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_CLI_MODEL,
    DEFAULT_HOURS,
    DEFAULT_LIMIT,
    run_classification_pass,
)


def main(
    *,
    hours: int = DEFAULT_HOURS,
    limit: int = DEFAULT_LIMIT,
    batch_size: int = DEFAULT_BATCH_SIZE,
    model: str = DEFAULT_CLI_MODEL,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
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
    parser.add_argument("--model", default=DEFAULT_CLI_MODEL)
    args = parser.parse_args()
    main(hours=args.hours, limit=args.limit, batch_size=args.batch_size, model=args.model)
