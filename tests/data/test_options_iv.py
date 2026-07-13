from datetime import date, datetime, timezone

import duckdb
import polars as pl
import pytest

from stockmoney.data.db import append_rows, run_migrations
from stockmoney.data.options_iv import (
    IV_RV_RATIO,
    entry_iv_for_symbol,
    entry_iv_proxy,
    real_entry_iv_for_symbol,
)

SYMBOL = "NVDA"
TRADE_DATE = date(2026, 7, 12)


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _seed_iv(conn, rows: list[dict]):
    av = datetime(2026, 7, 12, 21, tzinfo=timezone.utc)
    append_rows(conn, "iv_surface_daily", pl.DataFrame({
        "symbol": [r["symbol"] for r in rows],
        "trade_date": [r["trade_date"] for r in rows],
        "expiry_date": [r["expiry_date"] for r in rows],
        "delta_bucket": [r["delta_bucket"] for r in rows],
        "implied_vol": [r["implied_vol"] for r in rows],
        "source": ["test"] * len(rows),
        "ingested_at": [av] * len(rows),
    }))


def test_real_entry_iv_returns_none_when_no_snapshot_exists():
    conn = _conn()
    assert real_entry_iv_for_symbol(conn, SYMBOL, TRADE_DATE) is None


def test_real_entry_iv_reads_the_50_delta_bucket():
    conn = _conn()
    _seed_iv(conn, [
        {"symbol": SYMBOL, "trade_date": TRADE_DATE, "expiry_date": date(2026, 8, 11),
         "delta_bucket": "50", "implied_vol": 0.42},
        {"symbol": SYMBOL, "trade_date": TRADE_DATE, "expiry_date": date(2026, 8, 11),
         "delta_bucket": "25c", "implied_vol": 0.50},
    ])
    assert real_entry_iv_for_symbol(conn, SYMBOL, TRADE_DATE) == pytest.approx(0.42)


def test_real_entry_iv_picks_expiry_closest_to_target_dte():
    conn = _conn()
    _seed_iv(conn, [
        {"symbol": SYMBOL, "trade_date": TRADE_DATE, "expiry_date": TRADE_DATE.replace(day=19),
         "delta_bucket": "50", "implied_vol": 0.30},   # 7 DTE, far from 30
        {"symbol": SYMBOL, "trade_date": TRADE_DATE, "expiry_date": date(2026, 8, 10),
         "delta_bucket": "50", "implied_vol": 0.40},   # 29 DTE, closest to 30
        {"symbol": SYMBOL, "trade_date": TRADE_DATE, "expiry_date": date(2026, 10, 9),
         "delta_bucket": "50", "implied_vol": 0.35},   # ~89 DTE, far from 30
    ])
    assert real_entry_iv_for_symbol(conn, SYMBOL, TRADE_DATE, target_dte_days=30) == pytest.approx(0.40)


def test_real_entry_iv_never_uses_a_different_days_snapshot():
    """The point-in-time guard: a snapshot from a different trade_date (even
    the day before) must never be picked, even if it's the only data present."""
    conn = _conn()
    _seed_iv(conn, [
        {"symbol": SYMBOL, "trade_date": TRADE_DATE.replace(day=11), "expiry_date": date(2026, 8, 10),
         "delta_bucket": "50", "implied_vol": 0.99},
    ])
    assert real_entry_iv_for_symbol(conn, SYMBOL, TRADE_DATE) is None


def test_entry_iv_proxy_matches_ratio():
    assert entry_iv_proxy(0.30) == pytest.approx(0.30 * IV_RV_RATIO)


def test_entry_iv_for_symbol_prefers_real_over_proxy():
    conn = _conn()
    _seed_iv(conn, [
        {"symbol": SYMBOL, "trade_date": TRADE_DATE, "expiry_date": date(2026, 8, 10),
         "delta_bucket": "50", "implied_vol": 0.40},
    ])
    iv, source = entry_iv_for_symbol(conn, SYMBOL, TRADE_DATE, realized_vol_20d=0.20)
    assert source == "real"
    assert iv == pytest.approx(0.40)


def test_entry_iv_for_symbol_falls_back_to_proxy():
    conn = _conn()
    iv, source = entry_iv_for_symbol(conn, SYMBOL, TRADE_DATE, realized_vol_20d=0.20)
    assert source == "proxy"
    assert iv == pytest.approx(0.20 * IV_RV_RATIO)
