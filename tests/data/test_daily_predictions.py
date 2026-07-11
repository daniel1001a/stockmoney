from datetime import date, timedelta

import duckdb
import pytest

from stockmoney.data.daily_predictions import (
    get_prediction,
    grade_prediction,
    list_pending_predictions,
    record_prediction,
    win_rate_history,
)
from stockmoney.data.db import run_migrations
from stockmoney.models.feature_matrix import DOWN, RANGE, UP

TRADE_DATE = date(2026, 7, 1)
LABEL_END_DATE = date(2026, 7, 8)


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _record(conn, *, proba, entry_price=100.0, rv=0.5, trade_date=TRADE_DATE, label_end_date=LABEL_END_DATE):
    return record_prediction(
        conn,
        trade_date=trade_date, symbol="soxl", sector="semiconductor", horizon=5,
        label_end_date=label_end_date, regime=0, proba=proba, entry_price=entry_price,
        feature_values={"realized_vol_20d": rv, "adx_14": 20.0, "xsec_dispersion": 0.01,
                         "yield_curve_10y2y": 1.0, "dxy_chg_1d": 0.0, "oil_chg_1d": 0.0},
        model_version="test-v1",
    )


def _proba_for(cls: int) -> tuple[float, float, float]:
    p = [0.1, 0.1, 0.1]
    p[cls] = 0.8
    return tuple(p)


def test_record_and_get_round_trips():
    conn = _conn()
    pid = _record(conn, proba=_proba_for(UP))
    p = get_prediction(conn, pid)

    assert p.symbol == "SOXL"
    assert p.predicted_direction == "up"
    assert p.status == "pending"
    assert p.target_price_up > p.entry_price > p.target_price_down
    assert p.feature_values["realized_vol_20d"] == 0.5


def test_record_prediction_is_idempotent_per_symbol_and_trade_date():
    conn = _conn()
    first_id = _record(conn, proba=_proba_for(UP))
    second_id = _record(conn, proba=_proba_for(DOWN))  # different call, same symbol+trade_date

    assert first_id == second_id
    count = conn.execute(
        "SELECT count(*) FROM daily_predictions WHERE symbol = 'SOXL' AND trade_date = ?", [TRADE_DATE]
    ).fetchone()[0]
    assert count == 1
    # The original prediction is preserved, not overwritten by the second call.
    assert get_prediction(conn, first_id).predicted_direction == "up"


def test_list_pending_excludes_not_yet_matured():
    conn = _conn()
    _record(conn, proba=_proba_for(UP), label_end_date=date(2099, 1, 1))
    pending = list_pending_predictions(conn, as_of=TRADE_DATE)
    assert pending == []


def test_list_pending_includes_matured_and_excludes_graded():
    conn = _conn()
    pid = _record(conn, proba=_proba_for(UP))
    pending = list_pending_predictions(conn, as_of=LABEL_END_DATE)
    assert [p.prediction_id for p in pending] == [pid]

    grade_prediction(conn, pid, actual_price=110.0)
    assert list_pending_predictions(conn, as_of=LABEL_END_DATE) == []


def test_grade_win_when_prediction_matches_actual_direction():
    conn = _conn()
    # rv=0.5 -> daily_vol ~= 0.0315, band = 0.5*0.0315*sqrt(5) ~= 0.0352 -> ~3.5% band
    pid = _record(conn, proba=_proba_for(UP), entry_price=100.0)
    grade_prediction(conn, pid, actual_price=110.0)  # +10%, well above the band -> up
    p = get_prediction(conn, pid)
    assert p.status == "graded"
    assert p.actual_label == "up"
    assert p.outcome == "win"
    assert p.actual_return == pytest.approx(0.10)


def test_grade_loss_when_prediction_contradicts_actual_direction():
    conn = _conn()
    pid = _record(conn, proba=_proba_for(UP), entry_price=100.0)
    grade_prediction(conn, pid, actual_price=90.0)  # -10% -> down, predicted up -> loss
    p = get_prediction(conn, pid)
    assert p.actual_label == "down"
    assert p.outcome == "loss"


def test_grade_loss_when_predicted_up_but_stayed_in_range():
    conn = _conn()
    pid = _record(conn, proba=_proba_for(UP), entry_price=100.0)
    grade_prediction(conn, pid, actual_price=100.5)  # +0.5%, inside the ~3.5% band -> range
    p = get_prediction(conn, pid)
    assert p.actual_label == "range"
    assert p.outcome == "loss"


def test_grade_range_prediction_wins_when_actual_is_also_range():
    conn = _conn()
    pid = _record(conn, proba=_proba_for(RANGE), entry_price=100.0)
    grade_prediction(conn, pid, actual_price=100.5)
    p = get_prediction(conn, pid)
    assert p.predicted_direction == "range"
    assert p.actual_label == "range"
    assert p.outcome == "win"


def test_grade_range_prediction_loses_when_actual_moved():
    conn = _conn()
    pid = _record(conn, proba=_proba_for(RANGE), entry_price=100.0)
    grade_prediction(conn, pid, actual_price=110.0)
    p = get_prediction(conn, pid)
    assert p.outcome == "loss"


def test_grade_unknown_id_raises():
    conn = _conn()
    with pytest.raises(ValueError):
        grade_prediction(conn, "not-a-real-id", actual_price=100.0)


def test_grade_already_graded_raises():
    conn = _conn()
    pid = _record(conn, proba=_proba_for(UP))
    grade_prediction(conn, pid, actual_price=110.0)
    with pytest.raises(ValueError):
        grade_prediction(conn, pid, actual_price=111.0)


def test_win_rate_history_excludes_range_and_ungraded():
    conn = _conn()
    up_win = _record(conn, proba=_proba_for(UP),
                      trade_date=date(2026, 7, 1), label_end_date=date(2026, 7, 8))
    down_loss = _record(conn, proba=_proba_for(DOWN),
                         trade_date=date(2026, 7, 2), label_end_date=date(2026, 7, 9))
    range_pred = _record(conn, proba=_proba_for(RANGE),
                          trade_date=date(2026, 7, 3), label_end_date=date(2026, 7, 10))
    still_pending = _record(conn, proba=_proba_for(UP),
                             trade_date=date(2026, 7, 4), label_end_date=date(2026, 7, 11))

    grade_prediction(conn, up_win, actual_price=110.0)     # win
    grade_prediction(conn, down_loss, actual_price=110.0)  # actual up, predicted down -> loss
    grade_prediction(conn, range_pred, actual_price=100.5) # graded but range -> excluded from history

    history = win_rate_history(conn)
    assert [outcome for _, outcome in history] == ["win", "loss"]
