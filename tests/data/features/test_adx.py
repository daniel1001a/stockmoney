from datetime import date, datetime, timedelta, timezone

import duckdb
import polars as pl

from stockmoney.data.db import append_rows, run_migrations
from stockmoney.data.features.adx import PERIOD, compute_adx_14, wilder_adx


def test_wilder_adx_strong_uptrend_high_value():
    # A clean monotonic uptrend should produce a high ADX (strong trend).
    n = 60
    highs = [100.0 + i for i in range(n)]
    lows = [99.0 + i for i in range(n)]
    closes = [99.5 + i for i in range(n)]

    out = wilder_adx(highs, lows, closes, PERIOD)

    assert out  # defined
    first_idx = out[0][0]
    assert first_idx == 2 * PERIOD - 1
    # Persistent one-directional move → ADX climbs well above the 25 "trend" line.
    assert out[-1][1] > 40


def test_wilder_adx_too_short_returns_empty():
    assert wilder_adx([1, 2, 3], [1, 2, 3], [1, 2, 3], PERIOD) == []


def test_wilder_adx_choppy_lower_than_trending():
    n = 80
    # Choppy: alternating up/down, no sustained direction.
    highs, lows, closes = [], [], []
    for i in range(n):
        base = 100.0 + (1.0 if i % 2 == 0 else -1.0)
        highs.append(base + 0.5)
        lows.append(base - 0.5)
        closes.append(base)
    choppy = wilder_adx(highs, lows, closes, PERIOD)[-1][1]

    trend = wilder_adx(
        [100.0 + i for i in range(n)],
        [99.0 + i for i in range(n)],
        [99.5 + i for i in range(n)],
        PERIOD,
    )[-1][1]

    assert choppy < trend


def test_compute_adx_14_writes_features_with_available_at():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)

    n = 40
    start = date(2026, 1, 1)
    dates = [start + timedelta(days=i) for i in range(n)]
    ingested = [datetime(d.year, d.month, d.day, 21, tzinfo=timezone.utc) for d in dates]
    df = pl.DataFrame(
        {
            "symbol": ["SOXL"] * n,
            "trade_date": dates,
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.0 + i for i in range(n)],
            "source": ["test"] * n,
            "ingested_at": ingested,
        }
    )
    append_rows(conn, "ohlcv_daily", df)

    written = compute_adx_14(conn, symbols=["SOXL"])
    assert written == n - (2 * PERIOD - 1)

    row = conn.execute(
        "SELECT feature_date, available_at, feature_name FROM feature_store "
        "ORDER BY feature_date LIMIT 1"
    ).fetchone()
    assert row[0] == dates[2 * PERIOD - 1]
    assert row[1] == ingested[2 * PERIOD - 1]
    assert row[2] == "adx_14"
