from __future__ import annotations

from datetime import date

import duckdb
import pytest

from stockmoney.data.db import run_migrations
from stockmoney.data import trader_prediction_grades as tpg
from stockmoney.data import trader_predictions as tp
from stockmoney.data.trader_predictions import TraderPrediction
from stockmoney.league.league_table import (
    MIN_GRADED_FOR_RANKING,
    compute_stats,
    equity_curve,
    league_equity_curves,
    league_table,
)


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


def test_compute_stats_terminology_split_profitable_rate_and_expected_value():
    """Issue #7 P1: profitable_rate/expected_value are always present, and
    expected_value prefers the real option-priced average when any option
    was ever graded (a trader can be right on direction and still lose to
    theta/IV-crush -- ranking must not be fooled by that)."""
    preds = [
        _p("up", 0.8, "win", 0.10, 0, option_pnl=0.5),
        _p("down", 0.7, "loss", 0.05, 0, option_pnl=-0.3),
    ]
    s = compute_stats(preds)
    assert s["profitable_rate"] == s["option_win_rate"]
    assert s["expected_value"] == pytest.approx(s["avg_option_pnl"])
    assert s["cum_expected_value"] == pytest.approx(s["cum_option_pnl"])


def test_compute_stats_expected_value_falls_back_to_underlying_pnl_without_options():
    preds = [_p("up", 0.8, "win", 0.10, 0)]  # no option_pnl ever graded
    s = compute_stats(preds)
    assert s["expected_value"] == pytest.approx(s["avg_pnl"])
    assert s["cum_expected_value"] == pytest.approx(s["cum_pnl"])


def test_compute_stats_data_sufficient_flag():
    few = [_p("up", 0.8, "win", 0.10, 0)]
    assert compute_stats(few)["data_sufficient"] is False
    many = [_p("up", 0.8, "win", 0.10, 0) for _ in range(MIN_GRADED_FOR_RANKING)]
    assert compute_stats(many)["data_sufficient"] is True


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


def test_league_table_ranks_by_expected_value_not_hit_rate():
    """A trader that's right on direction but loses money as an option must
    not out-rank one with a lower hit rate but a positive EV."""
    conn = duckdb.connect(":memory:")
    run_migrations(conn)

    # chartist: 2/2 directionally right, but both option legs lost money.
    for i, symbol in enumerate(["SOXL", "NVDA"]):
        pid = tp.record_trader_prediction(
            conn, trader_id="chartist", method_version="m", trade_date=date(2026, 6, 1 + i),
            symbol=symbol, sector="semiconductor", horizon=5, label_end_date=date(2026, 6, 8 + i),
            direction="up", conviction=0.7, rationale="r", invalidation="i", entry_price=100.0,
            grade_vol=0.4, engine_payload={}, regime=0,
            option_structure={"spot": 100.0, "strike": 110.0, "is_call": True, "iv": 0.4,
                               "iv_source": "proxy", "t_years": 30 / 365, "dte_days": 30, "side": "long"},
        )
        tp.grade_trader_prediction(conn, pid, actual_price=130.0)  # directionally right ('up')
        tp.set_option_pnl(conn, pid, option_pnl=-0.4)  # but the option lost (theta/IV-crush)

    # analyst: 1/2 directionally right, option pnl positive on the win.
    pid1 = tp.record_trader_prediction(
        conn, trader_id="analyst", method_version="m", trade_date=date(2026, 6, 1),
        symbol="AVGO", sector="semiconductor", horizon=5, label_end_date=date(2026, 6, 8),
        direction="up", conviction=0.6, rationale="r", invalidation="i", entry_price=100.0,
        grade_vol=0.4, engine_payload={}, regime=0,
        option_structure={"spot": 100.0, "strike": 110.0, "is_call": True, "iv": 0.4,
                           "iv_source": "proxy", "t_years": 30 / 365, "dte_days": 30, "side": "long"},
    )
    tp.grade_trader_prediction(conn, pid1, actual_price=130.0)
    tp.set_option_pnl(conn, pid1, option_pnl=0.9)
    pid2 = tp.record_trader_prediction(
        conn, trader_id="analyst", method_version="m", trade_date=date(2026, 6, 2),
        symbol="AMD", sector="semiconductor", horizon=5, label_end_date=date(2026, 6, 9),
        direction="up", conviction=0.6, rationale="r", invalidation="i", entry_price=100.0,
        grade_vol=0.4, engine_payload={}, regime=0,
        option_structure={"spot": 100.0, "strike": 110.0, "is_call": True, "iv": 0.4,
                           "iv_source": "proxy", "t_years": 30 / 365, "dte_days": 30, "side": "long"},
    )
    tp.grade_trader_prediction(conn, pid2, actual_price=90.0)  # wrong
    tp.set_option_pnl(conn, pid2, option_pnl=-0.2)

    rows = {r["trader_id"]: r for r in league_table(conn, window=20)}
    assert rows["chartist"]["rolling"]["hit_rate"] == 1.0
    assert rows["analyst"]["rolling"]["hit_rate"] == 0.5
    # analyst's positive EV must out-rank chartist's negative EV despite the
    # lower hit rate -- ranking is on expected_value, not hit_rate.
    ranked_ids = [r["trader_id"] for r in league_table(conn, window=20)]
    assert ranked_ids.index("analyst") < ranked_ids.index("chartist")


