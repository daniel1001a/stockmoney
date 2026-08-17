"""Round-trip tests for export_league_for_sync.py / import_league_from_sync.py
(issue #7 follow-up): the League's own tables get UPDATED after insertion
(confirmed/withdrawn/graded calls, portfolio cash changes), which is exactly
what the original watermark/anti-join sync (export_for_sync.py) can't handle
-- these tests specifically verify that an UPDATE survives an export+import
round trip, not just a fresh insert.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import duckdb

import export_league_for_sync as exp
import import_league_from_sync as imp
from stockmoney.data import trader_predictions as tp
from stockmoney.data.db import run_migrations

TRADE_DATE = date(2026, 6, 1)
END = TRADE_DATE + timedelta(days=7)


def _conn(path: str):
    conn = duckdb.connect(path)
    run_migrations(conn)
    return conn


def _record_call(conn, *, symbol="SOXL", direction="up") -> str:
    return tp.record_trader_prediction(
        conn, trader_id="chartist", method_version="m", trade_date=TRADE_DATE,
        symbol=symbol, sector="semiconductor", horizon=5, label_end_date=END,
        direction=direction, conviction=0.7, rationale="r", invalidation="i",
        entry_price=100.0, grade_vol=0.4, engine_payload={}, regime=0,
        decision_point="pre_market",
    )


def test_export_writes_a_parquet_file_with_correct_row_count(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "a.duckdb")
    conn = _conn(db)
    _record_call(conn)
    _record_call(conn, symbol="NVDA")
    conn.close()

    summary = exp.run_export(db)

    assert summary["tables"]["trader_predictions"]["rows"] == 2
    assert (tmp_path / "data_sync" / "league_state" / "trader_predictions.parquet").exists()


def test_import_is_a_noop_when_no_export_exists_yet(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "fresh.duckdb")
    conn = _conn(db)
    conn.close()

    summary = imp.run_import(db)

    assert summary["total_rows"] == 0
    assert all(not t["imported"] for t in summary["tables"].values())


def test_round_trip_fresh_insert(tmp_path, monkeypatch):
    """A -> export -> B import: B ends up with exactly A's rows."""
    monkeypatch.chdir(tmp_path)
    db_a = str(tmp_path / "a.duckdb")
    conn_a = _conn(db_a)
    pid = _record_call(conn_a)
    conn_a.close()

    exp.run_export(db_a)

    db_b = str(tmp_path / "b.duckdb")
    conn_b = _conn(db_b)
    conn_b.close()
    imp.run_import(db_b)

    conn_b = duckdb.connect(db_b, read_only=True)
    row = conn_b.execute(
        "SELECT direction, status FROM trader_predictions WHERE prediction_id = ?", [pid]
    ).fetchone()
    conn_b.close()
    assert row == ("up", "pending")


def test_round_trip_survives_an_update_not_just_the_original_insert(tmp_path, monkeypatch):
    """The whole point of this design (vs export_for_sync.py's anti-join):
    a call that gets confirmed/withdrawn AFTER its first export must show
    that mutation on the next export, not the stale original 'pending' row."""
    monkeypatch.chdir(tmp_path)
    db_a = str(tmp_path / "a.duckdb")
    conn_a = _conn(db_a)
    pid = _record_call(conn_a)
    conn_a.close()

    exp.run_export(db_a)  # first export: 'pending', not confirmed

    # Mutate: confirm the call (simulates the post_open decision point).
    conn_a = duckdb.connect(db_a)
    tp.confirm_call(conn_a, pid)
    conn_a.close()

    exp.run_export(db_a)  # second export: must overwrite, not append

    db_b = str(tmp_path / "b.duckdb")
    conn_b = _conn(db_b)
    conn_b.close()
    imp.run_import(db_b)

    conn_b = duckdb.connect(db_b, read_only=True)
    n = conn_b.execute("SELECT count(*) FROM trader_predictions").fetchone()[0]
    confirmed_at = conn_b.execute(
        "SELECT confirmed_at FROM trader_predictions WHERE prediction_id = ?", [pid]
    ).fetchone()[0]
    conn_b.close()

    assert n == 1  # overwritten, not duplicated
    assert confirmed_at is not None  # the update survived the round trip


def test_import_is_idempotent_re_running_does_not_duplicate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db_a = str(tmp_path / "a.duckdb")
    conn_a = _conn(db_a)
    _record_call(conn_a)
    conn_a.close()
    exp.run_export(db_a)

    db_b = str(tmp_path / "b.duckdb")
    conn_b = _conn(db_b)
    conn_b.close()
    imp.run_import(db_b)
    imp.run_import(db_b)  # re-run

    conn_b = duckdb.connect(db_b, read_only=True)
    n = conn_b.execute("SELECT count(*) FROM trader_predictions").fetchone()[0]
    conn_b.close()
    assert n == 1


def test_export_covers_every_league_table():
    """A quick regression guard: if a new League table gets added and someone
    forgets to add it here, this test should fail loudly rather than the
    table silently never syncing."""
    expected = {
        "trader_predictions", "trader_prediction_grades", "trader_portfolios",
        "trader_trades", "trader_review_log", "trader_divergence_log",
        "trader_method_versions", "trader_method_proposals",
    }
    assert set(exp.TABLES) == expected
