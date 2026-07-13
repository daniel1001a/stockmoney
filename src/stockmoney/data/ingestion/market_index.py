"""Broad-market index OHLCV ingestion (SPY), for models/vrp.py's forward-
realized-volatility target -- see 036_market_index_ohlcv_daily.sql for why this
is a dedicated table rather than a watchlist_members addition to ohlcv_daily.

Reuses yfinance_ohlcv.fetch_ohlcv verbatim (same free daily-history source,
same schema) rather than re-implementing the download -- the only difference
from ingest_watchlist_ohlcv is the symbol list (a fixed market index, not the
watchlist) and the target table.
"""
from __future__ import annotations

from datetime import date

import duckdb

from stockmoney.data.ingestion.base import run_ingestion
from stockmoney.data.ingestion.yfinance_ohlcv import fetch_ohlcv

MARKET_INDEX_SYMBOLS = ["SPY"]


def ingest_market_index_ohlcv(
    conn: duckdb.DuckDBPyConnection,
    start: date,
    end: date,
    *,
    symbols: list[str] | None = None,
) -> int:
    symbols = symbols if symbols is not None else MARKET_INDEX_SYMBOLS
    result = run_ingestion(
        conn,
        source="yfinance",
        target_table="market_index_ohlcv_daily",
        window_start=start,
        window_end=end,
        fetch_fn=lambda: fetch_ohlcv(symbols, start, end),
    )
    return result.rows_written
