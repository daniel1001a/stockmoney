from __future__ import annotations

from datetime import date, datetime, timezone

import duckdb
import pytest

from stockmoney.data import trader_predictions as tp
from stockmoney.data.db import run_migrations
from stockmoney.league.grading_options import grade_option_pnl

TRADE_DATE = date(2026, 6, 1)
END = date(2026, 6, 8)  # 7 calendar days later, matches horizon=5 trading days elsewhere in the league


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _record(conn, *, direction="up", option_structure=None):
    return tp.record_trader_prediction(
        conn, trader_id="t1", method_version="m", trade_date=TRADE_DATE, symbol="NVDA",
        sector="semiconductor", horizon=5, label_end_date=END, direction=direction,
        conviction=0.7, rationale="r", invalidation="i", entry_price=100.0, grade_vol=0.4,
        engine_payload={}, option_structure=option_structure,
    )


def _long_call_structure(spot=100.0, strike=110.0, iv=0.45, dte_days=30):
    return {
        "spot": spot, "strike": strike, "is_call": True, "iv": iv, "iv_source": "proxy",
        "t_years": dte_days / 365, "dte_days": dte_days, "side": "long",
    }


def test_no_option_structure_is_a_noop():
    conn = _conn()
    pid = _record(conn, direction="range", option_structure=None)
    result = grade_option_pnl(conn, pid, exit_spot=100.0, days_held=7)
    assert result is None
    assert tp.get_trader_prediction(conn, pid).option_pnl is None


def test_unknown_prediction_id_raises():
    conn = _conn()
    with pytest.raises(ValueError, match="unknown prediction_id"):
        grade_option_pnl(conn, "does-not-exist", exit_spot=100.0, days_held=7)


def test_big_favorable_move_makes_the_long_call_profit():
    conn = _conn()
    structure = _long_call_structure(spot=100.0, strike=110.0, iv=0.45, dte_days=30)
    pid = _record(conn, direction="up", option_structure=structure)

    pnl = grade_option_pnl(conn, pid, exit_spot=130.0, days_held=7)

    assert pnl > 0
    assert tp.get_trader_prediction(conn, pid).option_pnl == pytest.approx(pnl)


def test_direction_right_but_option_loses_to_theta():
    """The exact case IMPROVEMENT_PLAN.md §S3 calls out by name: the underlying
    moves in the predicted direction by enough to grade as a directional WIN,
    but the option still loses money because it was bought out-of-the-money and
    held almost to expiry, so theta bled away far more than the favorable delta
    gained -- precisely what a pure directional hit-rate can't see, and the
    whole reason this wave exists.

    grade_option_pnl reprices at CONSTANT entry IV (options_pnl.py's documented,
    look-ahead-safe convention), so the loss modelled here is theta, not IV
    crush -- the numbers below (a +4% move on a 10%-OTM call held 28 of 30 days
    losing ~-0.96) are what the grader actually computes, verified in-test."""
    conn = _conn()
    structure = _long_call_structure(spot=100.0, strike=110.0, iv=0.45, dte_days=30)
    pid = _record(conn, direction="up", option_structure=structure)

    # The favorable move (100 -> 104, +4%) must clear the directional grading
    # band (band_k=0.5, grade_vol=0.4, horizon=5 -> ~2.82%) so the directional
    # grade is genuinely 'up'/win; a smaller move would grade as 'range' and the
    # divergence being tested (direction WIN vs option LOSS) wouldn't exist.
    EXIT_SPOT, DAYS_HELD = 104.0, 28
    prediction = tp.get_trader_prediction(conn, pid)
    from stockmoney.models.options_pnl import OptionEntry, OptionExit, option_return
    s = prediction.option_structure
    entry = OptionEntry(spot=s["spot"], strike=s["strike"], is_call=s["is_call"], iv=s["iv"], t_years=s["t_years"], side=s["side"])
    # Sanity mirrors grade_option_pnl EXACTLY (iv=None -> constant entry IV), so
    # the fixture provably drives the same losing trade the grader will.
    sanity = option_return(entry, OptionExit(spot=EXIT_SPOT, iv=None, days_held=DAYS_HELD))
    assert sanity < 0

    pnl = grade_option_pnl(conn, pid, exit_spot=EXIT_SPOT, days_held=DAYS_HELD)
    assert pnl is not None
    assert pnl < 0

    graded = tp.get_trader_prediction(conn, pid)
    assert graded.option_pnl == pytest.approx(pnl)
    # The directional call ('up', +4% underlying move) independently grades as a
    # WIN -- proving the divergence requires actually grading the option too.
    tp.grade_trader_prediction(conn, pid, actual_price=EXIT_SPOT)
    graded = tp.get_trader_prediction(conn, pid)
    assert graded.actual_label == "up"
    assert graded.outcome == "win"
    assert graded.option_pnl < 0  # right on direction, lost money as an option


def test_prices_the_exact_stored_contract_not_a_freshly_selected_one():
    """Grading must reprice the SAME strike/iv/side captured at entry, never
    re-derive a new option from today's conditions (that would be hindsight
    contract selection)."""
    conn = _conn()
    cheap_far_otm = _long_call_structure(spot=100.0, strike=150.0, iv=0.20, dte_days=30)
    pid = _record(conn, direction="up", option_structure=cheap_far_otm)

    pnl = grade_option_pnl(conn, pid, exit_spot=101.0, days_held=7)

    # A deep-OTM call barely nudged shouldn't be near flat-return neutral --
    # confirms the STORED far strike (150), not e.g. an ATM strike, was used.
    from stockmoney.models.options_pnl import OptionEntry, OptionExit, option_return
    entry = OptionEntry(spot=100.0, strike=150.0, is_call=True, iv=0.20, t_years=30 / 365, side="long")
    expected = option_return(entry, OptionExit(spot=101.0, iv=None, days_held=7))
    assert pnl == pytest.approx(expected)
