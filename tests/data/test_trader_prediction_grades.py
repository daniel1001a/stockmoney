"""Multi-horizon grading (issue #7 P1, CONTEXT.md's Grading Horizon): the same
Call independently graded at 1/5/21 trading days, alongside (not instead of)
its original single-horizon grade on trader_predictions itself."""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from stockmoney.data import trader_predictions as tp
from stockmoney.data import trader_prediction_grades as tpg
from stockmoney.data.db import run_migrations

TRADE_DATE = date(2026, 6, 1)


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _record(conn, *, trade_date=TRADE_DATE, symbol="SOXL", direction="up") -> str:
    return tp.record_trader_prediction(
        conn, trader_id="chartist", method_version="m", trade_date=trade_date,
        symbol=symbol, sector="semiconductor", horizon=5,
        label_end_date=date(2026, 6, 8), direction=direction, conviction=0.7,
        rationale="r", invalidation="i", entry_price=100.0, grade_vol=0.4,
        engine_payload={}, regime=0,
    )


def test_record_pending_grades_writes_one_row_per_horizon():
    conn = _conn()
    pid = _record(conn)
    tpg.record_pending_grades(conn, prediction_id=pid, trade_date=TRADE_DATE)

    rows = conn.execute(
        "SELECT horizon_days, status FROM trader_prediction_grades WHERE prediction_id = ? ORDER BY horizon_days",
        [pid],
    ).fetchall()
    assert [r[0] for r in rows] == [1, 5, 21]
    assert all(r[1] == "pending" for r in rows)


def test_record_pending_grades_is_idempotent():
    conn = _conn()
    pid = _record(conn)
    tpg.record_pending_grades(conn, prediction_id=pid, trade_date=TRADE_DATE)
    tpg.record_pending_grades(conn, prediction_id=pid, trade_date=TRADE_DATE)  # re-run, e.g. idempotent predict

    n = conn.execute(
        "SELECT count(*) FROM trader_prediction_grades WHERE prediction_id = ?", [pid]
    ).fetchone()[0]
    assert n == 3


def test_record_pending_grades_label_end_dates_scale_with_horizon():
    conn = _conn()
    pid = _record(conn)
    tpg.record_pending_grades(conn, prediction_id=pid, trade_date=TRADE_DATE)

    rows = dict(conn.execute(
        "SELECT horizon_days, label_end_date FROM trader_prediction_grades WHERE prediction_id = ?", [pid]
    ).fetchall())
    assert rows[1] < rows[5] < rows[21]


def test_list_pending_grades_only_returns_matured_rows():
    conn = _conn()
    pid = _record(conn)
    tpg.record_pending_grades(conn, prediction_id=pid, trade_date=TRADE_DATE)

    # As of the trade date itself, nothing has matured yet.
    assert tpg.list_pending_grades(conn, as_of=TRADE_DATE) == []

    # Far enough out, all three horizons have matured.
    pending = tpg.list_pending_grades(conn, as_of=date(2026, 7, 15))
    assert {p.horizon_days for p in pending} == {1, 5, 21}
    assert all(p.prediction_id == pid for p in pending)


def test_grade_horizon_computes_directional_outcome():
    conn = _conn()
    pid = _record(conn, direction="up")
    tpg.record_pending_grades(conn, prediction_id=pid, trade_date=TRADE_DATE)
    pending = tpg.list_pending_grades(conn, as_of=date(2026, 7, 15))
    one_day = next(p for p in pending if p.horizon_days == 1)

    tpg.grade_horizon(conn, prediction_id=pid, horizon_days=1, actual_price=130.0)

    row = conn.execute(
        "SELECT status, actual_price, actual_label, outcome FROM trader_prediction_grades "
        "WHERE prediction_id = ? AND horizon_days = 1",
        [pid],
    ).fetchone()
    assert row[0] == "graded"
    assert row[1] == 130.0
    assert row[2] == "up"  # +30% on entry_price=100 clears the band
    assert row[3] == "win"  # direction was 'up'
    assert one_day.label_end_date is not None  # sanity: the pending row had a real maturity date


def test_grade_horizon_is_idempotent_raises_on_double_grade():
    conn = _conn()
    pid = _record(conn)
    tpg.record_pending_grades(conn, prediction_id=pid, trade_date=TRADE_DATE)
    tpg.grade_horizon(conn, prediction_id=pid, horizon_days=1, actual_price=130.0)
    with pytest.raises(ValueError):
        tpg.grade_horizon(conn, prediction_id=pid, horizon_days=1, actual_price=130.0)


def test_graded_for_horizon_joins_direction_and_conviction():
    conn = _conn()
    pid = _record(conn, direction="up")
    tpg.record_pending_grades(conn, prediction_id=pid, trade_date=TRADE_DATE)
    tpg.grade_horizon(conn, prediction_id=pid, horizon_days=1, actual_price=130.0)
    tpg.grade_horizon(conn, prediction_id=pid, horizon_days=5, actual_price=95.0)  # still pending for 21

    graded_1d = tpg.graded_for_horizon(conn, horizon_days=1)
    assert len(graded_1d) == 1
    row = graded_1d[0]
    assert row.trader_id == "chartist"
    assert row.direction == "up"
    assert row.conviction == pytest.approx(0.7)
    assert row.horizon == 1
    assert row.outcome == "win"

    graded_5d = tpg.graded_for_horizon(conn, horizon_days=5)
    assert graded_5d[0].outcome == "loss"  # -5% is inside the range band, not 'up'

    # 21d never graded -> not included
    assert tpg.graded_for_horizon(conn, horizon_days=21) == []


def test_graded_for_horizon_filters_by_trader():
    conn = _conn()
    pid = _record(conn)
    tpg.record_pending_grades(conn, prediction_id=pid, trade_date=TRADE_DATE)
    tpg.grade_horizon(conn, prediction_id=pid, horizon_days=1, actual_price=130.0)

    assert len(tpg.graded_for_horizon(conn, horizon_days=1, trader_id="chartist")) == 1
    assert tpg.graded_for_horizon(conn, horizon_days=1, trader_id="analyst") == []
