"""Unit tests for scripts/import_from_sync.py against hand-made Parquet files.

Ships before Agent 3's first real export exists (NEXT_AGENT_PLAN.md Wave 0):
the sample Parquet stands in for the OpenClaw exporter's output so the
idempotency / anti-join / look-ahead guarantees are verified end to end now.
"""
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import import_from_sync

UTC = timezone.utc


def _write(exports_dir: Path, table: str, name: str, df: pl.DataFrame) -> Path:
    d = exports_dir / table
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    df.write_parquet(path)
    return path


def _catalyst_df(signal_ids, symbol="NVDA") -> pl.DataFrame:
    n = len(signal_ids)
    return pl.DataFrame(
        {
            "signal_id": list(signal_ids),
            "symbol": [symbol] * n,
            "as_of_date": [date(2026, 7, 11)] * n,
            "catalyst_summary": ["export rule eases chip sales"] * n,
            "transmission_chain": ["rule -> market -> NVDA"] * n,
            "novelty_score": [0.7] * n,
            "sentiment_score": [0.4] * n,
            "priced_in_estimate": [0.3] * n,
            "source_refs": ['["a1"]'] * n,
            "model_version": ["test-v1"] * n,
            "available_at": [datetime(2026, 7, 11, 13, 0, tzinfo=UTC)] * n,
            "created_at": [datetime(2026, 7, 11, 13, 0, tzinfo=UTC)] * n,
        }
    )


def _count(db_path: str, table: str) -> int:
    conn = duckdb.connect(db_path, read_only=True)
    try:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_fresh_import_inserts_rows(tmp_path):
    db = str(tmp_path / "live.duckdb")
    exports = tmp_path / "exports"
    _write(exports, "catalyst_signals", "day1.parquet", _catalyst_df(["s1", "s2"]))

    summary = import_from_sync.run_import(db, str(exports))

    assert summary["tables"]["catalyst_signals"]["rows_inserted"] == 2
    assert summary["total_rows_inserted"] == 2
    assert _count(db, "catalyst_signals") == 2


def test_rerun_same_file_is_idempotent(tmp_path):
    db = str(tmp_path / "live.duckdb")
    exports = tmp_path / "exports"
    _write(exports, "catalyst_signals", "day1.parquet", _catalyst_df(["s1", "s2"]))

    import_from_sync.run_import(db, str(exports))
    second = import_from_sync.run_import(db, str(exports))

    # Unchanged file -> skipped via the SHA watermark, nothing re-read/inserted.
    assert second["tables"]["catalyst_signals"]["files_skipped"] == 1
    assert second["tables"]["catalyst_signals"]["rows_inserted"] == 0
    assert _count(db, "catalyst_signals") == 2


def test_reexported_file_inserts_only_new_rows(tmp_path):
    # Agent 3 re-exports the same logical file with rows appended: new checksum
    # -> the file IS re-read, but the PK anti-join skips the already-present
    # rows so only the genuinely new one lands.
    db = str(tmp_path / "live.duckdb")
    exports = tmp_path / "exports"
    _write(exports, "catalyst_signals", "day1.parquet", _catalyst_df(["s1", "s2"]))
    import_from_sync.run_import(db, str(exports))

    _write(exports, "catalyst_signals", "day1.parquet", _catalyst_df(["s1", "s2", "s3"]))
    second = import_from_sync.run_import(db, str(exports))

    assert second["tables"]["catalyst_signals"]["files_imported"] == 1
    assert second["tables"]["catalyst_signals"]["rows_inserted"] == 1
    assert _count(db, "catalyst_signals") == 3


def test_composite_pk_keeps_distinct_ingested_at(tmp_path):
    # Two snapshots of the same option cell on the same trade_date but captured
    # at different ingested_at are DIFFERENT rows (ingested_at is in the PK and
    # is the look-ahead timestamp) -- both must survive.
    db = str(tmp_path / "live.duckdb")
    exports = tmp_path / "exports"

    def iv_row(ingested_at):
        return {
            "symbol": "NVDA",
            "trade_date": date(2026, 7, 11),
            "expiry_date": date(2026, 8, 15),
            "delta_bucket": "50",
            "implied_vol": 0.42,
            "source": "yfinance",
            "ingested_at": ingested_at,
        }

    df = pl.DataFrame(
        [
            iv_row(datetime(2026, 7, 11, 21, 0, tzinfo=UTC)),
            iv_row(datetime(2026, 7, 12, 21, 0, tzinfo=UTC)),
        ]
    )
    _write(exports, "iv_surface_daily", "snap.parquet", df)

    summary = import_from_sync.run_import(db, str(exports))
    assert summary["tables"]["iv_surface_daily"]["rows_inserted"] == 2
    assert _count(db, "iv_surface_daily") == 2


def test_missing_exports_dir_is_graceful(tmp_path):
    db = str(tmp_path / "live.duckdb")
    summary = import_from_sync.run_import(db, str(tmp_path / "nope"))

    assert summary["exports_dir_exists"] is False
    assert summary["total_rows_inserted"] == 0


def test_stray_column_is_rejected(tmp_path):
    db = str(tmp_path / "live.duckdb")
    exports = tmp_path / "exports"
    df = _catalyst_df(["s1"]).with_columns(pl.lit("boom").alias("not_a_real_col"))
    _write(exports, "catalyst_signals", "day1.parquet", df)

    with pytest.raises(ValueError, match="not_a_real_col"):
        import_from_sync.run_import(db, str(exports))
    # Failed file was rolled back and NOT watermarked, so a later good export
    # of the same table still works.
    assert _count(db, "catalyst_signals") == 0


def test_unknown_table_arg_rejected(tmp_path):
    db = str(tmp_path / "live.duckdb")
    with pytest.raises(ValueError, match="Unknown table"):
        import_from_sync.run_import(db, str(tmp_path), only_table="bogus_table")
