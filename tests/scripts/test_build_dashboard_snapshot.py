import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from stockmoney.data.db import MARKET_SYMBOL, append_rows, run_migrations, sector_symbol
from stockmoney.data.features.base import FeatureValue, write_features

import build_dashboard_snapshot as bds

TARGET = "SOXL"
SECTOR = "semiconductor"


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _seed(conn, n=90, seed=0):
    """Mirrors tests/models/test_production.py's seeding style: enough
    history for GMM/logistic fitting and a 5-fold walk-forward split."""
    rng = np.random.default_rng(seed)
    start = date(2024, 1, 1)
    dates = [start + timedelta(days=i) for i in range(n)]
    av = [datetime(d.year, d.month, d.day, 21, tzinfo=timezone.utc) for d in dates]

    closes = [100.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + rng.normal(scale=0.01)))

    append_rows(conn, "ohlcv_daily", pl.DataFrame({
        "symbol": [TARGET] * n, "trade_date": dates, "close": closes,
        "source": ["test"] * n, "ingested_at": av,
    }))

    def _fv(sym, vals):
        return [FeatureValue(d, sym, value=float(v), available_at=a) for d, v, a in zip(dates, vals, av)]

    rv, adx, disp = rng.uniform(0.2, 0.6, n), rng.uniform(10, 40, n), rng.uniform(0.005, 0.02, n)
    curve, dxy, oil = rng.normal(size=n), rng.normal(scale=0.01, size=n), rng.normal(scale=0.02, size=n)

    write_features(conn, feature_name="realized_vol_20d", feature_version="v1",
                   source_table="ohlcv_daily", values=_fv(TARGET, rv))
    write_features(conn, feature_name="adx_14", feature_version="v1",
                   source_table="ohlcv_daily", values=_fv(TARGET, adx))
    write_features(conn, feature_name="xsec_dispersion", feature_version="v1",
                   source_table="ohlcv_daily", values=_fv(sector_symbol(SECTOR), disp))
    write_features(conn, feature_name="yield_curve_10y2y_ffill", feature_version="v1",
                   source_table="macro_series_daily", values=_fv(MARKET_SYMBOL, curve))
    write_features(conn, feature_name="dxy_chg_1d_ffill", feature_version="v1",
                   source_table="macro_series_daily", values=_fv(MARKET_SYMBOL, dxy))
    write_features(conn, feature_name="oil_chg_1d_ffill", feature_version="v1",
                   source_table="macro_series_daily", values=_fv(MARKET_SYMBOL, oil))
    return dates


def test_run_snapshot_build_writes_prediction_and_backtest_row_for_seeded_symbol():
    conn = _conn()
    _seed(conn)

    summary = bds.run_snapshot_build(conn)

    assert summary["SOXL"] == "ok"
    pred_row = conn.execute(
        "SELECT symbol FROM daily_predictions WHERE symbol = 'SOXL'"
    ).fetchone()
    assert pred_row is not None

    snap_row = conn.execute(
        "SELECT symbol, overall_n, ev_of_continuing_now FROM symbol_backtest_snapshot WHERE symbol = 'SOXL'"
    ).fetchone()
    assert snap_row is not None
    assert snap_row[1] > 0  # overall_n from the walk-forward report


def test_run_snapshot_build_skips_unsupported_symbols_without_crashing():
    conn = _conn()
    _seed(conn)  # only SOXL has data; the other 10 watchlist members don't

    summary = bds.run_snapshot_build(conn)

    assert summary["SOXL"] == "ok"
    assert summary["AAPL"].startswith("prediction skipped")
    assert conn.execute("SELECT count(*) FROM symbol_backtest_snapshot").fetchone()[0] == 1


def test_run_snapshot_build_is_safe_to_run_twice_same_day():
    conn = _conn()
    _seed(conn)

    bds.run_snapshot_build(conn)
    bds.run_snapshot_build(conn)

    # symbol_backtest_snapshot: today's row replaced, not duplicated.
    assert conn.execute(
        "SELECT count(*) FROM symbol_backtest_snapshot WHERE symbol = 'SOXL'"
    ).fetchone()[0] == 1
    # daily_predictions: record_live_prediction -> record_prediction is
    # idempotent per (symbol, trade_date), so still exactly one row.
    assert conn.execute(
        "SELECT count(*) FROM daily_predictions WHERE symbol = 'SOXL'"
    ).fetchone()[0] == 1
