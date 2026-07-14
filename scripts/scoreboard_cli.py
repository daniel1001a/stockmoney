"""Daily win-rate scoreboard CLI -- the project's standing judge for any
strategy (the user's requested "每天證明勝率" engine). All logic lives in
stockmoney.backtest.scoreboard; this script only wires up the DB connection
and prints the table.

Usage:
    STOCKMONEY_DB=data/stockmoney_live.duckdb .venv/bin/python -m scripts.scoreboard_cli
    STOCKMONEY_DB=data/stockmoney_live.duckdb .venv/bin/python -m scripts.scoreboard_cli --horizons 5
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone

import duckdb

from stockmoney.backtest.scoreboard import DEFAULT_HORIZONS, format_table, run_scoreboard
from stockmoney.data.db import DEFAULT_DB_PATH


def main() -> None:
    parser = argparse.ArgumentParser(description="Print the dated strategy scoreboard.")
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
        result = run_scoreboard(conn, horizons=tuple(args.horizons))
        print(format_table(result))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
