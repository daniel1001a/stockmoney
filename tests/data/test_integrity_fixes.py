from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import duckdb
import pytest

from stockmoney.data import trader_predictions as tp
from stockmoney.data.db import run_migrations
from stockmoney.data.integrity_fixes import (
    nearest_friday,
    realign_trader_trades_expiries,
    run_all_strike_and_expiry_fixes,
    snap_option_positions_strikes,
    snap_trader_predictions_option_structures,
    snap_trader_trades_strikes,
)
from stockmoney.models.strike_ladder import is_on_ladder, snap_strike

NOW = datetime.now(timezone.utc)


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _insert_trade(
    conn, trade_id, *, strike, status="open", entry_at=None, exit_at=None,
    expiry_date=date(2026, 7, 24),
):
    entry_at = entry_at or datetime(2026, 6, 1, tzinfo=timezone.utc)
    conn.execute(
        """INSERT INTO trader_trades
        (trade_id, trader_id, symbol, option_right, side, strike, expiry_date, contracts,
         entry_at, entry_underlying, entry_premium, exit_at, exit_underlying, exit_premium,
         realized_pnl, status, thesis, exit_reason, linked_prediction_id, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            trade_id, "chartist", "NFLX", "call", "long", strike, expiry_date, 1,
            entry_at, 967.65, 20.0, exit_at, None, None, None, status, "t", None, None, NOW,
        ],
    )


def _insert_position(conn, position_id, *, strike):
    conn.execute(
        """INSERT INTO option_positions
        (position_id, symbol, option_right, side, strike, expiry_date, entry_date,
         entry_underlying_price, entry_premium, entry_iv, regime_at_entry, thesis_note,
         status, closed_at, closed_reason, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            position_id, "NVDA", "call", "long", strike, date(2026, 7, 31),
            date(2026, 6, 30), 212.03, 7.42, 0.6, 1, "note", "open", None, None, NOW,
        ],
    )


# --- snap_trader_trades_strikes --------------------------------------------

def test_snaps_off_ladder_strikes():
    conn = _conn()
    _insert_trade(conn, "t1", strike=996.7)
    n = snap_trader_trades_strikes(conn)
    assert n == 1
    got = conn.execute("SELECT strike FROM trader_trades WHERE trade_id='t1'").fetchone()[0]
    assert is_on_ladder(got)
    assert got == snap_strike(996.7)


def test_leaves_already_on_ladder_strikes_untouched():
    conn = _conn()
    _insert_trade(conn, "t1", strike=1000.0)  # already on the >=500 tier's $10 ladder
    n = snap_trader_trades_strikes(conn)
    assert n == 0
    assert conn.execute("SELECT strike FROM trader_trades WHERE trade_id='t1'").fetchone()[0] == 1000.0


def test_is_idempotent():
    conn = _conn()
    _insert_trade(conn, "t1", strike=996.7)
    snap_trader_trades_strikes(conn)
    n_second_pass = snap_trader_trades_strikes(conn)
    assert n_second_pass == 0


def test_does_not_touch_premium_or_pnl_columns():
    conn = _conn()
    _insert_trade(conn, "t1", strike=996.7, status="closed", exit_at=datetime(2026, 6, 10, tzinfo=timezone.utc))
    conn.execute("UPDATE trader_trades SET exit_premium=25.0, realized_pnl=500.0 WHERE trade_id='t1'")
    snap_trader_trades_strikes(conn)
    row = conn.execute(
        "SELECT entry_premium, exit_premium, realized_pnl FROM trader_trades WHERE trade_id='t1'"
    ).fetchone()
    assert row == (20.0, 25.0, 500.0)  # untouched


# --- snap_option_positions_strikes -----------------------------------------

def test_snaps_option_positions_strikes():
    conn = _conn()
    _insert_position(conn, "pos-0", strike=218.4)  # off the 100-250/$2.5 ladder
    n = snap_option_positions_strikes(conn)
    assert n == 1
    got = conn.execute("SELECT strike FROM option_positions WHERE position_id='pos-0'").fetchone()[0]
    assert is_on_ladder(got)


# --- snap_trader_predictions_option_structures -----------------------------

def _record_prediction(conn, *, symbol="GS", structure, graded=False):
    pid = tp.record_trader_prediction(
        conn, trader_id="chartist", method_version="m", trade_date=date(2026, 6, 1), symbol=symbol,
        sector="financials", horizon=5, label_end_date=date(2026, 6, 8), direction="up",
        conviction=0.7, rationale="r", invalidation="i", entry_price=structure["spot"], grade_vol=0.4,
        engine_payload={}, option_structure=structure,
    )
    if graded:
        tp.set_option_pnl(conn, pid, option_pnl=0.42)
    return pid


