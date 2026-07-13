"""Daily options-chain snapshot capture -- run this EVERY trading day.

Unlike every other data source in this project, yfinance option chains are
snapshot-only: there is no free historical option-chain API, so a day this
script doesn't run is a day of IV surface / put-call ratio / GEX / skew data
that is gone forever, with no way to backfill it later. That makes this the
single most time-sensitive script in the repo -- it deserves its own small,
dedicated, easily-scheduled entrypoint independent of the full nightly_refresh
pipeline (which also does much slower model fitting/backtesting and is more
likely to have something else fail first).

What it captures (into iv_surface_daily / put_call_ratio_daily /
options_derived_daily): 25/50-delta implied vol, put/call volume+OI, and
derived GEX/skew estimates for the whole watchlist. `trade_date` is resolved
from the latest real trading day in ohlcv_daily (not wall-clock date), so
running this on a weekend/holiday can't orphan the snapshot under a date
nothing else will ever join to -- see options_chain.ingest_watchlist_options.

Once enough days have accumulated (CLAUDE.md section 12's discipline: no
fixed number, just "does the ablation test pass"), promote via
models/backtest_options_feature_ablation.py exactly like
backtest_vix_term_ablation.py did for the VIX-term-structure candidate.

Usage (schedule this daily -- OpenClaw cron / APScheduler / crontab):
    .venv/bin/python scripts/capture_options_snapshot.py
    .venv/bin/python scripts/capture_options_snapshot.py --db data/stockmoney.duckdb
"""
from __future__ import annotations

import argparse
import json

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations
from stockmoney.data.ingestion.options_chain import ingest_watchlist_options


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    conn = get_connection(args.db)
    run_migrations(conn)
    written = ingest_watchlist_options(conn)  # no trade_date: resolves latest real trading day
    days_captured = conn.execute("SELECT count(DISTINCT trade_date) FROM iv_surface_daily").fetchone()[0]
    conn.close()
    print(json.dumps({**written, "distinct_days_captured_so_far": days_captured}, indent=2))


if __name__ == "__main__":
    main()
