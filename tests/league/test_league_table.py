from __future__ import annotations

from datetime import date

import duckdb
import pytest

from stockmoney.data.db import run_migrations
from stockmoney.data import trader_predictions as tp
from stockmoney.data.trader_predictions import TraderPrediction
from stockmoney.league.league_table import compute_stats, equity_curve, league_equity_curves, league_table


def _p(
    direction, conviction, outcome, actual_return, regime, *,
    status="graded", option_pnl=None, trade_date=date(2026, 6, 1), symbol="S",
) -> TraderPrediction:
    return TraderPrediction(
        prediction_id="x", trader_id="t", method_version="m", trade_date=trade_date,
        symbol=symbol, sector="semiconductor", horizon=5, label_end_date=date(2026, 6, 8),
        direction=direction, conviction=conviction, rationale="r", invalidation="i",
        entry_price=100.0, band_k=0.5, grade_vol=0.4, regime=regime, status=status,
        actual_return=actual_return, outcome=outcome, option_pnl=option_pnl,
    )


def test_compute_stats_math():
    preds = [
        _p("up", 0.8, "win", 0.10, 0),
        _p("down", 0.7, "loss", 0.05, 0),
        _p("up", 0.4, "loss", -0.03, 1),
        _p("range", 0.9, "win", 0.00, 1),
    ]
    s = compute_stats(preds, high_conviction=0.6)
    assert s["n_graded"] == 4 and s["n_directional"] == 3
    assert s["hit_rate"] == pytest.approx(1 / 3)
    assert s["brier"] == pytest.approx((0.04 + 0.49 + 0.16 + 0.01) / 4)
    # signed pnl: up +0.10, down -(+0.05), up -0.03 -> mean 0.02/3
    assert s["avg_pnl"] == pytest.approx(0.02 / 3)
    # high-conviction (>=0.6): 0.8 win, 0.7 loss, 0.9 win -> 2/3
    assert s["high_conviction_n"] == 3
    assert s["high_conviction_precision"] == pytest.approx(2 / 3)


def test_compute_stats_empty_is_honest_not_crash():
    s = compute_stats([])
    assert s["n_graded"] == 0
    assert s["hit_rate"] is None and s["brier"] is None and s["avg_pnl"] is None
    assert s["cum_pnl"] == 0.0


def test_pnl_cost_bps_applied():
    s = compute_stats([_p("up", 0.7, "win", 0.10, 0)], cost_bps=50)  # 50bps = 0.005
    assert s["avg_pnl"] == pytest.approx(0.10 - 0.005)


def test_compute_stats_option_pnl_honest_empty_when_no_option_graded():
    preds = [_p("up", 0.7, "win", 0.10, 0)]  # option_pnl=None (no structure that day)
    s = compute_stats(preds)
    assert s["n_option_graded"] == 0
    assert s["option_win_rate"] is None
    assert s["avg_option_pnl"] is None
    assert s["cum_option_pnl"] == 0.0
    assert s["directional_win_option_loss_n"] == 0


def test_compute_stats_option_pnl_math():
    preds = [
        _p("up", 0.8, "win", 0.10, 0, option_pnl=0.5),
        _p("down", 0.7, "loss", 0.05, 0, option_pnl=-0.3),
        _p("up", 0.4, "loss", -0.03, 1, option_pnl=-0.9),
    ]
    s = compute_stats(preds)
    assert s["n_option_graded"] == 3
    assert s["option_win_rate"] == pytest.approx(1 / 3)
    assert s["avg_option_pnl"] == pytest.approx((0.5 - 0.3 - 0.9) / 3)
    assert s["cum_option_pnl"] == pytest.approx(0.5 - 0.3 - 0.9)


def test_compute_stats_flags_directional_win_option_loss():
    """The exact §S3 case: right on direction, lost money as an option."""
    preds = [
        _p("up", 0.8, "win", 0.02, 0, option_pnl=-0.4),   # win + option loss -> flagged
        _p("up", 0.8, "win", 0.30, 0, option_pnl=0.9),    # win + option win -> not flagged
        _p("down", 0.6, "loss", 0.05, 0, option_pnl=-0.2),  # not a directional win at all
    ]
    s = compute_stats(preds)
    assert s["directional_win_option_loss_n"] == 1


