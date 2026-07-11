"""Architectural-invariant canary (CLAUDE.md section 4): regime detection may
observe ONLY {realized_vol_20d, adx_14, xsec_dispersion} (3 columns), never
the direction model's other 3 macro columns. Unlike test_leakage_canary.py
(temporal look-ahead), this guards column width -- a distinct way the same
section 4 boundary could regress silently if a future edit routes dataset.X
into a RegimeTrack call instead of dataset.regime_X."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import duckdb
import numpy as np
import polars as pl

from stockmoney.data.db import MARKET_SYMBOL, append_rows, run_migrations, sector_symbol
from stockmoney.data.features.base import FeatureValue, write_features
from stockmoney.models.feature_matrix import Dataset, REGIME_COLUMNS
from stockmoney.models.walk_forward import run_walk_forward

TARGET = "SOXL"
SECTOR = "semiconductor"


class _ShapeAssertingRegimeTrack:
    def fit(self, train_X: np.ndarray) -> None:
        assert train_X.shape[1] == len(REGIME_COLUMNS), (
            f"RegimeTrack.fit received {train_X.shape[1]} columns, expected {len(REGIME_COLUMNS)}"
        )

    def label(self, X: np.ndarray) -> np.ndarray:
        assert X.shape[1] == len(REGIME_COLUMNS), (
            f"RegimeTrack.label received {X.shape[1]} columns, expected {len(REGIME_COLUMNS)}"
        )
        return np.zeros(len(X), dtype=int)


class _RecordingDirectionModel:
    def __init__(self):
        self.fit_width = None
        self.predict_width = None

    def fit(self, X, y, regimes):
        self.fit_width = X.shape[1]

    def predict_proba(self, X, regimes):
        self.predict_width = X.shape[1]
        return np.tile(np.array([1 / 3, 1 / 3, 1 / 3]), (len(X), 1))


def _synthetic_dataset(n=120, horizon=5, seed=0) -> Dataset:
    rng = np.random.default_rng(seed)
    start = date(2020, 1, 1)
    trade_dates = [start + timedelta(days=i) for i in range(n)]
    label_end_dates = [start + timedelta(days=i + horizon) for i in range(n)]
    X = rng.normal(size=(n, 6))
    return Dataset(
        X=X, regime_X=X[:, :3], y=rng.integers(0, 3, size=n),
        trade_dates=trade_dates,
        available_at=[datetime(d.year, d.month, d.day, tzinfo=timezone.utc) for d in trade_dates],
        label_end_dates=label_end_dates, fwd_return=rng.normal(scale=0.02, size=n),
    )


def test_walk_forward_regime_track_never_sees_more_than_regime_columns():
    ds = _synthetic_dataset()
    direction_model = _RecordingDirectionModel()
    run_walk_forward(ds, _ShapeAssertingRegimeTrack, lambda: direction_model)
    assert direction_model.fit_width == 6
    assert direction_model.predict_width == 6


# --- production.py end-to-end canary -----------------------------------

def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _seed(conn, n=90, seed=0):
    """Local, minimal copy of test_production.py's seeding helper -- kept
    self-contained per this file's own docstring convention (no conftest.py
    in this project, and cross-importing private test helpers across module
    boundaries is a bigger smell than a small duplicated fixture)."""
    rng = np.random.default_rng(seed)
    start = date(2024, 1, 1)
    dates = [start + timedelta(days=i) for i in range(n)]
    av = [datetime(d.year, d.month, d.day, 21, tzinfo=timezone.utc) for d in dates]

    closes = [100.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + rng.normal(scale=0.01)))

    append_rows(conn, "ohlcv_daily", pl.DataFrame({
        "symbol": [TARGET] * n,
        "trade_date": dates,
        "close": closes,
        "source": ["test"] * n,
        "ingested_at": av,
    }))

    def _fv(sym, vals):
        return [FeatureValue(d, sym, value=float(v), available_at=a) for d, v, a in zip(dates, vals, av)]

    write_features(conn, feature_name="realized_vol_20d", feature_version="v1",
                   source_table="ohlcv_daily", values=_fv(TARGET, rng.uniform(0.2, 0.6, size=n)))
    write_features(conn, feature_name="adx_14", feature_version="v1",
                   source_table="ohlcv_daily", values=_fv(TARGET, rng.uniform(10, 40, size=n)))
    write_features(conn, feature_name="xsec_dispersion", feature_version="v1",
                   source_table="ohlcv_daily", values=_fv(sector_symbol(SECTOR), rng.uniform(0.005, 0.02, size=n)))
    write_features(conn, feature_name="yield_curve_10y2y_ffill", feature_version="v1",
                   source_table="macro_series_daily", values=_fv(MARKET_SYMBOL, rng.normal(size=n)))
    write_features(conn, feature_name="dxy_chg_1d_ffill", feature_version="v1",
                   source_table="macro_series_daily", values=_fv(MARKET_SYMBOL, rng.normal(scale=0.01, size=n)))
    write_features(conn, feature_name="oil_chg_1d_ffill", feature_version="v1",
                   source_table="macro_series_daily", values=_fv(MARKET_SYMBOL, rng.normal(scale=0.02, size=n)))
    return dates


def test_production_regime_track_never_sees_more_than_regime_columns(monkeypatch):
    import stockmoney.models.production as production

    monkeypatch.setattr(production, "KMeansGMMTrack", lambda **kwargs: _ShapeAssertingRegimeTrack())
    conn = _conn()
    _seed(conn, n=90)
    pred = production.predict_latest(conn, target_symbol=TARGET, sector=SECTOR)
    assert pred is not None  # the spy's assertion already fired if width was wrong
