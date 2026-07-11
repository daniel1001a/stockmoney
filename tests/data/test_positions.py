from datetime import date, datetime, timezone

import duckdb
import polars as pl
import pytest

from stockmoney.data.db import append_rows, run_migrations
from stockmoney.data.positions import (
    close_position,
    get_position,
    latest_underlying_price,
    list_open_positions,
    open_position,
    price_on_date,
)


def _migrated_conn() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def test_open_position_round_trips():
    conn = _migrated_conn()
    position_id = open_position(
        conn, symbol="soxl", option_right="call", side="long", strike=180.0,
        expiry_date=date(2026, 9, 18), entry_date=date(2026, 7, 10),
        entry_underlying_price=174.82, entry_premium=12.5, entry_iv=0.55,
        thesis_note="semis breakout",
    )
    position = get_position(conn, position_id)
    assert position.symbol == "SOXL"  # normalized upper
    assert position.option_right == "call"
    assert position.side == "long"
    assert position.entry_premium == 12.5
    assert position.entry_iv == 0.55


def test_open_position_rejects_invalid_right():
    conn = _migrated_conn()
    with pytest.raises(ValueError):
        open_position(
            conn, symbol="SOXL", option_right="banana", side="long", strike=1,
            expiry_date=date(2026, 9, 18), entry_date=date(2026, 7, 10),
            entry_underlying_price=1, entry_premium=1,
        )


def test_list_open_positions_excludes_closed():
    conn = _migrated_conn()
    open_id = open_position(
        conn, symbol="SOXL", option_right="call", side="long", strike=1,
        expiry_date=date(2026, 9, 18), entry_date=date(2026, 7, 10),
        entry_underlying_price=1, entry_premium=1,
    )
    closed_id = open_position(
        conn, symbol="NVDA", option_right="put", side="short", strike=1,
        expiry_date=date(2026, 9, 18), entry_date=date(2026, 7, 9),
        entry_underlying_price=1, entry_premium=1,
    )
    close_position(conn, closed_id, reason="test")

    open_positions = list_open_positions(conn)
    assert [p.position_id for p in open_positions] == [open_id]


def test_close_position_is_idempotent_guard():
    conn = _migrated_conn()
    position_id = open_position(
        conn, symbol="SOXL", option_right="call", side="long", strike=1,
        expiry_date=date(2026, 9, 18), entry_date=date(2026, 7, 10),
        entry_underlying_price=1, entry_premium=1,
    )
    close_position(conn, position_id, reason="done")
    with pytest.raises(ValueError):
        close_position(conn, position_id, reason="again")


def test_close_position_unknown_id_raises():
    conn = _migrated_conn()
    with pytest.raises(ValueError):
        close_position(conn, "not-a-real-id")


def test_latest_underlying_price_dedupes_by_ingested_at():
    conn = _migrated_conn()
    df = pl.DataFrame({
        "symbol": ["SOXL", "SOXL"],
        "trade_date": [date(2026, 7, 8), date(2026, 7, 8)],
        "open": [100.0, 100.0],
        "high": [101.0, 101.0],
        "low": [99.0, 99.0],
        "close": [100.0, 174.82],
        "adj_close": [100.0, 174.82],
        "volume": [1000, 1000],
        "source": ["yfinance", "yfinance"],
        "ingested_at": [
            datetime(2026, 7, 9, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc),
        ],
    })
    append_rows(conn, "ohlcv_daily", df)

    result = latest_underlying_price(conn, "soxl")
    assert result == (date(2026, 7, 8), 174.82)


def test_latest_underlying_price_returns_none_when_missing():
    conn = _migrated_conn()
    assert latest_underlying_price(conn, "ZZZZ") is None


def test_price_on_date_dedupes_by_ingested_at():
    conn = _migrated_conn()
    df = pl.DataFrame({
        "symbol": ["SOXL", "SOXL", "SOXL"],
        "trade_date": [date(2026, 7, 7), date(2026, 7, 8), date(2026, 7, 8)],
        "open": [100.0, 100.0, 100.0],
        "high": [101.0, 101.0, 101.0],
        "low": [99.0, 99.0, 99.0],
        "close": [90.0, 100.0, 174.82],
        "adj_close": [90.0, 100.0, 174.82],
        "volume": [1000, 1000, 1000],
        "source": ["yfinance", "yfinance", "yfinance"],
        "ingested_at": [
            datetime(2026, 7, 7, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 9, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc),
        ],
    })
    append_rows(conn, "ohlcv_daily", df)

    assert price_on_date(conn, "soxl", date(2026, 7, 8)) == 174.82
    assert price_on_date(conn, "soxl", date(2026, 7, 7)) == 90.0


def test_price_on_date_returns_none_when_missing():
    conn = _migrated_conn()
    assert price_on_date(conn, "SOXL", date(2026, 7, 8)) is None
