from datetime import date

import duckdb
import pandas as pd
import pytest

from stockmoney.data.db import run_migrations
from stockmoney.data.ingestion.yfinance_ohlcv import fetch_ohlcv, ingest_watchlist_ohlcv


def _fake_download(symbols, start, end, auto_adjust, group_by, progress):
    idx = pd.to_datetime(["2026-01-02", "2026-01-05"])
    frames = {}
    for i, symbol in enumerate(symbols):
        frames[symbol] = pd.DataFrame(
            {
                "Open": [100.0 + i, 101.0 + i],
                "High": [102.0 + i, 103.0 + i],
                "Low": [99.0 + i, 100.0 + i],
                "Close": [101.0 + i, 102.0 + i],
                "Adj Close": [101.0 + i, 102.0 + i],
                "Volume": [1000, 1100],
            },
            index=idx,
        )
    return pd.concat(frames, axis=1)


def _fake_download_empty(symbols, start, end, auto_adjust, group_by, progress):
    idx = pd.to_datetime([])
    frames = {
        s: pd.DataFrame(
            columns=["Open", "High", "Low", "Close", "Adj Close", "Volume"], index=idx
        )
        for s in symbols
    }
    return pd.concat(frames, axis=1)


def test_fetch_ohlcv_reshapes_multi_symbol(monkeypatch):
    monkeypatch.setattr("yfinance.download", _fake_download)

    df = fetch_ohlcv(["AAPL", "MSFT"], date(2026, 1, 1), date(2026, 1, 6))

    assert df.height == 4
    assert df.columns == [
        "symbol", "trade_date", "open", "high", "low",
        "close", "adj_close", "volume", "source",
    ]
    assert set(df["symbol"].unique().to_list()) == {"AAPL", "MSFT"}
    assert set(df["source"].unique().to_list()) == {"yfinance"}
    aapl_row = df.filter(df["symbol"] == "AAPL").sort("trade_date").row(0, named=True)
    assert aapl_row["trade_date"] == date(2026, 1, 2)
    assert aapl_row["close"] == 101.0


def test_fetch_ohlcv_returns_empty_frame_with_correct_schema(monkeypatch):
    monkeypatch.setattr("yfinance.download", _fake_download_empty)

    df = fetch_ohlcv(["AAPL"], date(2026, 1, 1), date(2026, 1, 6))

    assert df.height == 0
    assert df.columns == [
        "symbol", "trade_date", "open", "high", "low",
        "close", "adj_close", "volume", "source",
    ]


def test_ingest_watchlist_ohlcv_end_to_end(monkeypatch):
    monkeypatch.setattr("yfinance.download", _fake_download)

    conn = duckdb.connect(":memory:")
    run_migrations(conn)

    n_watchlist = conn.execute(
        "SELECT count(*) FROM watchlist_members WHERE removed_date IS NULL"
    ).fetchone()[0]

    rows_written = ingest_watchlist_ohlcv(conn, date(2026, 1, 1), date(2026, 1, 6))

    assert rows_written == n_watchlist * 2  # 2 fake trading days per symbol
    assert (
        conn.execute("SELECT count(*) FROM ohlcv_daily").fetchone()[0]
        == n_watchlist * 2
    )

    run_row = conn.execute(
        "SELECT status, source, target_table, rows_written FROM ingestion_runs"
    ).fetchone()
    assert run_row == ("success", "yfinance", "ohlcv_daily", n_watchlist * 2)
