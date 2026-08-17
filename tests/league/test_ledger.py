from __future__ import annotations

from datetime import date

import duckdb
import pytest

from stockmoney.data import trader_predictions as tp
from stockmoney.data.db import run_migrations
from stockmoney.league import ledger
from stockmoney.league.grading_options import grade_option_pnl
from stockmoney.models.options_pnl import DEFAULT_SPREAD_PCT, OptionEntry, OptionExit, entry_premium, exit_premium

TRADE_DATE = date(2026, 6, 1)
END = date(2026, 6, 8)


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _structure(spot=100.0, strike=110.0, iv=0.45, dte_days=30):
    return {
        "spot": spot, "strike": strike, "is_call": True, "iv": iv, "iv_source": "proxy",
        "t_years": dte_days / 365, "dte_days": dte_days, "side": "long",
    }


def _record(conn, *, direction="up", option_structure=None, symbol="NVDA"):
    pid = tp.record_trader_prediction(
        conn, trader_id="t1", method_version="m", trade_date=TRADE_DATE, symbol=symbol,
        sector="semiconductor", horizon=5, label_end_date=END, direction=direction,
        conviction=0.7, rationale="regime says up", invalidation="i", entry_price=100.0, grade_vol=0.4,
        engine_payload={}, option_structure=option_structure,
    )
    return tp.get_trader_prediction(conn, pid)


def test_no_option_structure_books_nothing():
    conn = _conn()
    prediction = _record(conn, direction="range", option_structure=None)
    assert ledger.open_trade(conn, prediction) is None
    assert conn.execute("SELECT count(*) FROM trader_trades").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM trader_portfolios").fetchone()[0] == 0


