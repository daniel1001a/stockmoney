import duckdb

from stockmoney.data.db import run_migrations
from stockmoney.data.watchlist import sector_for_symbol


def _migrated_conn() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def test_sector_for_symbol_returns_active_member_sector():
    conn = _migrated_conn()
    assert sector_for_symbol(conn, "AAPL") == "big_tech"


def test_sector_for_symbol_aliases_etf_sector_to_feature_group():
    # SOXL's watchlist taxonomy label is "semiconductor_etf", but the
    # xsec_dispersion feature was only ever computed for the "semiconductor"
    # group -- production.predict_latest needs the aliased value, not the
    # literal watchlist label, or it silently finds nothing.
    conn = _migrated_conn()
    assert sector_for_symbol(conn, "soxl") == "semiconductor"
    assert sector_for_symbol(conn, "SOXS") == "semiconductor"


def test_sector_for_symbol_none_for_unknown_symbol():
    conn = _migrated_conn()
    assert sector_for_symbol(conn, "ZZZZ") is None


def test_sector_for_symbol_ignores_removed_members():
    conn = _migrated_conn()
    conn.execute(
        "UPDATE watchlist_members SET removed_date = CURRENT_DATE WHERE symbol = 'SOXL'"
    )
    assert sector_for_symbol(conn, "SOXL") is None