def test_league_table_pushes_insufficient_data_traders_below_sufficient_ones():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    # chartist: one lucky graded call with a huge EV, but far below
    # MIN_GRADED_FOR_RANKING -- statistically meaningless despite the number.
    pid = tp.record_trader_prediction(
        conn, trader_id="chartist", method_version="m", trade_date=date(2026, 6, 1),
        symbol="SOXL", sector="semiconductor", horizon=5, label_end_date=date(2026, 6, 8),
        direction="up", conviction=0.7, rationale="r", invalidation="i", entry_price=100.0,
        grade_vol=0.4, engine_payload={}, regime=0,
    )
    tp.grade_trader_prediction(conn, pid, actual_price=200.0)  # +100%, absurdly high avg_pnl

    # analyst: MIN_GRADED_FOR_RANKING graded calls at a modest, real EV.
    for i in range(MIN_GRADED_FOR_RANKING):
        pid = tp.record_trader_prediction(
            conn, trader_id="analyst", method_version="m", trade_date=date(2026, 6, 1),
            symbol=f"SYM{i}", sector="semiconductor", horizon=5, label_end_date=date(2026, 6, 8),
            direction="up", conviction=0.6, rationale="r", invalidation="i", entry_price=100.0,
            grade_vol=0.4, engine_payload={}, regime=0,
        )
        tp.grade_trader_prediction(conn, pid, actual_price=105.0)  # modest +5%

    rows = league_table(conn, window=20)
    chartist = next(r for r in rows if r["trader_id"] == "chartist")
    analyst = next(r for r in rows if r["trader_id"] == "analyst")
    assert chartist["rolling"]["data_sufficient"] is False
    assert analyst["rolling"]["data_sufficient"] is True
    assert chartist["rolling"]["expected_value"] > analyst["rolling"]["expected_value"]  # the lucky number really is bigger...
    ranked_ids = [r["trader_id"] for r in rows]
    assert ranked_ids.index("analyst") < ranked_ids.index("chartist")  # ...but must not out-rank a real sample


def test_league_table_includes_multi_horizon_breakdown():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    pid = tp.record_trader_prediction(
        conn, trader_id="chartist", method_version="m", trade_date=date(2026, 6, 1),
        symbol="SOXL", sector="semiconductor", horizon=5, label_end_date=date(2026, 6, 8),
        direction="up", conviction=0.7, rationale="r", invalidation="i", entry_price=100.0,
        grade_vol=0.4, engine_payload={}, regime=0,
    )
    tpg.record_pending_grades(conn, prediction_id=pid, trade_date=date(2026, 6, 1))
    tpg.grade_horizon(conn, prediction_id=pid, horizon_days=1, actual_price=130.0)

    rows = {r["trader_id"]: r for r in league_table(conn, window=20)}
    horizons = rows["chartist"]["horizons"]
    assert set(horizons) == {"1", "5", "21"}
    assert horizons["1"]["n_graded"] == 1
    assert horizons["1"]["hit_rate"] == 1.0
    assert horizons["5"]["n_graded"] == 0  # not yet graded at this horizon
    assert horizons["5"]["data_sufficient"] is False


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
