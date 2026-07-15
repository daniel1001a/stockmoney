"""Experiment 3 (news-as-signal) scoreboard runner.

Runs the standard scoreboard registry (`scoreboard.default_strategies()`,
which already includes the two baselines this experiment needs --
`sellput_otm5_naive`, `B2_random`, `long_stock`) PLUS the two news plug-ins
from `stockmoney.backtest.news_strategies`, and prints the same
`format_table` the rest of the project already uses -- no new report format,
per WORKER3_AUTONOMOUS_SPEC.md's "reuse the existing tested harness".

Usage:
    STOCKMONEY_DB=data/stockmoney_live.duckdb .venv/bin/python -m scripts.news_experiment_cli
    STOCKMONEY_DB=data/stockmoney_live.duckdb .venv/bin/python -m scripts.news_experiment_cli --horizons 2 3 5
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone

import duckdb

from stockmoney.backtest.news_strategies import news_strategies
from stockmoney.backtest.scoreboard import (
    DEFAULT_HORIZONS,
    default_strategies,
    format_table,
    run_scoreboard,
)
from stockmoney.data.db import DEFAULT_DB_PATH


def main() -> None:
    parser = argparse.ArgumentParser(description="Print the scoreboard including the news-experiment strategies.")
    parser.add_argument(
        "--horizons", type=int, nargs="+", default=list(DEFAULT_HORIZONS),
        help=f"Trading-day horizons to evaluate (default {DEFAULT_HORIZONS}).",
    )
    args = parser.parse_args()

    db = os.environ.get("STOCKMONEY_DB", DEFAULT_DB_PATH)
    print(f"DB = {db} (read-only)")
    print(f"generated_at (UTC) = {datetime.now(timezone.utc).isoformat()}")

    conn = duckdb.connect(db, read_only=True)
    try:
        strategies = default_strategies() + news_strategies()
        result = run_scoreboard(conn, horizons=tuple(args.horizons), strategies=strategies)
        print(format_table(result))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
