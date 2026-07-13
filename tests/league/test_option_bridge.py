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
