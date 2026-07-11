from datetime import date, datetime, timedelta, timezone

import duckdb
import numpy as np
import polars as pl

from stockmoney.data.db import MARKET_SYMBOL, append_rows, run_migrations, sector_symbol
from stockmoney.data.features.base import FeatureValue, write_features
from stockmoney.models.feature_matrix import (
    DOWN,
    FEATURE_COLUMNS,
    RANGE,
    REGIME_COLUMNS,
    UP,
    build_feature_matrix,
    latest_unresolved_feature_rows,
    to_dataset,
)

TARGET = "SOXL"
SECTOR = "semiconductor"


def _seed(conn, closes: list[float], *, rv=0.0001, adx=20.0, disp=0.01,
          curve=1.0, dxy=0.0, oil=0.0, rsi=50.0, vol_z=0.0):
    start = date(2026, 1, 1)
    dates = [start + timedelta(days=i) for i in range(len(closes))]
    av = [datetime(d.year, d.month, d.day, 21, tzinfo=timezone.utc) for d in dates]

    append_rows(conn, "ohlcv_daily", pl.DataFrame({
        "symbol": [TARGET] * len(closes),
        "trade_date": dates,
        "close": closes,
        "source": ["test"] * len(closes),
        "ingested_at": av,
    }))

    def _fv(sym, val):
        return [FeatureValue(d, sym, value=val, available_at=a) for d, a in zip(dates, av)]

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
    write_features(conn, feature_name="rsi_14", feature_version="v1",
                   source_table="ohlcv_daily", values=_fv(TARGET, rsi))
    write_features(conn, feature_name="volume_zscore_20d", feature_version="v1",
                   source_table="ohlcv_daily", values=_fv(TARGET, vol_z))
    return dates


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def test_to_dataset_regime_x_has_only_regime_columns():
    conn = _conn()
    closes = [100.0 + i for i in range(11)]
    _seed(conn, closes)
    m = build_feature_matrix(conn, target_symbol=TARGET, sector=SECTOR, horizon=5)
    ds = to_dataset(m)
    assert ds.regime_X.shape == (ds.X.shape[0], 3)
    np.testing.assert_array_equal(ds.regime_X, m.select(REGIME_COLUMNS).to_numpy())


def test_forward_return_and_label_end_date():
    conn = _conn()
    closes = [100.0 + i for i in range(11)]  # +1/day
    dates = _seed(conn, closes)

    m = build_feature_matrix(conn, target_symbol=TARGET, sector=SECTOR, horizon=5)
    ds = to_dataset(m)

    # 11 closes, horizon 5 → rows for indices 0..5 = 6 rows.
    assert len(ds.trade_dates) == 6
    # Row 0: close[5]/close[0]-1 = 105/100 - 1.
    assert ds.fwd_return[0] == (105.0 / 100.0 - 1.0)
    assert ds.label_end_dates[0] == dates[5]


def test_labels_up_down_range_with_vol_scaled_band():
    # Tiny vol → band ≈ 0, so sign of the move decides up vs down; exactly flat → range.
    up = build_feature_matrix(_seeded([100.0 + i for i in range(11)]),
                              target_symbol=TARGET, sector=SECTOR, horizon=5)
    down = build_feature_matrix(_seeded([110.0 - i for i in range(11)]),
                                target_symbol=TARGET, sector=SECTOR, horizon=5)
    flat = build_feature_matrix(_seeded([100.0] * 11),
                                target_symbol=TARGET, sector=SECTOR, horizon=5)

    assert set(up["label"].to_list()) == {UP}
    assert set(down["label"].to_list()) == {DOWN}
    assert set(flat["label"].to_list()) == {RANGE}


def test_range_when_move_inside_band():
    # Large vol → wide band; a small real move stays classified as range.
    conn = _conn()
    closes = [100.0, 100.5, 101.0, 101.5, 102.0, 102.5, 103.0, 103.5, 104.0, 104.5, 105.0]
    _seed(conn, closes, rv=2.0)  # 200% annualized vol → very wide band
    m = build_feature_matrix(conn, target_symbol=TARGET, sector=SECTOR, horizon=5)
    assert set(m["label"].to_list()) == {RANGE}


def _seeded(closes):
    conn = _conn()
    _seed(conn, closes)
    return conn


def test_latest_unresolved_returns_rows_build_feature_matrix_drops():
    conn = _conn()
    closes = [100.0 + i for i in range(11)]
    dates = _seed(conn, closes)

    unresolved = latest_unresolved_feature_rows(
        conn, target_symbol=TARGET, sector=SECTOR, horizon=5, n=10
    )
    # 11 closes, horizon 5 -> indices 0..5 resolved (6 rows), 6..10 unresolved (5 rows).
    assert unresolved.height == 5
    assert unresolved["trade_date"].to_list() == dates[6:11]
    for c in FEATURE_COLUMNS:
        assert c in unresolved.columns
    assert "fwd_return" not in unresolved.columns
    assert "label" not in unresolved.columns


def test_latest_unresolved_respects_n():
    conn = _conn()
    closes = [100.0 + i for i in range(11)]
    dates = _seed(conn, closes)

    latest_one = latest_unresolved_feature_rows(
        conn, target_symbol=TARGET, sector=SECTOR, horizon=5, n=1
    )
    assert latest_one.height == 1
    assert latest_one["trade_date"].to_list() == [dates[-1]]


def test_latest_unresolved_empty_when_no_feature_data_exists():
    conn = _conn()
    unresolved = latest_unresolved_feature_rows(
        conn, target_symbol=TARGET, sector=SECTOR, horizon=5, n=5
    )
    assert unresolved.height == 0


def test_build_feature_matrix_and_latest_unresolved_are_disjoint_and_exhaustive():
    """The core look-ahead-safety guarantee: every trade_date with complete
    features falls into exactly one of the two partitions -- never both,
    never neither."""
    conn = _conn()
    closes = [100.0 + i for i in range(23)]
    dates = _seed(conn, closes)
    horizon = 5

    resolved = build_feature_matrix(conn, target_symbol=TARGET, sector=SECTOR, horizon=horizon)
    unresolved = latest_unresolved_feature_rows(
        conn, target_symbol=TARGET, sector=SECTOR, horizon=horizon, n=len(dates)
    )

    resolved_dates = set(resolved["trade_date"].to_list())
    unresolved_dates = set(unresolved["trade_date"].to_list())

    assert resolved_dates & unresolved_dates == set()
    assert resolved_dates | unresolved_dates == set(dates)
