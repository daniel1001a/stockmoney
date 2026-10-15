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
    uv run python scripts/classify_scan.py [--hours 6] [--limit 200] [--batch-size 20] [--model haiku] [--engine claude|jev|shadow]

Engines:
    claude  (default) Claude CLI only.
    jev     TypeSafe Jev first (needs TYPESAFE_API_KEY); uncertain or
            candidate-worthy items escalate to the Claude CLI.
    shadow  Claude CLI results are applied; Jev is only logged to
            `jev_judgments` for calibration (needs TYPESAFE_API_KEY).
"""
from __future__ import annotations

import argparse
import json

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations
from stockmoney.data.jev_classify import make_jev_classify_fn, make_shadow_classify_fn
from stockmoney.data.scan_classify import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_CLI_MODEL,
    DEFAULT_HOURS,
    DEFAULT_LIMIT,
    claude_cli_classify_fn,
    run_classification_pass,
)


def main(
    *,
    hours: int = DEFAULT_HOURS,
    limit: int = DEFAULT_LIMIT,
    batch_size: int = DEFAULT_BATCH_SIZE,
    model: str = DEFAULT_CLI_MODEL,
    engine: str = "claude",
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    conn = get_connection(db_path)
    run_migrations(conn)
    if engine not in ("claude", "jev", "shadow"):
        raise ValueError(f"unknown engine: {engine}")
    claude_fn = lambda batch, symbols: claude_cli_classify_fn(batch, symbols, model=model)  # noqa: E731
    classify_fn = None
    if engine == "jev":
        classify_fn = make_jev_classify_fn(conn, escalate_fn=claude_fn)
    elif engine == "shadow":
        classify_fn = make_shadow_classify_fn(
            claude_fn, make_jev_classify_fn(conn, escalate_fn=lambda b, s: [])
        )
    try:
        counts = run_classification_pass(
            conn, hours=hours, limit=limit, batch_size=batch_size, model=model,
            classify_fn=classify_fn,
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
    parser.add_argument("--engine", choices=["claude", "jev", "shadow"], default="claude")
    args = parser.parse_args()
    main(
        hours=args.hours, limit=args.limit, batch_size=args.batch_size,
        model=args.model, engine=args.engine,
    )
