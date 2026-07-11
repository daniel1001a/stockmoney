"""Tests for the daily attribution / review engine (stockmoney.data.attribution).

Covers: the factor-decomposition additivity identity, the three verdict
classes, that a feature candidate is proposed only for wrong_signal_existed,
idempotency, injection safety, and the discretion/meta isolation canary
(no training path reads attribution_log / feature_candidates).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import pytest

from stockmoney.data import attribution as A
from stockmoney.data.attribution import (
    RIGHT_REASON_RIGHT,
    WRONG_NOISE,
    WRONG_SIGNAL_EXISTED,
    decompose_return,
    record_attribution,
    run_attribution,
)
from stockmoney.data.daily_predictions import get_prediction, grade_prediction, record_prediction
from stockmoney.data.db import run_migrations
from stockmoney.data.features.dispersion import SECTOR_MEMBERS

START = date(2026, 6, 1)
END = date(2026, 6, 8)  # 5 weekday trading days after START (weekend-skipping like production)
FLAT_MEMBER_START = 100.0
FLAT_MEMBER_END = 100.5  # ~+0.5% across every member -> tiny, ~flat market/sector


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _put_price(conn, symbol, trade_date, close):
    conn.execute(
        "INSERT INTO ohlcv_daily (symbol, trade_date, close, source, ingested_at) VALUES (?, ?, ?, 'test', ?)",
        [symbol, trade_date, close, datetime.now(timezone.utc)],
    )


def _seed_flat_members(conn):
    """Every single-name market/sector member moves ~+0.5% over the window, so
    macro and sector contributions are near zero and a target's move is almost
    all idiosyncratic."""
    for m in A.MARKET_MEMBERS:
        _put_price(conn, m, START, FLAT_MEMBER_START)
        _put_price(conn, m, END, FLAT_MEMBER_END)


def _graded_prediction(conn, *, symbol, sector, proba, entry, exit_price, realized_vol=0.4):
    """Create + grade one prediction. `proba` is (down, range, up)."""
    _put_price(conn, symbol, START, entry)
    _put_price(conn, symbol, END, exit_price)
    pid = record_prediction(
        conn,
        trade_date=START, symbol=symbol, sector=sector, horizon=5,
        label_end_date=END, regime=0, proba=proba, entry_price=entry,
        feature_values={"realized_vol_20d": realized_vol}, model_version="test-v1",
    )
    grade_prediction(conn, pid, actual_price=exit_price)
    return get_prediction(conn, pid)


# --- factor decomposition ---------------------------------------------------

def test_decomposition_is_additive():
    conn = _conn()
    _seed_flat_members(conn)
    # SOXL is not a sector member, so its move is cleanly idiosyncratic.
    pred = _graded_prediction(conn, symbol="SOXL", sector="semiconductor",
                              proba=(0.1, 0.1, 0.8), entry=100.0, exit_price=70.0)
    decomp = decompose_return(conn, pred)
    assert decomp is not None
    total = decomp.attr_macro + decomp.attr_sector + decomp.attr_idiosyncratic
    assert total == pytest.approx(pred.actual_return, abs=1e-9)
    # A -30% SOXL move against flat members is dominated by idiosyncratic.
    assert abs(decomp.attr_idiosyncratic) > abs(decomp.attr_macro) + abs(decomp.attr_sector)


# --- verdict classification -------------------------------------------------

def test_verdict_right_reason_right_on_win():
    conn = _conn()
    _seed_flat_members(conn)
    # Predicted range, actual stays within band -> range -> win.
    pred = _graded_prediction(conn, symbol="SOXL", sector="semiconductor",
                              proba=(0.2, 0.6, 0.2), entry=100.0, exit_price=100.5)
    assert pred.outcome == "win"
    assert record_attribution(conn, pred) == RIGHT_REASON_RIGHT


def test_verdict_wrong_noise_when_move_stays_in_band():
    conn = _conn()
    _seed_flat_members(conn)
    # Predicted up, but actual move is small (within band) -> actual_label range
    # -> a directional miss on what was really just noise.
    pred = _graded_prediction(conn, symbol="SOXL", sector="semiconductor",
                              proba=(0.1, 0.2, 0.7), entry=100.0, exit_price=100.6)
    assert pred.predicted_direction == "up"
    assert pred.actual_label == "range"
    assert record_attribution(conn, pred) == WRONG_NOISE


def test_verdict_wrong_signal_existed_on_idio_dominated_miss():
    conn = _conn()
    _seed_flat_members(conn)
    # Predicted up, but SOXL fell 30% against flat members -> real directional
    # miss dominated by idiosyncratic residual.
    pred = _graded_prediction(conn, symbol="SOXL", sector="semiconductor",
                              proba=(0.1, 0.1, 0.8), entry=100.0, exit_price=70.0)
    assert pred.predicted_direction == "up"
    assert pred.actual_label == "down"
    assert record_attribution(conn, pred) == WRONG_SIGNAL_EXISTED


# --- feature candidate emission --------------------------------------------

def test_candidate_proposed_only_for_wrong_signal_existed():
    conn = _conn()
    _seed_flat_members(conn)
    pred = _graded_prediction(conn, symbol="SOXL", sector="semiconductor",
                              proba=(0.1, 0.1, 0.8), entry=100.0, exit_price=70.0)
    record_attribution(conn, pred)
    rows = conn.execute(
        "SELECT proposed_feature_name, status, source_attribution_date FROM feature_candidates"
    ).fetchall()
    assert rows == [("idio_signal::SOXL", "proposed", START)]


def test_no_candidate_for_win_or_noise():
    conn = _conn()
    _seed_flat_members(conn)
    win = _graded_prediction(conn, symbol="SOXL", sector="semiconductor",
                             proba=(0.2, 0.6, 0.2), entry=100.0, exit_price=100.5)
    noise = _graded_prediction(conn, symbol="NVDA", sector="semiconductor",
                               proba=(0.1, 0.2, 0.7), entry=100.0, exit_price=100.6)
    record_attribution(conn, win)
    record_attribution(conn, noise)
    assert conn.execute("SELECT count(*) FROM feature_candidates").fetchone()[0] == 0


# --- idempotency ------------------------------------------------------------

def test_run_attribution_is_idempotent():
    conn = _conn()
    _seed_flat_members(conn)
    _graded_prediction(conn, symbol="SOXL", sector="semiconductor",
                       proba=(0.1, 0.1, 0.8), entry=100.0, exit_price=70.0)
    first = run_attribution(conn)
    assert first[WRONG_SIGNAL_EXISTED] == 1
    assert first["candidates_proposed"] == 1
    n_log = conn.execute("SELECT count(*) FROM attribution_log").fetchone()[0]
    n_cand = conn.execute("SELECT count(*) FROM feature_candidates").fetchone()[0]

    second = run_attribution(conn)  # re-run must not duplicate
    assert second["skipped"] == 1
    assert second["candidates_proposed"] == 0
    assert conn.execute("SELECT count(*) FROM attribution_log").fetchone()[0] == n_log
    assert conn.execute("SELECT count(*) FROM feature_candidates").fetchone()[0] == n_cand


def test_pending_predictions_are_not_attributed():
    conn = _conn()
    _seed_flat_members(conn)
    _put_price(conn, "SOXL", START, 100.0)
    pid = record_prediction(
        conn, trade_date=START, symbol="SOXL", sector="semiconductor", horizon=5,
        label_end_date=END, regime=0, proba=(0.1, 0.1, 0.8), entry_price=100.0,
        feature_values={"realized_vol_20d": 0.4}, model_version="test-v1",
    )
    pred = get_prediction(conn, pid)
    assert pred.status == "pending"
    assert record_attribution(conn, pred) is None
    assert conn.execute("SELECT count(*) FROM attribution_log").fetchone()[0] == 0


# --- injection safety -------------------------------------------------------

def test_malicious_symbol_stored_as_data_only():
    conn = _conn()
    _seed_flat_members(conn)
    evil = "SOXL'; DROP TABLE feature_candidates; --"
    pred = _graded_prediction(conn, symbol=evil, sector="semiconductor",
                              proba=(0.1, 0.1, 0.8), entry=100.0, exit_price=70.0)
    record_attribution(conn, pred)
    # Tables intact, and the payload is stored verbatim as data.
    assert conn.execute("SELECT count(*) FROM feature_candidates").fetchone()[0] == 1
    name = conn.execute("SELECT proposed_feature_name FROM feature_candidates").fetchone()[0]
    assert name == f"idio_signal::{evil.upper()}"
    # attribution_log also survived and stored the symbol as data.
    logged = conn.execute("SELECT symbol FROM attribution_log").fetchone()[0]
    assert logged == evil.upper()


# --- discretion/meta isolation canary --------------------------------------

def test_training_paths_never_read_attribution_tables():
    """CLAUDE.md sections 7/11: attribution_log / feature_candidates are a
    read-only side-branch and must never feed the model. Guard against a
    future edit wiring them into any training/inference path."""
    src = Path(__file__).resolve().parents[2] / "src" / "stockmoney" / "models"
    training_files = ["feature_matrix.py", "walk_forward.py", "regime.py",
                      "direction.py", "production.py"]
    for name in training_files:
        text = (src / name).read_text()
        assert "attribution_log" not in text, f"{name} must not read attribution_log"
        assert "feature_candidates" not in text, f"{name} must not read feature_candidates"
