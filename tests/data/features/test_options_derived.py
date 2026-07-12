from datetime import date, datetime, timedelta, timezone

import duckdb
import polars as pl

from stockmoney.data.db import append_rows, run_migrations
from stockmoney.data.features.options_derived import (
    GEX_FEATURE_NAME,
    PUT_CALL_RATIO_FEATURE_NAME,
    SKEW_CHG_FEATURE_NAME,
    SKEW_LEVEL_FEATURE_NAME,
    compute_gex_feature,
    compute_put_call_ratio_feature,
    compute_skew_feature,
)

SYMBOL = "SOXL"


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _dates(n, start=date(2026, 1, 1)):
    return [start + timedelta(days=i) for i in range(n)]


def _ingested(dates):
    return [datetime(d.year, d.month, d.day, 21, tzinfo=timezone.utc) for d in dates]


def _seed_derived_metric(conn, metric_name, values, symbol=SYMBOL):
    dates = _dates(len(values))
    ingested = _ingested(dates)
    append_rows(conn, "options_derived_daily", pl.DataFrame({
        "symbol": [symbol] * len(values),
        "trade_date": dates,
        "metric_name": [metric_name] * len(values),
        "metric_value": values,
        "method_version": ["v1_test"] * len(values),
        "source": ["test"] * len(values),
        "ingested_at": ingested,
    }))
    return dates


def test_compute_gex_feature_writes_one_row_per_date():
    conn = _conn()
    dates = _seed_derived_metric(conn, "gex_estimate", [1.0, -2.0, 3.5])
    written = compute_gex_feature(conn, symbols=[SYMBOL])
    assert written == 3

    rows = conn.execute(
        "SELECT feature_date, feature_value FROM feature_store WHERE feature_name = ? ORDER BY feature_date",
        [GEX_FEATURE_NAME],
    ).fetchall()
    assert rows == list(zip(dates, [1.0, -2.0, 3.5]))


def test_compute_gex_feature_dedupes_to_latest_ingested_at():
    conn = _conn()
    d = date(2026, 1, 1)
    ia_old = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
    ia_new = datetime(2026, 1, 1, 22, tzinfo=timezone.utc)
    append_rows(conn, "options_derived_daily", pl.DataFrame({
        "symbol": [SYMBOL, SYMBOL], "trade_date": [d, d],
        "metric_name": ["gex_estimate", "gex_estimate"], "metric_value": [1.0, 9.0],
        "method_version": ["v1_test", "v1_test"], "source": ["test", "test"],
        "ingested_at": [ia_old, ia_new],
    }))
    compute_gex_feature(conn, symbols=[SYMBOL])
    value = conn.execute(
        "SELECT feature_value FROM feature_store WHERE feature_name = ?", [GEX_FEATURE_NAME]
    ).fetchone()[0]
    assert value == 9.0  # the later ingest wins, not the first-written row


def test_compute_skew_feature_writes_level_and_change():
    conn = _conn()
    dates = _seed_derived_metric(conn, "skew_25delta", [0.05, 0.08, 0.03])
    written = compute_skew_feature(conn, symbols=[SYMBOL])
    assert written == 3 + 2  # 3 level rows + 2 change rows (first date has no prior)

    level = conn.execute(
        "SELECT feature_date, feature_value FROM feature_store WHERE feature_name = ? ORDER BY feature_date",
        [SKEW_LEVEL_FEATURE_NAME],
    ).fetchall()
    assert level == list(zip(dates, [0.05, 0.08, 0.03]))

    chg = conn.execute(
        "SELECT feature_date, feature_value FROM feature_store WHERE feature_name = ? ORDER BY feature_date",
        [SKEW_CHG_FEATURE_NAME],
    ).fetchall()
    assert chg[0][0] == dates[1]
    assert abs(chg[0][1] - 0.03) < 1e-9   # 0.08 - 0.05
    assert abs(chg[1][1] - (-0.05)) < 1e-9  # 0.03 - 0.08


def test_compute_put_call_ratio_feature_computes_volume_ratio():
    conn = _conn()
    dates = _dates(2)
    ingested = _ingested(dates)
    append_rows(conn, "put_call_ratio_daily", pl.DataFrame({
        "symbol": [SYMBOL, SYMBOL], "trade_date": dates,
        "put_volume": [200, 50], "call_volume": [100, 100],
        "put_oi": [None, None], "call_oi": [None, None],
        "source": ["test", "test"], "ingested_at": ingested,
    }))
    written = compute_put_call_ratio_feature(conn, symbols=[SYMBOL])
    assert written == 2

    rows = dict(conn.execute(
        "SELECT feature_date, feature_value FROM feature_store WHERE feature_name = ?",
        [PUT_CALL_RATIO_FEATURE_NAME],
    ).fetchall())
    assert rows[dates[0]] == 2.0   # 200/100
    assert rows[dates[1]] == 0.5   # 50/100


def test_compute_put_call_ratio_feature_skips_zero_call_volume():
    conn = _conn()
    dates = _dates(1)
    append_rows(conn, "put_call_ratio_daily", pl.DataFrame({
        "symbol": [SYMBOL], "trade_date": dates,
        "put_volume": [10], "call_volume": [0],
        "put_oi": [None], "call_oi": [None],
        "source": ["test"], "ingested_at": _ingested(dates),
    }))
    written = compute_put_call_ratio_feature(conn, symbols=[SYMBOL])
    assert written == 0


def test_compute_functions_are_idempotent_across_two_runs():
    conn = _conn()
    _seed_derived_metric(conn, "gex_estimate", [1.0, 2.0])
    first = compute_gex_feature(conn, symbols=[SYMBOL])
    second = compute_gex_feature(conn, symbols=[SYMBOL])
    assert first == 2
    assert second == 0  # nothing new to add on a re-run
