from datetime import date

import duckdb
import polars as pl
import pytest

from stockmoney.data.db import run_migrations
from stockmoney.data.ingestion.base import run_ingestion


def _migrated_conn() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def test_run_ingestion_success_writes_rows_and_audit_log():
    conn = _migrated_conn()

    def fetch_fn() -> pl.DataFrame:
        return pl.DataFrame(
            {
                "symbol": ["NVDA"],
                "trade_date": [date(2026, 1, 2)],
                "close": [140.0],
                "source": ["yfinance"],
            }
        )

    result = run_ingestion(
        conn,
        source="yfinance",
        target_table="ohlcv_daily",
        fetch_fn=fetch_fn,
        window_start=date(2026, 1, 2),
        window_end=date(2026, 1, 2),
    )

    assert result.status == "success"
    assert result.rows_written == 1
    assert len(conn.execute("SELECT * FROM ohlcv_daily").fetchall()) == 1

    run_row = conn.execute(
        "SELECT source, target_table, rows_written, status, error_message, "
        "window_start, window_end FROM ingestion_runs WHERE run_id = ?",
        [result.run_id],
    ).fetchone()
    assert run_row == (
        "yfinance", "ohlcv_daily", 1, "success", None,
        date(2026, 1, 2), date(2026, 1, 2),
    )


def test_run_ingestion_failure_logs_and_reraises():
    conn = _migrated_conn()

    def failing_fetch_fn() -> pl.DataFrame:
        raise RuntimeError("upstream API timed out")

    with pytest.raises(RuntimeError, match="upstream API timed out"):
        run_ingestion(
            conn,
            source="yfinance",
            target_table="ohlcv_daily",
            fetch_fn=failing_fetch_fn,
        )

    assert len(conn.execute("SELECT * FROM ohlcv_daily").fetchall()) == 0
    run_row = conn.execute(
        "SELECT status, rows_written, error_message FROM ingestion_runs"
    ).fetchone()
    assert run_row[0] == "failed"
    assert run_row[1] == 0
    assert "upstream API timed out" in run_row[2]
