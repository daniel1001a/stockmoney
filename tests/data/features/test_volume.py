from datetime import date, datetime, timedelta, timezone

import duckdb
import polars as pl

from stockmoney.data.db import append_rows, run_migrations
from stockmoney.data.features.volume import WINDOW, compute_volume_zscore_20d, rolling_volume_zscore


def test_rolling_volume_zscore_too_short_returns_empty():
    assert rolling_volume_zscore([1.0, 2.0, 3.0], WINDOW) == []


def test_rolling_volume_zscore_constant_volume_is_zero():
    volumes = [1000.0] * 25
    out = rolling_volume_zscore(volumes, WINDOW)
    assert out
    assert out[0][0] == WINDOW - 1
    for _, z in out:
        assert z == 0.0


def test_rolling_volume_zscore_spike_is_positive_and_large():
    volumes = [1000.0] * 25
    volumes[-1] = 5000.0  # today's volume is a huge spike vs trailing window
    out = rolling_volume_zscore(volumes, WINDOW)
    assert out[-1][1] > 2.0


def test_rolling_volume_zscore_dip_is_negative():
    volumes = [1000.0] * 25
    volumes[-1] = 100.0  # today's volume is unusually low
    out = rolling_volume_zscore(volumes, WINDOW)
    assert out[-1][1] < 0.0


def test_compute_volume_zscore_20d_writes_features_with_available_at():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)

    n = 25
    start = date(2026, 1, 1)
    dates = [start + timedelta(days=i) for i in range(n)]
    ingested = [datetime(d.year, d.month, d.day, 21, tzinfo=timezone.utc) for d in dates]
    df = pl.DataFrame(
        {
            "symbol": ["SOXL"] * n,
            "trade_date": dates,
            "volume": [1_000_000] * (n - 1) + [5_000_000],
            "source": ["test"] * n,
            "ingested_at": ingested,
        }
    )
    append_rows(conn, "ohlcv_daily", df)

    written = compute_volume_zscore_20d(conn, symbols=["SOXL"])
    assert written == n - WINDOW + 1

    row = conn.execute(
        "SELECT feature_date, available_at, feature_name FROM feature_store "
        "ORDER BY feature_date LIMIT 1"
    ).fetchone()
    assert row[0] == dates[WINDOW - 1]
    assert row[1] == ingested[WINDOW - 1]
    assert row[2] == "volume_zscore_20d"
