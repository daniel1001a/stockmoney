from datetime import date, datetime, timedelta, timezone

import duckdb
import polars as pl
import pytest

from stockmoney.data.db import append_rows, run_migrations
from stockmoney.data.features.realized_vol import (
    FEATURE_NAME,
    FEATURE_VERSION,
    WINDOW,
    compute_realized_vol_20d,
)


def _migrated_conn() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _seed_ohlcv(conn, symbol: str, closes: list[float], start: date) -> list[datetime]:
    dates = [start + timedelta(days=i) for i in range(len(closes))]
    ingested_ats = [
        datetime(d.year, d.month, d.day, 21, 0, tzinfo=timezone.utc) for d in dates
    ]
    df = pl.DataFrame(
        {
            "symbol": [symbol] * len(closes),
            "trade_date": dates,
            "close": closes,
            "source": ["test"] * len(closes),
            "ingested_at": ingested_ats,
        }
    )
    append_rows(conn, "ohlcv_daily", df)
    return ingested_ats


def test_realized_vol_row_count_and_zero_variance_for_constant_growth():
    conn = _migrated_conn()
    n_days = WINDOW + 5
    closes = [100.0 * (1.001**i) for i in range(n_days)]
    _seed_ohlcv(conn, "NVDA", closes, date(2026, 1, 2))

    n = compute_realized_vol_20d(conn)

    assert n == 5  # n_days - WINDOW
    rows = conn.execute(
        "SELECT feature_value, feature_name, feature_version, source_table "
        "FROM feature_store ORDER BY feature_date"
    ).fetchall()
    assert len(rows) == 5
    for value, feature_name, feature_version, source_table in rows:
        assert value == pytest.approx(0.0, abs=1e-6)
        assert feature_name == FEATURE_NAME
        assert feature_version == FEATURE_VERSION
        assert source_table == "ohlcv_daily"


def test_realized_vol_available_at_matches_latest_ingested_version():
    conn = _migrated_conn()
    n_days = WINDOW + 1
    closes = [100.0 + i for i in range(n_days)]
    last_date = date(2026, 1, 2) + timedelta(days=n_days - 1)
    _seed_ohlcv(conn, "AMD", closes, date(2026, 1, 2))

    # Simulate a stale intraday snapshot for the last day, ingested earlier,
    # later superseded by the final close already seeded above.
    stale_ingested_at = datetime(2025, 12, 1, tzinfo=timezone.utc)
    stale = pl.DataFrame(
        {
            "symbol": ["AMD"],
            "trade_date": [last_date],
            "close": [9999.0],
            "source": ["test"],
            "ingested_at": [stale_ingested_at],
        }
    )
    append_rows(conn, "ohlcv_daily", stale)

    compute_realized_vol_20d(conn)

    row = conn.execute(
        "SELECT feature_date, available_at FROM feature_store WHERE symbol = 'AMD'"
    ).fetchone()
    assert row[0] == last_date
    final_ingested_at = datetime(
        last_date.year, last_date.month, last_date.day, 21, 0, tzinfo=timezone.utc
    )
    assert row[1] == final_ingested_at
    assert row[1] != stale_ingested_at


def test_realized_vol_rerun_over_same_window_is_idempotent():
    conn = _migrated_conn()
    n_days = WINDOW + 5
    closes = [100.0 * (1.001**i) for i in range(n_days)]
    _seed_ohlcv(conn, "NVDA", closes, date(2026, 1, 2))

    first = compute_realized_vol_20d(conn)
    second = compute_realized_vol_20d(conn)  # simulates the next cron tick, no new data

    assert first == 5
    assert second == 0
    assert conn.execute("SELECT count(*) FROM feature_store").fetchone()[0] == 5


def test_realized_vol_insufficient_history_produces_no_rows():
    conn = _migrated_conn()
    closes = [100.0 + i for i in range(WINDOW)]  # exactly WINDOW days, not WINDOW+1
    _seed_ohlcv(conn, "TSM", closes, date(2026, 1, 2))

    n = compute_realized_vol_20d(conn)

    assert n == 0
    assert conn.execute("SELECT count(*) FROM feature_store").fetchone()[0] == 0
