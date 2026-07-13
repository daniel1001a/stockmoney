from datetime import date, datetime, timezone

import duckdb
import polars as pl
import pytest

from stockmoney.data.db import MARKET_SYMBOL, append_rows, run_migrations

EXPECTED_TABLES = {
    "watchlist_members",
    "ohlcv_daily",
    "iv_surface_daily",
    "put_call_ratio_daily",
    "vix_term_structure_daily",
    "options_derived_daily",
    "macro_series_daily",
    "fund_flow_etf_daily",
    "fund_flow_futures_oi_weekly",
    "alt_social_hourly",
    "alt_developer_daily",
    "alt_search_daily",
    "event_calendar",
    "event_news_gdelt",
    "feature_store",
    # Tier 1 additions
    "attribution_log",
    "feature_candidates",
    "ingestion_runs",
    # Round 6: social/media discovery engine
    "social_posts_raw",
    "news_articles_raw",
    "watchlist_candidates",
    "option_positions",
    "daily_predictions",
    "scan_classifications",
    "symbol_backtest_snapshot",
    "catalyst_signals",
    "calibration_runs",
    "calibration_candidates",
    # Trader League Arena (worker-1)
    "traders",
    "trader_predictions",
    "trader_method_versions",
    "trader_method_proposals",
    "trader_review_log",
    "trader_divergence_log",
    # Wave C: market-level VRP (models/vrp.py)
    "market_index_ohlcv_daily",
}

# 15 base migrations + 3 Tier1 tables + 1 ALTER + 3 round-6 tables + 1 options-risk table
# + 1 predictions table + 1 scan_classifications table + 1 symbol_backtest_snapshot table
# + 1 catalyst_signals table + 1 event_news_gdelt weighting-columns ALTER
# + 1 calibration_campaign migration (2 tables) = 29
# + 4 Trader League migrations (030 traders, 031 trader_predictions,
#   032 trader_methods [2 tables], 033 trader_review [2 tables]) = 33
# + 2 redesign migrations (034 news_items, 035 trader_portfolios [2 tables]) = 35
# + 1 Wave C migration (036 market_index_ohlcv_daily) = 36
EXPECTED_MIGRATION_COUNT = 36


def _table_names(conn: duckdb.DuckDBPyConnection) -> set[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()
    return {row[0] for row in rows}


def _migrated_conn() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def test_run_migrations_creates_all_tables():
    conn = _migrated_conn()

    tables = _table_names(conn)
    assert EXPECTED_TABLES <= tables
    assert "_schema_migrations" in tables
    assert (
        len(conn.execute("SELECT * FROM _schema_migrations").fetchall())
        == EXPECTED_MIGRATION_COUNT
    )


def test_watchlist_seed_data():
    conn = _migrated_conn()

    rows = conn.execute("SELECT symbol, tier FROM watchlist_members").fetchall()
    assert len(rows) == 11
    assert all(tier == "core" for _, tier in rows)
    symbols = {symbol for symbol, _ in rows}
    assert symbols == {
        "NVDA", "AVGO", "AMD", "TSM", "SOXL", "SOXS",
        "AAPL", "MSFT", "GOOGL", "META", "AMZN",
    }


def test_run_migrations_is_idempotent():
    conn = _migrated_conn()
    run_migrations(conn)

    assert (
        len(conn.execute("SELECT * FROM _schema_migrations").fetchall())
        == EXPECTED_MIGRATION_COUNT
    )
    assert len(conn.execute("SELECT * FROM watchlist_members").fetchall()) == 11


def test_feature_store_has_value_text_column():
    conn = _migrated_conn()
    cols = {
        row[0]
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'feature_store'"
        ).fetchall()
    }
    assert "feature_value" in cols
    assert "feature_value_text" in cols


def test_all_migrations_have_checksum():
    conn = _migrated_conn()
    null_checksums = conn.execute(
        "SELECT count(*) FROM _schema_migrations WHERE checksum IS NULL"
    ).fetchone()[0]
    assert null_checksums == 0


def test_checksum_detects_tampered_applied_migration():
    conn = _migrated_conn()
    # Simulate someone editing an already-applied migration file: corrupt the
    # stored checksum so the current file no longer matches.
    conn.execute(
        "UPDATE _schema_migrations SET checksum = 'deadbeef' WHERE filename = '001_watchlist.sql'"
    )
    with pytest.raises(RuntimeError, match="modified after being applied"):
        run_migrations(conn)


def test_legacy_null_checksum_is_backfilled_not_rejected():
    conn = _migrated_conn()
    # Simulate a DB migrated before the checksum feature existed.
    conn.execute("UPDATE _schema_migrations SET checksum = NULL")
    run_migrations(conn)  # must not raise
    null_checksums = conn.execute(
        "SELECT count(*) FROM _schema_migrations WHERE checksum IS NULL"
    ).fetchone()[0]
    assert null_checksums == 0


def test_append_rows_inserts_and_autofills_ingested_at():
    conn = _migrated_conn()
    df = pl.DataFrame(
        {
            "symbol": ["NVDA", "NVDA"],
            "trade_date": [date(2026, 1, 2), date(2026, 1, 3)],
            "close": [140.0, 142.5],
            "source": ["yfinance", "yfinance"],
        }
    )
    n = append_rows(conn, "ohlcv_daily", df)
    assert n == 2

    rows = conn.execute(
        "SELECT symbol, close, source, ingested_at FROM ohlcv_daily ORDER BY trade_date"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0][0] == "NVDA"
    # ingested_at auto-filled (not null)
    assert rows[0][3] is not None


def test_append_rows_respects_explicit_ingested_at():
    conn = _migrated_conn()
    stamp = datetime(2026, 1, 2, 21, 0, tzinfo=timezone.utc)
    df = pl.DataFrame(
        {"symbol": ["AMD"], "trade_date": [date(2026, 1, 2)], "source": ["yfinance"]}
    )
    append_rows(conn, "ohlcv_daily", df, ingested_at=stamp)
    got = conn.execute("SELECT ingested_at FROM ohlcv_daily").fetchone()[0]
    assert got == stamp


def test_append_rows_rejects_unknown_column():
    conn = _migrated_conn()
    df = pl.DataFrame({"symbol": ["NVDA"], "not_a_column": [1]})
    with pytest.raises(ValueError, match="not_a_column"):
        append_rows(conn, "ohlcv_daily", df)


def test_append_rows_rejects_unknown_table():
    conn = _migrated_conn()
    df = pl.DataFrame({"a": [1]})
    with pytest.raises(ValueError, match="Unknown table"):
        append_rows(conn, "nope_table", df)


def test_append_rows_empty_df_is_noop():
    conn = _migrated_conn()
    df = pl.DataFrame({"symbol": [], "trade_date": [], "source": []})
    assert append_rows(conn, "ohlcv_daily", df) == 0


def test_append_rows_feature_store_categorical_via_value_text():
    conn = _migrated_conn()
    now = datetime.now(timezone.utc)
    df = pl.DataFrame(
        {
            "feature_date": [date(2026, 1, 2)],
            "symbol": [MARKET_SYMBOL],
            "feature_name": ["market_regime"],
            "feature_value_text": ["trend_up"],
            "feature_version": ["v1"],
            "available_at": [now],
            "computed_at": [now],
            "source_table": ["regime_model"],
        }
    )
    assert append_rows(conn, "feature_store", df) == 1
    val = conn.execute(
        "SELECT feature_value_text FROM feature_store WHERE feature_name = 'market_regime'"
    ).fetchone()[0]
    assert val == "trend_up"
