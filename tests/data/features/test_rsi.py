from datetime import date, datetime, timedelta, timezone

import duckdb
import polars as pl

from stockmoney.data.db import append_rows, run_migrations
from stockmoney.data.features.rsi import PERIOD, compute_rsi_14, wilder_rsi


def test_wilder_rsi_pure_uptrend_is_100():
    n = 30
    closes = [100.0 + i for i in range(n)]  # every day is a gain, no losses at all
    out = wilder_rsi(closes, PERIOD)

    assert out
    assert out[0][0] == PERIOD
    for _, rsi in out:
        assert rsi == 100.0


def test_wilder_rsi_pure_downtrend_is_zero():
    n = 30
    closes = [100.0 - i for i in range(n)]  # every day is a loss, no gains at all
    out = wilder_rsi(closes, PERIOD)

    for _, rsi in out:
        assert rsi == 0.0


def test_wilder_rsi_flat_series_is_neutral():
    # No gains and no losses -> avg_gain=avg_loss=0 -> defined as neutral 50.
    closes = [100.0] * 20
    out = wilder_rsi(closes, PERIOD)
    assert out
    for _, rsi in out:
        assert rsi == 50.0


def test_wilder_rsi_too_short_returns_empty():
    assert wilder_rsi([1.0, 2.0, 3.0], PERIOD) == []


def test_wilder_rsi_bounded_0_to_100():
    closes = [100.0, 102.0, 101.0, 103.0, 99.0, 104.0, 98.0, 105.0, 97.0, 106.0,
              96.0, 107.0, 95.0, 108.0, 94.0, 109.0, 93.0, 110.0]
    out = wilder_rsi(closes, PERIOD)
    assert out
    for _, rsi in out:
        assert 0.0 <= rsi <= 100.0


def test_compute_rsi_14_writes_features_with_available_at():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)

    n = 30
    start = date(2026, 1, 1)
    dates = [start + timedelta(days=i) for i in range(n)]
    ingested = [datetime(d.year, d.month, d.day, 21, tzinfo=timezone.utc) for d in dates]
    df = pl.DataFrame(
        {
            "symbol": ["SOXL"] * n,
            "trade_date": dates,
            "close": [100.0 + i for i in range(n)],
            "source": ["test"] * n,
            "ingested_at": ingested,
        }
    )
    append_rows(conn, "ohlcv_daily", df)

    written = compute_rsi_14(conn, symbols=["SOXL"])
    assert written == n - PERIOD

    row = conn.execute(
        "SELECT feature_date, available_at, feature_name FROM feature_store "
        "ORDER BY feature_date LIMIT 1"
    ).fetchone()
    assert row[0] == dates[PERIOD]
    assert row[1] == ingested[PERIOD]
    assert row[2] == "rsi_14"
