"""Backfill 8 years of core-layer history (OHLCV + FRED macro).

Idempotent: append_rows / the ingestion audit trail tolerate re-runs, so this
can be run repeatedly. Options chains are snapshot-only (no history) and are
intentionally excluded — the module-A vertical slice uses core features only.

Usage:
    uv run python scripts/backfill.py [years]
"""
from __future__ import annotations

import sys
from datetime import date, timedelta

from stockmoney.data.db import get_connection, run_migrations, DEFAULT_DB_PATH
from stockmoney.data.ingestion.fred_macro import ingest_macro_series
from stockmoney.data.ingestion.yfinance_ohlcv import ingest_watchlist_ohlcv


def main(years: int = 8, db_path: str = DEFAULT_DB_PATH) -> None:
    conn = get_connection(db_path)
    run_migrations(conn)

    end = date.today()
    start = end - timedelta(days=round(years * 365.25))

    print(f"Backfilling {years}y: {start} -> {end}")
    ohlcv_rows = ingest_watchlist_ohlcv(conn, start, end)
    print(f"  ohlcv_daily: {ohlcv_rows} rows")
    macro_rows = ingest_macro_series(conn, start, end)
    print(f"  macro_series_daily: {macro_rows} rows")

    print("\nCoverage:")
    print(conn.sql(
        "SELECT symbol, count(*) AS n, min(trade_date) AS first, max(trade_date) AS last "
        "FROM ohlcv_daily GROUP BY symbol ORDER BY symbol"
    ))
    conn.close()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8)
