from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, Mapping

import duckdb
import polars as pl

from stockmoney.data.db import append_rows


@dataclass
class IngestionResult:
    run_id: str
    rows_written: int
    status: str  # 'success' | 'failed'


def run_ingestion(
    conn: duckdb.DuckDBPyConnection,
    *,
    source: str,
    target_table: str,
    fetch_fn: Callable[[], pl.DataFrame],
    window_start: date | None = None,
    window_end: date | None = None,
) -> IngestionResult:
    """Run one ingestion job: fetch -> append_rows -> audit in ingestion_runs.

    This is the single supported entry point for bringing external data in.
    Every connector (yfinance, FRED, Reddit, GitHub, GDELT, ...) only needs to
    supply ``fetch_fn`` returning a polars DataFrame shaped like the target
    raw table; this function handles the append-only write plus the audit
    trail, so every crawler behaves consistently and a crash is never
    silently lost. Failures are logged to ``ingestion_runs`` (status='failed')
    before being re-raised.
    """
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)

    try:
        df = fetch_fn()
        rows_written = append_rows(conn, target_table, df, ingested_at=started_at)
        _log_run(
            conn, run_id, source, target_table, window_start, window_end,
            rows_written, "success", None, started_at,
        )
        return IngestionResult(run_id, rows_written, "success")
    except Exception as exc:
        _log_run(
            conn, run_id, source, target_table, window_start, window_end,
            0, "failed", str(exc), started_at,
        )
        raise


def run_multi_ingestion(
    conn: duckdb.DuckDBPyConnection,
    *,
    source: str,
    fetch_fn: Callable[[], Mapping[str, pl.DataFrame]],
    window_start: date | None = None,
    window_end: date | None = None,
) -> dict[str, IngestionResult]:
    """Run one ingestion whose single upstream pull writes several tables.

    Some sources (an options chain, a GDELT batch) produce data for multiple
    raw tables from one expensive fetch. ``fetch_fn`` returns a mapping of
    ``{target_table: DataFrame}``; each table's write gets its own audit row
    in ``ingestion_runs`` (so coverage is tracked per table), while the fetch
    happens only once. A fetch failure is logged once against ``source`` and
    re-raised; a per-table write failure is logged against that table and
    re-raised.
    """
    started_at = datetime.now(timezone.utc)

    try:
        frames = fetch_fn()
    except Exception as exc:
        _log_run(
            conn, str(uuid.uuid4()), source, source, window_start, window_end,
            0, "failed", str(exc), started_at,
        )
        raise

    results: dict[str, IngestionResult] = {}
    for target_table, df in frames.items():
        run_id = str(uuid.uuid4())
        try:
            rows_written = append_rows(conn, target_table, df, ingested_at=started_at)
            _log_run(
                conn, run_id, source, target_table, window_start, window_end,
                rows_written, "success", None, started_at,
            )
            results[target_table] = IngestionResult(run_id, rows_written, "success")
        except Exception as exc:
            _log_run(
                conn, run_id, source, target_table, window_start, window_end,
                0, "failed", str(exc), started_at,
            )
            raise
    return results


def _log_run(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    source: str,
    target_table: str,
    window_start: date | None,
    window_end: date | None,
    rows_written: int,
    status: str,
    error_message: str | None,
    started_at: datetime,
) -> None:
    row = pl.DataFrame(
        {
            "run_id": [run_id],
            "source": [source],
            "target_table": [target_table],
            "window_start": [window_start],
            "window_end": [window_end],
            "rows_written": [rows_written],
            "status": [status],
            "error_message": [error_message],
            "started_at": [started_at],
            "finished_at": [datetime.now(timezone.utc)],
        }
    )
    append_rows(conn, "ingestion_runs", row)