def test_open_trade_debits_cash_by_the_computed_fill():
    conn = _conn()
    structure = _structure()
    prediction = _record(conn, option_structure=structure)

    trade_id = ledger.open_trade(conn, prediction)
    assert trade_id is not None

    entry = OptionEntry(spot=structure["spot"], strike=structure["strike"], is_call=True,
                         iv=structure["iv"], t_years=structure["t_years"], side="long")
    mid = entry_premium(entry)
    expected_fill = mid * (1.0 + DEFAULT_SPREAD_PCT / 2.0)
    scale = ledger.position_size_scale(prediction.conviction)
    expected_contracts = int((ledger.STARTING_CAPITAL * ledger.MAX_POSITION_PCT * scale) // (expected_fill * 100))
    assert expected_contracts >= 1  # sanity: fixture must actually afford a contract

    row = conn.execute(
        "SELECT contracts, entry_premium, status, linked_prediction_id FROM trader_trades WHERE trade_id = ?",
        [trade_id],
    ).fetchone()
    assert row[0] == expected_contracts
    assert row[1] == pytest.approx(expected_fill, abs=1e-3)
    assert row[2] == "open"
    assert row[3] == prediction.prediction_id

    cash = conn.execute("SELECT cash FROM trader_portfolios WHERE trader_id = 't1'").fetchone()[0]
    assert cash == pytest.approx(ledger.STARTING_CAPITAL - expected_contracts * expected_fill * 100)


def test_open_trade_stamps_quote_lag():
    """Issue #7 P1: every real fill is honestly marked with the ~15min
    yfinance quote lag it was actually priced off of."""
    conn = _conn()
    prediction = _record(conn, option_structure=_structure())
    trade_id = ledger.open_trade(conn, prediction)
    lag = conn.execute(
        "SELECT quote_lag_minutes FROM trader_trades WHERE trade_id = ?", [trade_id]
    ).fetchone()[0]
    assert lag == ledger.QUOTE_LAG_MINUTES == 15


def test_open_trade_expiry_is_always_a_friday():
    # dte_days=30 from TRADE_DATE (2026-06-01, a Monday) lands on 2026-07-01,
    # a Wednesday -- regression test for the 2026-08-14 incident where raw
    # trade_date + dte_days arithmetic booked non-Friday expiries, which
    # stockmoney-refresh-live's source-DB validation rejects wholesale.
    conn = _conn()
    prediction = _record(conn, option_structure=_structure(dte_days=30))
    trade_id = ledger.open_trade(conn, prediction)
    expiry = conn.execute("SELECT expiry_date FROM trader_trades WHERE trade_id = ?", [trade_id]).fetchone()[0]
    assert expiry.weekday() == 4
    assert expiry >= TRADE_DATE


def test_open_trade_skips_when_account_has_almost_no_cash():
    conn = _conn()
    prediction = _record(conn, option_structure=_structure())
    ledger.ensure_portfolio(conn, "t1", TRADE_DATE)
    conn.execute("UPDATE trader_portfolios SET cash = 0.50 WHERE trader_id = 't1'")

    # Not enough cash left for even one contract -- must record no trade,
    # not raise or drive cash negative.
    assert ledger.open_trade(conn, prediction) is None
    assert conn.execute("SELECT count(*) FROM trader_trades").fetchone()[0] == 0
    cash = conn.execute("SELECT cash FROM trader_portfolios WHERE trader_id = 't1'").fetchone()[0]
    assert cash == pytest.approx(0.50)


def test_open_trade_is_idempotent():
    conn = _conn()
    prediction = _record(conn, option_structure=_structure())
    first = ledger.open_trade(conn, prediction)
    second = ledger.open_trade(conn, prediction)
    assert first is not None
    assert second is None
    assert conn.execute("SELECT count(*) FROM trader_trades").fetchone()[0] == 1


def test_close_trade_credits_cash_and_matches_manual_repricing():
    conn = _conn()
    structure = _structure()
    prediction = _record(conn, option_structure=structure)
    ledger.open_trade(conn, prediction)

    EXIT_SPOT = 130.0
    tp.grade_trader_prediction(conn, prediction.prediction_id, actual_price=EXIT_SPOT)
    days_held = (END - TRADE_DATE).days
    grade_option_pnl(conn, prediction.prediction_id, exit_spot=EXIT_SPOT, days_held=days_held)
    graded = tp.get_trader_prediction(conn, prediction.prediction_id)

    cash_before = conn.execute("SELECT cash FROM trader_portfolios WHERE trader_id = 't1'").fetchone()[0]
    trade_id = ledger.close_trade(conn, graded)
    assert trade_id is not None

    entry = OptionEntry(spot=structure["spot"], strike=structure["strike"], is_call=True,
                         iv=structure["iv"], t_years=structure["t_years"], side="long")
    mid_exit = exit_premium(entry, OptionExit(spot=EXIT_SPOT, iv=None, days_held=max(days_held, 1)))
    expected_fill = mid_exit * (1.0 - DEFAULT_SPREAD_PCT / 2.0)

    row = conn.execute(
        "SELECT exit_premium, realized_pnl, status, contracts, entry_premium FROM trader_trades WHERE trade_id = ?",
        [trade_id],
    ).fetchone()
    exit_prem, realized_pnl, status, contracts, entry_fill = row
    assert status == "closed"
    assert exit_prem == pytest.approx(expected_fill, abs=1e-3)
    assert realized_pnl == pytest.approx((expected_fill - entry_fill) * 100 * contracts, abs=0.5)
    assert realized_pnl > 0  # big favorable move -> the long call should be profitable

    cash_after = conn.execute("SELECT cash FROM trader_portfolios WHERE trader_id = 't1'").fetchone()[0]
    assert cash_after == pytest.approx(cash_before + expected_fill * 100 * contracts, abs=0.5)


def test_close_trade_noop_when_nothing_was_opened():
    conn = _conn()
    prediction = _record(conn, direction="range", option_structure=None)
    tp.grade_trader_prediction(conn, prediction.prediction_id, actual_price=105.0)
    graded = tp.get_trader_prediction(conn, prediction.prediction_id)
    assert ledger.close_trade(conn, graded) is None
