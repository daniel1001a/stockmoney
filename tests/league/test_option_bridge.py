from __future__ import annotations

from datetime import date, datetime, timezone

import duckdb
import numpy as np
import polars as pl
import pytest

from stockmoney.data.db import append_rows, run_migrations
from stockmoney.league.context import MarketContext
from stockmoney.league.engines import EngineCall
from stockmoney.league.option_bridge import build_option_structure
from stockmoney.models.production import ProductionPrediction
from stockmoney.models.strike_ladder import is_on_ladder

TRADE_DATE = date(2026, 6, 1)
END = date(2026, 6, 8)


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _ctx(*, symbol="NVDA", entry_price=100.0, grade_vol=0.4, regime=0):
    prod = ProductionPrediction(
        as_of_date=TRADE_DATE, symbol=symbol, sector="semiconductor", horizon=5,
        regime=regime, proba=np.array([0.1, 0.2, 0.7]),
        feature_values={"realized_vol_20d": grade_vol, "adx_14": 25.0, "xsec_dispersion": 0.03},
        model_version="gmm-logistic-v2",
    )
    return MarketContext(
        symbol=symbol, sector="semiconductor", trade_date=TRADE_DATE, horizon=5,
        label_end_date=END, entry_price=entry_price, grade_vol=grade_vol, band_k=0.5,
        regime=regime, available_at=datetime.now().astimezone(), production=prod,
    )


def _call(direction, conviction=0.7):
    return EngineCall(
        direction=direction, conviction=conviction, rationale="r", invalidation="i",
        method_version="m", engine_payload={},
    )


def _seed_real_iv(conn, symbol, iv, *, expiry_date=date(2026, 7, 1)):
    av = datetime(2026, 6, 1, 21, tzinfo=timezone.utc)
    append_rows(conn, "iv_surface_daily", pl.DataFrame({
        "symbol": [symbol], "trade_date": [TRADE_DATE], "expiry_date": [expiry_date],
        "delta_bucket": ["50"], "implied_vol": [iv], "source": ["test"], "ingested_at": [av],
    }))


def test_up_call_produces_a_long_call_structure():
    conn = _conn()
    structure = build_option_structure(conn, _ctx(), _call("up"))
    assert structure is not None
    assert structure["is_call"] is True
    assert structure["side"] == "long"
    assert structure["spot"] == pytest.approx(100.0)
    assert structure["strike"] > 100.0  # target_delta=0.40 call is OTM, same as option_selection's own test


def test_down_call_produces_a_long_put_structure():
    conn = _conn()
    structure = build_option_structure(conn, _ctx(), _call("down"))
    assert structure is not None
    assert structure["is_call"] is False
    assert structure["strike"] < 100.0


def test_range_call_produces_no_structure():
    conn = _conn()
    assert build_option_structure(conn, _ctx(), _call("range")) is None


def test_missing_grade_vol_produces_no_structure():
    conn = _conn()
    ctx = _ctx()
    ctx.grade_vol = None
    assert build_option_structure(conn, ctx, _call("up")) is None


def test_uses_proxy_iv_when_no_real_snapshot_exists():
    conn = _conn()
    structure = build_option_structure(conn, _ctx(grade_vol=0.4), _call("up"))
    assert structure["iv_source"] == "proxy"
    assert structure["iv"] == pytest.approx(0.4 * 1.1)  # options_iv.IV_RV_RATIO


def test_uses_real_iv_when_a_same_day_snapshot_exists():
    conn = _conn()
    _seed_real_iv(conn, "NVDA", 0.55)
    structure = build_option_structure(conn, _ctx(grade_vol=0.4), _call("up"))
    assert structure["iv_source"] == "real"
    assert structure["iv"] == pytest.approx(0.55)


def test_dte_days_matches_selection_params_default():
    conn = _conn()
    structure = build_option_structure(conn, _ctx(), _call("up"))
    assert structure["dte_days"] == 30
    assert structure["t_years"] == pytest.approx(30 / 365)


def test_strike_is_snapped_to_a_listed_increment_not_a_raw_bs_strike():
    """The flagship "impossible strike" bug: option_selection's raw
    Black-Scholes-inverted strike (e.g. 996.7139... for a ~$967 underlying)
    must never reach the arena's stored option_structure verbatim."""
    conn = _conn()
    structure = build_option_structure(conn, _ctx(symbol="NFLX", entry_price=967.65), _call("up"))
    assert is_on_ladder(structure["strike"])


def test_strike_is_snapped_for_a_put_too():
    conn = _conn()
    structure = build_option_structure(conn, _ctx(symbol="TSLA", entry_price=234.6), _call("down"))
    assert is_on_ladder(structure["strike"])


def test_nan_entry_price_produces_no_structure_not_a_crash():
    """Regression (2026-07-15 incident): a NaN entry_price (from a yfinance
    partial-bar close, see stockmoney.data.positions.latest_underlying_price)
    used to reach option_selection.select_option -> strike_ladder.snap_strike
    and raise ValueError('strike must be a positive finite number, got nan'),
    aborting orchestration.run_predictions for the whole league. It must be
    treated as "no usable entry price" -- same as the existing missing-
    grade_vol case -- and return None instead of raising."""
    conn = _conn()
    assert build_option_structure(conn, _ctx(entry_price=float("nan")), _call("up")) is None


def test_nan_grade_vol_produces_no_structure_not_a_crash():
    """Same NaN-vs-guard gap as above, via the proxy-IV path (grade_vol ->
    entry_iv_proxy): `ctx.grade_vol <= 0` does not catch NaN either, so this
    exercises option_selection.select_option's NaN guard on `iv` rather than
    a guard inside build_option_structure itself."""
    conn = _conn()
    assert build_option_structure(conn, _ctx(grade_vol=float("nan")), _call("up")) is None
