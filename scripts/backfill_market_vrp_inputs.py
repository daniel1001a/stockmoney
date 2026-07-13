"""Backfill years of SPY + VIX term-structure history for the market-level VRP
backtest (models/vrp.py, models/backtest_vrp_gate.py).

Unlike per-symbol options chains (snapshot-only, no free history — the reason
Agent 3's daily capture cron exists at all), both of these sources have full
free multi-year daily history available right now, so this is a one-time-ish
backfill in the same spirit as scripts/backfill.py's 8y OHLCV/macro pull, not
something that needs cron scheduling.

Idempotent-safe to re-run (append_rows/ingestion_runs tolerate re-runs, same
as backfill.py), but re-running with an overlapping window DOES insert
duplicate rows with a fresh ingested_at (run_ingestion is append-only) --
models/vrp.py's loaders already dedup to the latest ingested_at per
trade_date, so duplicates from repeated backfills never corrupt the VRP
matrix, they just add harmless extra `_sync_import_log`-style history.

Usage:
    .venv/bin/python scripts/backfill_market_vrp_inputs.py [years]
    .venv/bin/python scripts/backfill_market_vrp_inputs.py 8 --db data/stockmoney_live.duckdb
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations
from stockmoney.data.ingestion.market_index import ingest_market_index_ohlcv
from stockmoney.data.ingestion.vix_term import ingest_vix_term


def main(years: int = 8, db_path: str = DEFAULT_DB_PATH) -> None:
    conn = get_connection(db_path)
    run_migrations(conn)

    end = date.today()
    start = end - timedelta(days=round(years * 365.25))

    print(f"Backfilling {years}y: {start} -> {end}")
    spy_rows = ingest_market_index_ohlcv(conn, start, end)
    print(f"  market_index_ohlcv_daily: {spy_rows} rows")
    vix_rows = ingest_vix_term(conn, start, end)
    print(f"  vix_term_structure_daily: {vix_rows} rows")

    print("\nCoverage:")
    print(conn.sql(
        "SELECT symbol, count(*) AS n, min(trade_date) AS first, max(trade_date) AS last "
        "FROM market_index_ohlcv_daily GROUP BY symbol"
    ))
    print(conn.sql(
        "SELECT tenor_days, count(*) AS n, min(trade_date) AS first, max(trade_date) AS last "
        "FROM vix_term_structure_daily GROUP BY tenor_days ORDER BY tenor_days"
    ))
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("years", nargs="?", type=int, default=8)
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    main(args.years, args.db)
