from __future__ import annotations

from datetime import date

import duckdb
import pytest

from stockmoney.data.db import run_migrations
from stockmoney.data import trader_predictions as tp

START = date(2026, 6, 1)
END = date(2026, 6, 8)


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _record(conn, *, trader_id="chartist", symbol="SOXL", direction="up", conviction=0.7,
            entry=100.0, grade_vol=0.4, regime=0, trade_date=START):
    return tp.record_trader_prediction(
        conn, trader_id=trader_id, method_version="m-v1", trade_date=trade_date,
        symbol=symbol, sector="semiconductor", horizon=5, label_end_date=END,
        direction=direction, conviction=conviction, rationale="r", invalidation="i",
        entry_price=entry, grade_vol=grade_vol, engine_payload={"k": 1}, regime=regime,
    )


def test_record_is_idempotent_per_trader_symbol_day():
    conn = _conn()
    a = _record(conn)
    b = _record(conn, direction="down", conviction=0.9)  # same trader/symbol/day
    assert a == b
    assert conn.execute("SELECT count(*) FROM trader_predictions").fetchone()[0] == 1
    # the stored row keeps the first call, not the second
    assert tp.get_trader_prediction(conn, a).direction == "up"


def test_two_traders_same_symbol_day_are_distinct_rows():
    conn = _conn()
    _record(conn, trader_id="chartist")
    _record(conn, trader_id="analyst")
    assert conn.execute("SELECT count(*) FROM trader_predictions").fetchone()[0] == 2


def test_invalid_direction_rejected():
    conn = _conn()
    with pytest.raises(ValueError):
        _record(conn, direction="sideways")


def test_all_traders_graded_on_one_identical_ruler():
    """The core fairness property: given the same (entry, grade_vol, horizon),
    the band label is identical regardless of trader, so a matching direction
    is a win for both and a mismatching one is a loss for both."""
    conn = _conn()
    # +30% move over the window. band = 0.5 * (0.4/sqrt(252)) * sqrt(5) ~= 2.8%.
    chart = _record(conn, trader_id="chartist", direction="up")
    ana = _record(conn, trader_id="analyst", direction="down")
    tp.grade_trader_prediction(conn, chart, actual_price=130.0)
    tp.grade_trader_prediction(conn, ana, actual_price=130.0)
    c = tp.get_trader_prediction(conn, chart)
    a = tp.get_trader_prediction(conn, ana)
    assert c.actual_label == a.actual_label == "up"   # same market fact
    assert c.outcome == "win" and a.outcome == "loss"  # different calls, one ruler


def test_range_call_wins_only_when_actual_stays_in_band():
    conn = _conn()
    pid = _record(conn, direction="range")
    tp.grade_trader_prediction(conn, pid, actual_price=100.5)  # +0.5%, inside band
    assert tp.get_trader_prediction(conn, pid).outcome == "win"


def test_double_grade_rejected():
    conn = _conn()
    pid = _record(conn)
    tp.grade_trader_prediction(conn, pid, actual_price=130.0)
    with pytest.raises(ValueError):
        tp.grade_trader_prediction(conn, pid, actual_price=130.0)


def test_pending_listing_respects_maturity():
    conn = _conn()
    _record(conn)
    assert tp.list_pending_trader_predictions(conn, as_of=date(2026, 6, 7)) == []  # not matured
    assert len(tp.list_pending_trader_predictions(conn, as_of=END)) == 1


def test_engine_payload_roundtrips_as_dict():
    conn = _conn()
    pid = _record(conn)
    p = tp.get_trader_prediction(conn, pid)
    assert p.engine_payload == {"k": 1}
