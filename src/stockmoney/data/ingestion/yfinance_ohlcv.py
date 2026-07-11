from __future__ import annotations

from datetime import date

import duckdb
import polars as pl

from stockmoney.data.ingestion.base import run_ingestion

_EMPTY_SCHEMA = {
    "symbol": pl.Utf8,
    "trade_date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "adj_close": pl.Float64,
    "volume": pl.Int64,
    "source": pl.Utf8,
}


def fetch_ohlcv(symbols: list[str], start: date, end: date) -> pl.DataFrame:
    """Fetch daily OHLCV for ``symbols`` from yfinance and reshape into the
    long/tidy ``ohlcv_daily`` schema (one row per symbol per trade_date)."""
    import yfinance as yf

    data = yf.download(
        symbols,
        start=start,
        end=end,
        auto_adjust=False,
        group_by="ticker",
        progress=False,
    )

    frames = []
    for symbol in symbols:
        sub = data[symbol].dropna(how="all")
        if sub.empty:
            continue
        frames.append(
            pl.DataFrame(
                {
                    "symbol": [symbol] * len(sub),
                    "trade_date": [ts.date() for ts in sub.index.to_pydatetime()],
                    "open": sub["Open"].tolist(),
                    "high": sub["High"].tolist(),
                    "low": sub["Low"].tolist(),
                    "close": sub["Close"].tolist(),
                    "adj_close": sub["Adj Close"].tolist(),
                    "volume": [int(v) for v in sub["Volume"].tolist()],
                    "source": ["yfinance"] * len(sub),
                },
                schema=_EMPTY_SCHEMA,
            )
        )

    if not frames:
        return pl.DataFrame(schema=_EMPTY_SCHEMA)
    return pl.concat(frames)


def ingest_watchlist_ohlcv(
    conn: duckdb.DuckDBPyConnection, start: date, end: date
) -> int:
    """Ingest OHLCV for every active watchlist symbol into ``ohlcv_daily``."""
    symbols = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT symbol FROM watchlist_members "
            "WHERE removed_date IS NULL ORDER BY symbol"
        ).fetchall()
    ]
    result = run_ingestion(
        conn,
        source="yfinance",
        target_table="ohlcv_daily",
        window_start=start,
        window_end=end,
        fetch_fn=lambda: fetch_ohlcv(symbols, start, end),
    )
    return result.rows_written
