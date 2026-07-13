from datetime import date

import duckdb
import pandas as pd

from stockmoney.data.db import run_migrations
from stockmoney.data.ingestion.market_index import ingest_market_index_ohlcv


def _fake_download(symbols, start, end, auto_adjust, group_by, progress):
    idx = pd.to_datetime(["2026-01-02", "2026-01-05"])
    frames = {}
    for i, symbol in enumerate(symbols):
        frames[symbol] = pd.DataFrame(
            {
                "Open": [400.0 + i, 401.0 + i],
                "High": [402.0 + i, 403.0 + i],
                "Low": [399.0 + i, 400.0 + i],
                "Close": [401.0 + i, 402.0 + i],
                "Adj Close": [401.0 + i, 402.0 + i],
                "Volume": [1000, 1100],
            },
            index=idx,
        )
    return pd.concat(frames, axis=1)


def test_ingest_market_index_writes_to_dedicated_table_not_ohlcv_daily(monkeypatch):
    monkeypatch.setattr("yfinance.download", _fake_download)

    conn = duckdb.connect(":memory:")
    run_migrations(conn)

    rows_written = ingest_market_index_ohlcv(conn, date(2026, 1, 1), date(2026, 1, 6))

    assert rows_written == 2  # 2 fake trading days, 1 symbol (SPY default)
    assert conn.execute("SELECT count(*) FROM market_index_ohlcv_daily").fetchone()[0] == 2
    # Zero blast radius: existing watchlist-only ohlcv_daily must stay untouched.
    assert conn.execute("SELECT count(*) FROM ohlcv_daily").fetchone()[0] == 0

    row = conn.execute(
        "SELECT symbol, trade_date, close FROM market_index_ohlcv_daily ORDER BY trade_date"
    ).fetchone()
    assert row == ("SPY", date(2026, 1, 2), 401.0)


def test_ingest_market_index_respects_custom_symbols(monkeypatch):
    monkeypatch.setattr("yfinance.download", _fake_download)

    conn = duckdb.connect(":memory:")
    run_migrations(conn)

    ingest_market_index_ohlcv(conn, date(2026, 1, 1), date(2026, 1, 6), symbols=["QQQ"])

    symbols = conn.execute(
        "SELECT DISTINCT symbol FROM market_index_ohlcv_daily"
    ).fetchall()
    assert symbols == [("QQQ",)]
