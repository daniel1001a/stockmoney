from datetime import date, datetime, timezone

import duckdb

from stockmoney.data.db import MARKET_SYMBOL, run_migrations
from stockmoney.data.features.base import FeatureValue, write_features


def _migrated_conn() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def test_write_features_numeric_and_categorical():
    conn = _migrated_conn()
    known_at = datetime(2026, 1, 2, 21, 0, tzinfo=timezone.utc)

    n = write_features(
        conn,
        feature_name="realized_vol_20d",
        feature_version="v1",
        source_table="ohlcv_daily",
        values=[
            FeatureValue(date(2026, 1, 2), "NVDA", value=0.42, available_at=known_at),
            FeatureValue(date(2026, 1, 2), MARKET_SYMBOL, value_text="trend_up", available_at=known_at),
        ],
    )
    assert n == 2

    numeric = conn.execute(
        "SELECT feature_value, feature_value_text, available_at, source_table "
        "FROM feature_store WHERE symbol = 'NVDA'"
    ).fetchone()
    assert numeric[0] == 0.42
    assert numeric[1] is None
    assert numeric[2] == known_at
    assert numeric[3] == "ohlcv_daily"

    categorical = conn.execute(
        "SELECT feature_value, feature_value_text FROM feature_store WHERE symbol = ?",
        [MARKET_SYMBOL],
    ).fetchone()
    assert categorical[0] is None
    assert categorical[1] == "trend_up"


def test_write_features_empty_list_is_noop():
    conn = _migrated_conn()
    assert write_features(
        conn, feature_name="x", feature_version="v1", source_table="t", values=[]
    ) == 0
    assert conn.execute("SELECT count(*) FROM feature_store").fetchone()[0] == 0


def test_write_features_skips_already_written_keys():
    conn = _migrated_conn()
    v1 = FeatureValue(date(2026, 1, 2), "NVDA", value=0.1)
    v2 = FeatureValue(date(2026, 1, 3), "NVDA", value=0.2)

    first = write_features(
        conn, feature_name="realized_vol_20d", feature_version="v1",
        source_table="ohlcv_daily", values=[v1, v2],
    )
    assert first == 2

    # Re-run over the same window plus one genuinely new date: only the new
    # date should be written, and the existing rows must be untouched.
    v3 = FeatureValue(date(2026, 1, 4), "NVDA", value=0.3)
    second = write_features(
        conn, feature_name="realized_vol_20d", feature_version="v1",
        source_table="ohlcv_daily", values=[v1, v2, v3],
    )
    assert second == 1
    assert conn.execute("SELECT count(*) FROM feature_store").fetchone()[0] == 3
    unchanged = conn.execute(
        "SELECT feature_value FROM feature_store WHERE feature_date = '2026-01-02'"
    ).fetchone()[0]
    assert unchanged == 0.1


def test_write_features_defaults_available_at_to_now():
    conn = _migrated_conn()
    before = datetime.now(timezone.utc)
    write_features(
        conn,
        feature_name="x",
        feature_version="v1",
        source_table="t",
        values=[FeatureValue(date(2026, 1, 2), "NVDA", value=1.0)],
    )
    after = datetime.now(timezone.utc)
    available_at = conn.execute("SELECT available_at FROM feature_store").fetchone()[0]
    assert before <= available_at <= after
