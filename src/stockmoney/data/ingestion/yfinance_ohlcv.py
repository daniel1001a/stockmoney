from __future__ import annotations

from datetime import date, timedelta

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
    long/tidy ``ohlcv_daily`` schema (one row per symbol per trade_date).

    Both ``start`` and ``end`` are INCLUSIVE from this function's callers'
    point of view -- but yfinance's own ``end`` parameter is exclusive
    (confirmed empirically: end=2026-07-10 returns rows only through
    2026-07-09), so a caller passing today's date as ``end`` would otherwise
    silently never receive today's own close. Every caller here
    (nightly_refresh's rolling window, build_live's historical backfill)
    reasons about ``end`` as "through this date", so the +1 day is applied
    once, internally, rather than asking every caller to remember yfinance's
    exclusive convention.
    """
    import yfinance as yf

    data = yf.download(
        symbols,
        start=start,
        end=end + timedelta(days=1),
        auto_adjust=False,
        group_by="ticker",
        progress=False,
    )

    frames = []
    for symbol in symbols:
        # yfinance sometimes returns a partial bar (valid Open/High/Low/Volume
        # but NaN Close) rather than omitting the row entirely -- a Close-less
        # bar is unusable downstream, so drop on NaN Close specifically rather
        # than relying on dropna(how="all") (which only catches fully-empty
        # rows) (incident 2026-07-15: 29 NaN-close rows for 2026-07-14).
        sub = data[symbol].dropna(how="all").dropna(subset=["Close"])
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
