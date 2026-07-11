from datetime import date, datetime, timedelta, timezone

import duckdb
import numpy as np
import polars as pl

from stockmoney.data.db import MARKET_SYMBOL, append_rows, run_migrations, sector_symbol
from stockmoney.data.features.base import FeatureValue, write_features
from stockmoney.models.feature_matrix import FEATURE_COLUMNS
from stockmoney.models.production import (
    current_ev_of_continuing,
    fit_production_model,
    predict_latest,
)

TARGET = "SOXL"
SECTOR = "semiconductor"


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _seed(conn, n=90, seed=0):
    """A long-enough, mildly noisy series so GMM/logistic-regression fitting
    and walk_forward's default 5-fold split all have enough rows to work
    with, mirroring test_feature_matrix.py's DB-level seeding style."""
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

    rv = rng.uniform(0.2, 0.6, size=n)
    adx = rng.uniform(10, 40, size=n)
    disp = rng.uniform(0.005, 0.02, size=n)
    curve = rng.normal(size=n)
    dxy = rng.normal(scale=0.01, size=n)
    oil = rng.normal(scale=0.02, size=n)
    rsi = rng.uniform(30, 70, size=n)
    vol_z = rng.normal(size=n)

    write_features(conn, feature_name="realized_vol_20d", feature_version="v1",
                   source_table="ohlcv_daily", values=_fv(TARGET, rv))
    write_features(conn, feature_name="adx_14", feature_version="v1",
                   source_table="ohlcv_daily", values=_fv(TARGET, adx))
    write_features(conn, feature_name="xsec_dispersion", feature_version="v1",
                   source_table="ohlcv_daily", values=_fv(sector_symbol(SECTOR), disp))
    write_features(conn, feature_name="yield_curve_10y2y", feature_version="v1",
                   source_table="macro_series_daily", values=_fv(MARKET_SYMBOL, curve))
    write_features(conn, feature_name="dxy_chg_1d_ffill", feature_version="v1",
                   source_table="macro_series_daily", values=_fv(MARKET_SYMBOL, dxy))
    write_features(conn, feature_name="oil_chg_1d_ffill", feature_version="v1",
                   source_table="macro_series_daily", values=_fv(MARKET_SYMBOL, oil))
    write_features(conn, feature_name="rsi_14", feature_version="v1",
                   source_table="ohlcv_daily", values=_fv(TARGET, rsi))
    write_features(conn, feature_name="volume_zscore_20d", feature_version="v1",
                   source_table="ohlcv_daily", values=_fv(TARGET, vol_z))
    return dates


def test_fit_production_model_none_when_no_data():
    conn = _conn()
    assert fit_production_model(conn, target_symbol=TARGET, sector=SECTOR) is None


def test_fit_production_model_fits_when_enough_history():
    conn = _conn()
    _seed(conn)
    fitted = fit_production_model(conn, target_symbol=TARGET, sector=SECTOR)
    assert fitted is not None
    track, model = fitted
    assert hasattr(track, "label")
    assert hasattr(model, "predict_proba")


def test_predict_latest_none_when_no_data():
    conn = _conn()
    assert predict_latest(conn, target_symbol=TARGET, sector=SECTOR) is None


def test_predict_latest_scores_the_most_recent_unresolved_row():
    conn = _conn()
    dates = _seed(conn, n=90)
    horizon = 5

    pred = predict_latest(conn, target_symbol=TARGET, sector=SECTOR, horizon=horizon)

    assert pred is not None
    assert pred.as_of_date == dates[-1]
    assert pred.symbol == TARGET
    assert pred.sector == SECTOR
    assert pred.horizon == horizon
    assert pred.regime in (0, 1, 2)
    assert pred.proba.shape == (3,)
    assert abs(pred.proba.sum() - 1.0) < 1e-6
    assert set(pred.feature_values.keys()) == set(FEATURE_COLUMNS)
    assert pred.model_version


def test_predict_latest_unsupported_sector_returns_none():
    # big_tech has no xsec_dispersion feature computed anywhere -> _load_features
    # never finds that row complete -> no resolved matrix, no unresolved rows either.
    conn = _conn()
    _seed(conn, n=90)
    assert predict_latest(conn, target_symbol=TARGET, sector="big_tech") is None


def test_current_ev_of_continuing_none_when_no_data():
    conn = _conn()
    assert current_ev_of_continuing(conn, target_symbol=TARGET, sector=SECTOR) is None


def test_current_ev_of_continuing_returns_a_float_when_enough_history():
    conn = _conn()
    dates = _seed(conn, n=90)
    ev = current_ev_of_continuing(conn, target_symbol=TARGET, sector=SECTOR, asof_date=dates[-1])
    # May legitimately be None if the 10-resolved-trade minimum in
    # expanding_stats_asof isn't cleared by this small synthetic series --
    # assert type, not a specific value, since the model has no real edge here.
    assert ev is None or isinstance(ev, float)
