from __future__ import annotations

import logging
import socket
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, Mapping

import duckdb
import polars as pl

from stockmoney.data.db import append_rows

FEED_FETCH_TIMEOUT_SECONDS = 15.0


@contextmanager
def capture_yfinance_errors():
    """Capture yfinance's internal per-ticker failure log records.

    yfinance's own downloader (yfinance/base.py) catches connection-level
    exceptions (proxy blocks, DNS failures, ...) per ticker and only
    `logger.error()`s them -- it never re-raises, so a total connectivity
    failure across every requested symbol looks byte-for-byte identical to a
    legitimate "no data in this date range" response (both are an empty
    DataFrame, no exception). This is the only place the failure is still
    visible: yfinance logs it to the 'yfinance' logger. Confirmed against a
    live 403'd proxy 2026-08-17, where ohlcv_daily/vix_term_structure_daily
    logged status='success' with 0 rows for exactly this reason. Callers
    should treat "frame ended up empty AND at least one error was captured"
    as a hard failure, distinct from "empty, no errors" (genuinely no data).
    """
    records: list[str] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    handler = _Handler(level=logging.ERROR)
    logger = logging.getLogger("yfinance")
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


def parse_feed_with_timeout(url: str, timeout: float = FEED_FETCH_TIMEOUT_SECONDS, **kwargs):
    """feedparser.parse() over the network with no bound: a server that opens
    the connection and then never sends data hangs the call indefinitely --
    confirmed cause of the 2026-08-14 stockmoney-scan-ingest/-classify
    incident (single feed hang blew a 300s cron timeout out to ~37 minutes,
    since the per-feed try/except in rss_news.py/reddit_scanner.py can only
    catch an exception, never a hang). feedparser has no per-call timeout
    argument; it honors socket.setdefaulttimeout() instead, which is process-
    global, so it's set immediately before the call and always restored in a
    finally -- never left mutated for unrelated code elsewhere in the
    process."""
    import feedparser

    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        return feedparser.parse(url, **kwargs)
    finally:
        socket.setdefaulttimeout(previous)


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
