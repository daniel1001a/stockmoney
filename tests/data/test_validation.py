from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from stockmoney.data.db import DEFAULT_DB_PATH, run_migrations
from stockmoney.data.integrity_fixes import run_all_strike_and_expiry_fixes
from stockmoney.data.validation import (
    Violation,
    assert_clean,
    expiry_violation,
    future_timestamp_violation,
    portfolio_stats_violation,
    premium_violation,
    realized_pnl_violation,
    strike_violation,
    validate_all,
    validate_ohlcv,
    validate_option_positions,
    validate_trader_trades,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ============================================================================
# Synthetic bad inputs -- each pure check must catch its violation.
# ============================================================================

def test_strike_violation_catches_the_reported_impossible_strikes():
    assert strike_violation("t", "r1", 996.7) is not None    # NFLX
    assert strike_violation("t", "r2", 234.6) is not None    # TSLA
    assert strike_violation("t", "r3", 247.3) is not None    # SOXX


def test_strike_violation_clean_for_listed_strikes():
    assert strike_violation("t", "r1", 1000.0) is None
    assert strike_violation("t", "r2", 235.0) is None


def test_strike_violation_catches_non_positive():
    v = strike_violation("t", "r1", 0.0)
    assert v is not None and v.field == "strike"
    assert strike_violation("t", "r2", -5.0) is not None


def test_expiry_violation_catches_non_friday():
    v = expiry_violation("t", "r1", date(2026, 7, 4), "open", as_of=date(2026, 6, 1))  # Saturday
    assert v is not None
    assert "Friday" in v.detail


def test_expiry_violation_clean_for_friday():
    assert expiry_violation("t", "r1", date(2026, 7, 24), "open", as_of=date(2026, 6, 1)) is None


def test_expiry_violation_catches_expired_open_position():
    v = expiry_violation("t", "r1", date(2026, 6, 5), "open", as_of=date(2026, 7, 15))  # Friday but past
    assert v is not None
    assert "as_of" in v.detail


def test_expiry_violation_allows_expired_closed_position():
    # A closed trade's expiry being "in the past" relative to today is normal.
    assert expiry_violation("t", "r1", date(2026, 6, 5), "closed", as_of=date(2026, 7, 15)) is None


def test_premium_violation_catches_premium_exceeding_spot():
    v = premium_violation("t", "r1", 150.0, spot=100.0)
    assert v is not None


def test_premium_violation_catches_zero_or_negative():
    assert premium_violation("t", "r1", 0.0, spot=100.0) is not None
    assert premium_violation("t", "r2", -1.0, spot=100.0) is not None


def test_premium_violation_clean_for_plausible_premium():
    assert premium_violation("t", "r1", 5.5, spot=100.0) is None


def test_future_timestamp_violation_catches_future_entry():
    future = datetime.now(timezone.utc) + timedelta(days=5)
    v = future_timestamp_violation("t", "r1", "entry_at", future)
    assert v is not None


def test_future_timestamp_violation_clean_for_past():
    past = datetime.now(timezone.utc) - timedelta(days=5)
    assert future_timestamp_violation("t", "r1", "entry_at", past) is None


def test_realized_pnl_violation_catches_sign_mismatch_long():
    # Long call, premium went UP (profit), but realized_pnl reported negative.
    v = realized_pnl_violation(
        "t", "r1", side="long", contracts=1, entry_premium=5.0, exit_premium=9.0, realized_pnl=-400.0,
    )
    assert v is not None


def test_realized_pnl_violation_clean_when_consistent():
    # (9.0 - 5.0) * 100 * 1 = 400.0
    v = realized_pnl_violation(
        "t", "r1", side="long", contracts=1, entry_premium=5.0, exit_premium=9.0, realized_pnl=400.0,
    )
    assert v is None


def test_realized_pnl_violation_handles_short_side_sign_convention():
    # Short: collected 5.0, bought back at 3.0 -> profit (entry - exit) * 100 = 200
    assert realized_pnl_violation(
        "t", "r1", side="short", contracts=1, entry_premium=5.0, exit_premium=3.0, realized_pnl=200.0,
    ) is None
    assert realized_pnl_violation(
        "t", "r2", side="short", contracts=1, entry_premium=5.0, exit_premium=3.0, realized_pnl=-200.0,
    ) is not None


def test_portfolio_stats_violation_catches_wins_losses_exceeding_closed():
    v = portfolio_stats_violation(
        "t", "trader1", n_closed=5, wins=4, losses=3, cum_pnl=100.0, trade_pnls=[100.0],
    )
    assert v is not None


def test_portfolio_stats_violation_catches_cum_pnl_mismatch():
    v = portfolio_stats_violation(
        "t", "trader1", n_closed=2, wins=1, losses=1, cum_pnl=999.0, trade_pnls=[100.0, -50.0],
    )
    assert v is not None
    assert "cum_pnl" in v.field


def test_portfolio_stats_violation_clean_when_consistent():
    v = portfolio_stats_violation(
        "t", "trader1", n_closed=2, wins=1, losses=1, cum_pnl=50.0, trade_pnls=[100.0, -50.0],
    )
    assert v is None


def test_assert_clean_raises_with_detail_on_errors():
    with pytest.raises(AssertionError, match="strike"):
        assert_clean([Violation("trader_trades", "strike", "r1", "strike 996.7 is not on a valid listed increment")])


def test_assert_clean_does_not_raise_on_warnings_only():
    assert_clean([Violation("t", "f", "r1", "just fyi", severity="warning")])  # must not raise


def test_assert_clean_passes_on_empty_list():
    assert_clean([])  # must not raise


# ============================================================================
# DB-level checks: synthetic in-memory DB with deliberately bad rows.
# ============================================================================

def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _insert_trade(conn, trade_id, **overrides):
    defaults = dict(
        trader_id="chartist", symbol="NFLX", option_right="call", side="long",
        strike=996.7, expiry_date=date(2026, 7, 4), contracts=1,
        entry_at=datetime(2026, 6, 1, tzinfo=timezone.utc), entry_underlying=967.65,
        entry_premium=20.0, exit_at=None, exit_underlying=None, exit_premium=None,
        realized_pnl=None, status="open", thesis="t", exit_reason=None,
        linked_prediction_id=None, created_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    conn.execute(
        """INSERT INTO trader_trades
        (trade_id, trader_id, symbol, option_right, side, strike, expiry_date, contracts,
         entry_at, entry_underlying, entry_premium, exit_at, exit_underlying, exit_premium,
         realized_pnl, status, thesis, exit_reason, linked_prediction_id, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            trade_id, defaults["trader_id"], defaults["symbol"], defaults["option_right"],
            defaults["side"], defaults["strike"], defaults["expiry_date"], defaults["contracts"],
            defaults["entry_at"], defaults["entry_underlying"], defaults["entry_premium"],
            defaults["exit_at"], defaults["exit_underlying"], defaults["exit_premium"],
            defaults["realized_pnl"], defaults["status"], defaults["thesis"], defaults["exit_reason"],
            defaults["linked_prediction_id"], defaults["created_at"],
        ],
    )


def test_validate_trader_trades_catches_impossible_strike():
    conn = _conn()
    _insert_trade(conn, "t1", strike=996.7, expiry_date=date(2026, 7, 24))
    violations = validate_trader_trades(conn, as_of=date(2026, 6, 1))
    assert any(v.field == "strike" for v in violations)


def test_validate_trader_trades_catches_non_friday_expiry():
    conn = _conn()
    _insert_trade(conn, "t1", strike=1000.0, expiry_date=date(2026, 7, 4))
    violations = validate_trader_trades(conn, as_of=date(2026, 6, 1))
    assert any(v.field == "expiry_date" for v in violations)


def test_validate_trader_trades_catches_premium_exceeding_spot():
    conn = _conn()
    _insert_trade(conn, "t1", strike=1000.0, expiry_date=date(2026, 7, 24), entry_premium=2000.0)
    violations = validate_trader_trades(conn, as_of=date(2026, 6, 1))
    assert any(v.field == "entry_premium" for v in violations)


def test_validate_trader_trades_catches_bad_realized_pnl():
    conn = _conn()
    _insert_trade(
        conn, "t1", strike=1000.0, expiry_date=date(2026, 7, 24), status="closed",
        exit_at=datetime(2026, 6, 5, tzinfo=timezone.utc), exit_underlying=970.0,
        exit_premium=25.0, realized_pnl=-99999.0,
    )
    violations = validate_trader_trades(conn, as_of=date(2026, 6, 1))
    assert any(v.field == "realized_pnl" for v in violations)


def test_validate_trader_trades_clean_row_is_clean():
    conn = _conn()
    _insert_trade(
        conn, "t1", strike=1000.0, expiry_date=date(2026, 7, 24), status="closed",
        exit_at=datetime(2026, 6, 5, tzinfo=timezone.utc), exit_underlying=970.0,
        entry_premium=20.0, exit_premium=25.0, realized_pnl=500.0,
    )
    violations = validate_trader_trades(conn, as_of=date(2026, 6, 1))
    assert violations == []


def _insert_ohlcv(conn, symbol, trade_date, **overrides):
    defaults = dict(
        open=100.0, high=101.0, low=99.0, close=100.5, adj_close=100.5,
        volume=1_000_000, source="yfinance", ingested_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    conn.execute(
        """INSERT INTO ohlcv_daily
        (symbol, trade_date, open, high, low, close, adj_close, volume, source, ingested_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        [
            symbol, trade_date, defaults["open"], defaults["high"], defaults["low"],
            defaults["close"], defaults["adj_close"], defaults["volume"],
            defaults["source"], defaults["ingested_at"],
        ],
    )


def test_validate_ohlcv_catches_nan_close():
    """2026-07-15 incident regression: yfinance partial bar with valid
    Open/High/Low/Volume but NaN Close must be flagged."""
    conn = _conn()
    _insert_ohlcv(conn, "XOM", date(2026, 7, 14), close=float("nan"))
    violations = validate_ohlcv(conn)
    assert len(violations) == 1
    assert violations[0].table == "ohlcv_daily"
    assert "close" in violations[0].field


def test_validate_ohlcv_catches_nan_in_other_ohlc_fields():
    conn = _conn()
    _insert_ohlcv(conn, "CVX", date(2026, 7, 14), open=float("nan"))
    violations = validate_ohlcv(conn)
    assert len(violations) == 1
    assert "open" in violations[0].field


def test_validate_ohlcv_clean_row_is_clean():
    conn = _conn()
    _insert_ohlcv(conn, "XOM", date(2026, 7, 14))
    assert validate_ohlcv(conn) == []


def test_validate_all_includes_ohlcv_violations():
    conn = _conn()
    _insert_ohlcv(conn, "XOM", date(2026, 7, 14), close=float("nan"))
    violations = validate_all(conn, as_of=date(2026, 7, 15))
    assert any(v.table == "ohlcv_daily" for v in violations)


# ============================================================================
# Live DuckDB (read-only): validate + prove the migration cleans it.
# ============================================================================

_LIVE_DB_CANDIDATES = [
    REPO_ROOT / "data" / "stockmoney.duckdb",
    REPO_ROOT / "data" / "stockmoney_live.duckdb",
]


def _first_existing_live_db() -> Path | None:
    for p in _LIVE_DB_CANDIDATES:
        if p.exists():
            return p
    return None


def _clone_into_memory(ro_conn: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    """Read trader_trades/option_positions out of a read-only connection to
    the live DB and copy them into a fresh in-memory DB. This lets the test
    both (a) genuinely read the live data, on-disk file untouched, and (b) run
    the mutating migration (run_all_strike_and_expiry_fixes) on a disposable
    copy without ever writing to the real file -- the migration itself is a
    separate, explicit, human-approved step against the real path (see
    integrity_fixes.py's module docstring for the one-liner)."""
    mem = duckdb.connect(":memory:")
    run_migrations(mem)
    for table in ("trader_trades", "option_positions"):
        cols = [
            r[0] for r in ro_conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='main' AND table_name=? ORDER BY ordinal_position",
                [table],
            ).fetchall()
        ]
        rows = ro_conn.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
        if not rows:
            continue
        placeholders = ", ".join(["?"] * len(cols))
        mem.executemany(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})", rows,
        )
    return mem


@pytest.mark.skipif(_first_existing_live_db() is None, reason="no local live/demo DuckDB file present")
def test_live_db_read_only_current_known_violations_are_only_strike_and_expiry():
    """Documents the CURRENT (pre-migration) state of the real DB honestly:
    as of this audit the only violation categories present are the ones this
    worker's migration (integrity_fixes.run_all_strike_and_expiry_fixes) is
    designed to fix -- off-ladder strikes and non-Friday expiries. Any OTHER
    violation category appearing here (bad premiums, future timestamps, P&L
    mismatches) would be a genuinely new problem and this test would (and
    should) fail loudly."""
    db_path = _first_existing_live_db()
    ro_conn = duckdb.connect(str(db_path), read_only=True)
    try:
        violations = validate_all(ro_conn, as_of=date(2026, 7, 15))
    finally:
        ro_conn.close()
    unexpected = [v for v in violations if v.field not in ("strike", "expiry_date")]
    assert unexpected == [], f"unexpected live-DB violations: {unexpected}"


@pytest.mark.skipif(_first_existing_live_db() is None, reason="no local live/demo DuckDB file present")
def test_migration_makes_the_real_live_data_pass_validation():
    """Proves the described fix actually works on real production-shaped
    data: clone the live DB's trader_trades/option_positions (read-only, file
    untouched) into memory, run the exact migration function, then assert the
    validator reports zero errors."""
    db_path = _first_existing_live_db()
    ro_conn = duckdb.connect(str(db_path), read_only=True)
    try:
        mem = _clone_into_memory(ro_conn)
    finally:
        ro_conn.close()

    run_all_strike_and_expiry_fixes(mem)
    violations = validate_all(mem, as_of=date(2026, 7, 15))
    errors = [v for v in violations if v.severity == "error"]
    assert errors == [], f"still-broken rows after migration: {errors}"