def test_snaps_off_ladder_strike_inside_option_structure_json():
    conn = _conn()
    structure = {
        "spot": 1055.18, "strike": 1059.3919626869124, "is_call": True, "iv": 0.4,
        "iv_source": "proxy", "t_years": 30 / 365, "dte_days": 30, "side": "long",
    }
    pid = _record_prediction(conn, structure=structure)
    n, skipped = snap_trader_predictions_option_structures(conn)
    assert n == 1
    assert skipped == []
    patched = tp.get_trader_prediction(conn, pid).option_structure
    assert is_on_ladder(patched["strike"])
    assert patched["spot"] == 1055.18  # only strike changed


def test_skips_and_reports_already_graded_rows_instead_of_mutating():
    conn = _conn()
    structure = {
        "spot": 1055.18, "strike": 1059.3919626869124, "is_call": True, "iv": 0.4,
        "iv_source": "proxy", "t_years": 30 / 365, "dte_days": 30, "side": "long",
    }
    pid = _record_prediction(conn, structure=structure, graded=True)
    n, skipped = snap_trader_predictions_option_structures(conn)
    assert n == 0
    assert skipped == [pid]
    untouched = tp.get_trader_prediction(conn, pid).option_structure
    assert untouched["strike"] == pytest.approx(1059.3919626869124)  # not silently mutated


# --- nearest_friday / realign_trader_trades_expiries -----------------------

@pytest.mark.parametrize(
    "d,expected",
    [
        (date(2026, 7, 4), date(2026, 7, 3)),   # Saturday -> nearest Friday is the day before
        (date(2026, 6, 1), date(2026, 5, 29)),  # Monday -> nearest Friday (3 days back beats 4 forward)
        (date(2026, 7, 24), date(2026, 7, 24)), # already a Friday
    ],
)
def test_nearest_friday_examples(d, expected):
    assert nearest_friday(d) == expected


def test_nearest_friday_respects_on_or_after_floor():
    # 2026-07-04 is a Saturday; nearest Friday alone is 2026-07-03 (in the
    # past relative to a 2026-07-04 floor), so it must walk forward a week.
    result = nearest_friday(date(2026, 7, 4), on_or_after=date(2026, 7, 4))
    assert result >= date(2026, 7, 4)
    assert result.weekday() == 4


def test_realign_trader_trades_expiries_fixes_non_friday_dates():
    conn = _conn()
    _insert_trade(conn, "t1", strike=1000.0, expiry_date=date(2026, 7, 4))  # Saturday
    n = realign_trader_trades_expiries(conn, as_of=date(2026, 6, 1))
    assert n == 1
    got = conn.execute("SELECT expiry_date FROM trader_trades WHERE trade_id='t1'").fetchone()[0]
    assert got.weekday() == 4


def test_realign_never_expires_an_open_trade_before_as_of():
    conn = _conn()
    _insert_trade(conn, "t1", strike=1000.0, status="open", expiry_date=date(2026, 6, 3))  # Wed, in the past
    n = realign_trader_trades_expiries(conn, as_of=date(2026, 7, 15))
    assert n == 1
    got = conn.execute("SELECT expiry_date FROM trader_trades WHERE trade_id='t1'").fetchone()[0]
    assert got >= date(2026, 7, 15)
    assert got.weekday() == 4


def test_realign_never_moves_a_closed_trades_expiry_before_its_exit():
    conn = _conn()
    _insert_trade(
        conn, "t1", strike=1000.0, status="closed",
        exit_at=datetime(2026, 6, 20, tzinfo=timezone.utc), expiry_date=date(2026, 6, 17),  # Wed
    )
    realign_trader_trades_expiries(conn)
    got = conn.execute("SELECT expiry_date FROM trader_trades WHERE trade_id='t1'").fetchone()[0]
    assert got >= date(2026, 6, 20)
    assert got.weekday() == 4


def test_realign_is_idempotent():
    conn = _conn()
    _insert_trade(conn, "t1", strike=1000.0, expiry_date=date(2026, 7, 4))
    realign_trader_trades_expiries(conn, as_of=date(2026, 6, 1))
    n_second_pass = realign_trader_trades_expiries(conn, as_of=date(2026, 6, 1))
    assert n_second_pass == 0


# --- run_all_strike_and_expiry_fixes ---------------------------------------

def test_run_all_returns_a_summary_and_is_idempotent():
    conn = _conn()
    _insert_trade(conn, "t1", strike=996.7, expiry_date=date(2026, 7, 4))
    _insert_position(conn, "pos-0", strike=218.4)
    summary1 = run_all_strike_and_expiry_fixes(conn)
    assert summary1["trader_trades_strikes_snapped"] == 1
    assert summary1["option_positions_strikes_snapped"] == 1
    assert summary1["trader_trades_expiries_realigned"] == 1
    summary2 = run_all_strike_and_expiry_fixes(conn)
    assert summary2["trader_trades_strikes_snapped"] == 0
    assert summary2["option_positions_strikes_snapped"] == 0
    assert summary2["trader_trades_expiries_realigned"] == 0