def test_league_table_integration_and_per_regime():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    # chartist: one graded win in regime 0
    pid = tp.record_trader_prediction(
        conn, trader_id="chartist", method_version="m", trade_date=date(2026, 6, 1),
        symbol="SOXL", sector="semiconductor", horizon=5, label_end_date=date(2026, 6, 8),
        direction="up", conviction=0.7, rationale="r", invalidation="i", entry_price=100.0,
        grade_vol=0.4, engine_payload={}, regime=0,
    )
    tp.grade_trader_prediction(conn, pid, actual_price=130.0)  # +30% -> up -> win

    rows = {r["trader_id"]: r for r in league_table(conn, window=20)}
    assert rows["chartist"]["overall"]["hit_rate"] == 1.0
    assert "0" in rows["chartist"]["by_regime"]
    # analyst has no graded rows -> honest empty scorecard, still listed
    assert rows["analyst"]["overall"]["n_graded"] == 0
    assert rows["analyst"]["overall"]["hit_rate"] is None


def test_equity_curve_accumulates_in_trade_date_order():
    # deliberately out of order + one ungraded/pending row that must be excluded
    preds = [
        _p("up", 0.8, "win", 0.10, 0, trade_date=date(2026, 6, 3), symbol="B", option_pnl=0.4),
        _p("down", 0.7, "loss", 0.05, 0, trade_date=date(2026, 6, 1), symbol="A", option_pnl=-0.2),
        _p("range", 0.5, "win", 0.00, 0, trade_date=date(2026, 6, 2), symbol="C"),
        _p("up", 0.6, "win", 0.20, 0, trade_date=date(2026, 6, 5), symbol="D", status="pending"),
    ]
    points = equity_curve(preds)
    assert len(points) == 3  # pending row excluded
    assert [p["trade_date"] for p in points] == [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)]

    # day 1: down loss -> signed pnl = -0.05
    assert points[0]["pnl"] == pytest.approx(-0.05)
    assert points[0]["cum_pnl"] == pytest.approx(-0.05)
    assert points[0]["option_pnl"] == pytest.approx(-0.2)
    assert points[0]["cum_option_pnl"] == pytest.approx(-0.2)

    # day 2: range call -> no directional exposure, no option pnl
    assert points[1]["pnl"] == 0.0
    assert points[1]["cum_pnl"] == pytest.approx(-0.05)
    assert points[1]["cum_option_pnl"] == pytest.approx(-0.2)

    # day 3: up win -> +0.10, option +0.4, cumulative carries forward
    assert points[2]["pnl"] == pytest.approx(0.10)
    assert points[2]["cum_pnl"] == pytest.approx(0.05)
    assert points[2]["option_pnl"] == pytest.approx(0.4)
    assert points[2]["cum_option_pnl"] == pytest.approx(0.2)


def test_equity_curve_empty_is_honest_not_crash():
    assert equity_curve([]) == []


def test_league_equity_curves_integration():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    pid = tp.record_trader_prediction(
        conn, trader_id="chartist", method_version="m", trade_date=date(2026, 6, 1),
        symbol="SOXL", sector="semiconductor", horizon=5, label_end_date=date(2026, 6, 8),
        direction="up", conviction=0.7, rationale="r", invalidation="i", entry_price=100.0,
        grade_vol=0.4, engine_payload={}, regime=0,
    )
    tp.grade_trader_prediction(conn, pid, actual_price=130.0)  # +30% -> up -> win

    curves = {r["trader_id"]: r for r in league_equity_curves(conn)}
    assert len(curves["chartist"]["points"]) == 1
    assert curves["chartist"]["points"][0]["cum_pnl"] == pytest.approx(0.30)
    # analyst has no graded rows -> honest empty points list, still listed
    assert curves["analyst"]["points"] == []
