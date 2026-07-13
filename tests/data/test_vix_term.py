"""Tests for the VIX term-structure feature (options-microstructure candidate).

Locks in the two things that matter: the slope math, and the look-ahead
guarantee (each feature_date uses only that day's index closes, and
available_at is pinned to the source ingested_at -- never the future)."""
from __future__ import annotations

from datetime import date, datetime, timezone

import duckdb

from stockmoney.data.db import MARKET_SYMBOL, run_migrations
from stockmoney.data.features.vix_term import compute_vix_term_features

IA = datetime(2026, 7, 10, 21, 0, tzinfo=timezone.utc)


def _seed(conn, rows):
    conn.executemany(
        "INSERT INTO vix_term_structure_daily (trade_date, tenor_days, vix_value, source, ingested_at) "
        "VALUES (?,?,?,?,?)",
        rows,
    )


def _feat(conn, name):
    return {
        r[0]: (r[1], r[2])
        for r in conn.execute(
            "SELECT feature_date, feature_value, available_at FROM feature_store "
            "WHERE feature_name = ? AND symbol = ?",
            [name, MARKET_SYMBOL],
        ).fetchall()
    }


def test_slope_math_and_availability():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    d = date(2026, 7, 10)
    # VIX9D=10, VIX3M=20 -> front slope = 20/10 - 1 = 1.0
    # VIX3M=20, VIX6M=25 -> back slope  = 25/20 - 1 = 0.25
    _seed(conn, [
        (d, 9, 10.0, "yfinance", IA),
        (d, 30, 15.0, "yfinance", IA),
        (d, 90, 20.0, "yfinance", IA),
        (d, 180, 25.0, "yfinance", IA),
    ])
    compute_vix_term_features(conn)

    slope = _feat(conn, "vix_term_slope")
    back = _feat(conn, "vix_term_slope_back")
    assert abs(slope[d][0] - 1.0) < 1e-9
    assert abs(back[d][0] - 0.25) < 1e-9
    # available_at is the source ingested_at, not the future
    assert slope[d][1] == IA


def test_missing_tenor_skips_that_slope():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    d = date(2026, 7, 10)
    # only front tenors present -> front slope computed, back slope absent
    _seed(conn, [(d, 9, 12.0, "yfinance", IA), (d, 90, 18.0, "yfinance", IA)])
    compute_vix_term_features(conn)
    assert d in _feat(conn, "vix_term_slope")
    assert d not in _feat(conn, "vix_term_slope_back")


def test_only_same_day_values_used():
    """A future day's index closes must never influence an earlier day's slope."""
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    d1, d2 = date(2026, 7, 9), date(2026, 7, 10)
    _seed(conn, [
        (d1, 9, 10.0, "yfinance", IA), (d1, 90, 10.0, "yfinance", IA),   # flat -> slope 0
        (d2, 9, 10.0, "yfinance", IA), (d2, 90, 30.0, "yfinance", IA),   # steep -> slope 2
    ])
    compute_vix_term_features(conn)
    slope = _feat(conn, "vix_term_slope")
    assert abs(slope[d1][0] - 0.0) < 1e-9
    assert abs(slope[d2][0] - 2.0) < 1e-9
