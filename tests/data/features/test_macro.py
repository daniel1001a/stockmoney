from datetime import date, datetime, timedelta, timezone

import duckdb
import polars as pl
import pytest

from stockmoney.data.db import MARKET_SYMBOL, append_rows, run_migrations
from stockmoney.data.features.macro import compute_macro_features


def _seed_series(conn, series_id: str, values: list[float], start: date, *, vintage_offset=0):
    n = len(values)
    dates = [start + timedelta(days=i) for i in range(n)]
    ingested = [datetime(d.year, d.month, d.day, 20, tzinfo=timezone.utc) for d in dates]
    vintages = [d + timedelta(days=vintage_offset) for d in dates]
    append_rows(conn, "macro_series_daily", pl.DataFrame({
        "series_id": [series_id] * n,
        "observation_date": dates,
        "value": values,
        "vintage_date": vintages,
        "source": ["fred"] * n,
        "ingested_at": ingested,
    }))
    return dates


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def test_yield_curve_spread_matches_manual_calc():
    conn = _conn()
    start = date(2026, 1, 1)
    _seed_series(conn, "DGS10", [4.5, 4.6, 4.4], start)
    _seed_series(conn, "DGS2", [4.0, 4.2, 4.1], start)

    n = compute_macro_features(conn)
    assert n >= 3

    rows = conn.execute(
        "SELECT feature_date, feature_value FROM feature_store "
        "WHERE feature_name = 'yield_curve_10y2y' ORDER BY feature_date"
    ).fetchall()
    assert [r[0] for r in rows] == [start, start + timedelta(days=1), start + timedelta(days=2)]
    assert [r[1] for r in rows] == pytest.approx([0.5, 0.4, 0.3])
    symbol = conn.execute(
        "SELECT DISTINCT symbol FROM feature_store WHERE feature_name = 'yield_curve_10y2y'"
    ).fetchone()[0]
    assert symbol == MARKET_SYMBOL


def test_momentum_feature_matches_manual_pct_change():
    conn = _conn()
    start = date(2026, 1, 1)
    _seed_series(conn, "DTWEXBGS", [100.0, 102.0, 99.96], start)

    compute_macro_features(conn)

    rows = conn.execute(
        "SELECT feature_date, feature_value FROM feature_store "
        "WHERE feature_name = 'dxy_chg_1d' ORDER BY feature_date"
    ).fetchall()
    # First day has no prior value -> no row. Day 2: 102/100-1=0.02. Day 3: 99.96/102-1.
    assert len(rows) == 2
    assert rows[0][1] == pytest.approx(0.02)
    assert rows[1][1] == pytest.approx(99.96 / 102.0 - 1.0)


def test_yield_curve_requires_both_series_on_same_date():
    conn = _conn()
    start = date(2026, 1, 1)
    _seed_series(conn, "DGS10", [4.5, 4.6], start)
    _seed_series(conn, "DGS2", [4.0], start)  # only 1 day overlaps

    compute_macro_features(conn)
    n = conn.execute(
        "SELECT count(*) FROM feature_store WHERE feature_name = 'yield_curve_10y2y'"
    ).fetchone()[0]
    assert n == 1


def _seed_trading_days(conn, days: list[date]):
    n = len(days)
    append_rows(conn, "ohlcv_daily", pl.DataFrame({
        "symbol": ["NVDA"] * n, "trade_date": days, "close": [100.0] * n,
        "source": ["test"] * n, "ingested_at": [datetime(d.year, d.month, d.day, tzinfo=timezone.utc) for d in days],
    }))


def test_ffill_momentum_reads_zero_on_no_print_days_and_true_change_on_print_day():
    """The bug this fixes: DTWEXBGS going several trading days without a
    print (a real FRED publication-lag characteristic) used to mean
    dxy_chg_1d_ffill had NO value at all for those dates, which stalled
    every downstream symbol's "today" in build_feature_matrix (every
    FEATURE_COLUMNS entry must be non-null). Forward-filling the raw level
    means those days read a legitimate 0% change instead of missing."""
    conn = _conn()
    start = date(2026, 1, 1)  # Thursday
    trading_days = [start, start + timedelta(days=1), start + timedelta(days=4),
                     start + timedelta(days=5), start + timedelta(days=6)]
    _seed_trading_days(conn, trading_days)
    # DTWEXBGS only prints on the first two days, then goes quiet for a
    # 3-day stretch before printing again on the last day.
    _seed_series(conn, "DTWEXBGS", [100.0, 102.0], start)  # dates: start, start+1

    compute_macro_features(conn)

    rows = dict(conn.execute(
        "SELECT feature_date, feature_value FROM feature_store WHERE feature_name = 'dxy_chg_1d_ffill'"
    ).fetchall())
    assert rows[trading_days[1]] == pytest.approx(0.02)     # real print: 102/100-1
    assert rows[trading_days[2]] == pytest.approx(0.0)      # no print yet -- carried forward, 0% change
    assert rows[trading_days[3]] == pytest.approx(0.0)      # still no print -- 0% change
    assert rows[trading_days[4]] == pytest.approx(0.0)      # DTWEXBGS never printed again in this fixture


def test_ffill_momentum_available_at_pinned_to_when_value_was_actually_known():
    """Look-ahead guarantee: a carried-forward day's available_at must stay
    at the original print's timestamp, never advance to the trading day
    itself (which would falsely claim same-day availability)."""
    conn = _conn()
    start = date(2026, 1, 1)
    trading_days = [start, start + timedelta(days=1), start + timedelta(days=2)]
    _seed_trading_days(conn, trading_days)
    _seed_series(conn, "DTWEXBGS", [100.0, 102.0], start)  # only start, start+1 print

    compute_macro_features(conn)

    available_at = dict(conn.execute(
        "SELECT feature_date, available_at FROM feature_store WHERE feature_name = 'dxy_chg_1d_ffill'"
    ).fetchall())
    # the carried-forward day (start+2) must use start+1's print timestamp, not its own date
    assert available_at[trading_days[2]] == available_at[trading_days[1]]
    assert available_at[trading_days[2]].date() < trading_days[2]


def test_ffill_momentum_does_not_affect_original_non_ffill_feature():
    conn = _conn()
    start = date(2026, 1, 1)
    trading_days = [start, start + timedelta(days=1), start + timedelta(days=4)]
    _seed_trading_days(conn, trading_days)
    _seed_series(conn, "DTWEXBGS", [100.0, 102.0], start)

    compute_macro_features(conn)

    old_rows = conn.execute(
        "SELECT feature_date, feature_value FROM feature_store WHERE feature_name = 'dxy_chg_1d' ORDER BY feature_date"
    ).fetchall()
    assert len(old_rows) == 1  # only the one real day-over-day print pair; no entry for the gap day
    assert old_rows[0][1] == pytest.approx(0.02)


def test_yield_curve_ffill_reads_flat_on_no_print_days_and_true_level_on_print_day():
    """Same publication-lag bug as dxy/oil, but for the curve LEVEL: DGS10/
    DGS2 have a T-1 publication delay, which used to stall build_feature_
    matrix's (and the trader league's) "today" by that same delay. Unlike the
    momentum features, this one carries forward the level itself -- no
    differencing step -- so a no-print day reads the last known spread
    unchanged, not zero."""
    conn = _conn()
    start = date(2026, 1, 1)
    trading_days = [start, start + timedelta(days=1), start + timedelta(days=4),
                     start + timedelta(days=5), start + timedelta(days=6)]
    _seed_trading_days(conn, trading_days)
    # DGS10/DGS2 only print on the first two days, then go quiet.
    _seed_series(conn, "DGS10", [4.5, 4.6], start)
    _seed_series(conn, "DGS2", [4.0, 4.2], start)

    compute_macro_features(conn)

    rows = dict(conn.execute(
        "SELECT feature_date, feature_value FROM feature_store WHERE feature_name = 'yield_curve_10y2y_ffill'"
    ).fetchall())
    assert rows[trading_days[0]] == pytest.approx(0.5)   # real print: 4.5 - 4.0
    assert rows[trading_days[1]] == pytest.approx(0.4)   # real print: 4.6 - 4.2
    assert rows[trading_days[2]] == pytest.approx(0.4)   # no print yet -- carried forward, unchanged
    assert rows[trading_days[3]] == pytest.approx(0.4)   # still no print -- unchanged
    assert rows[trading_days[4]] == pytest.approx(0.4)   # never printed again in this fixture


def test_yield_curve_ffill_available_at_pinned_to_when_value_was_actually_known():
    conn = _conn()
    start = date(2026, 1, 1)
    trading_days = [start, start + timedelta(days=1), start + timedelta(days=2)]
    _seed_trading_days(conn, trading_days)
    _seed_series(conn, "DGS10", [4.5, 4.6], start)
    _seed_series(conn, "DGS2", [4.0, 4.2], start)

    compute_macro_features(conn)

    available_at = dict(conn.execute(
        "SELECT feature_date, available_at FROM feature_store WHERE feature_name = 'yield_curve_10y2y_ffill'"
    ).fetchall())
    # the carried-forward day (start+2) must use start+1's print timestamp, not its own date
    assert available_at[trading_days[2]] == available_at[trading_days[1]]
    assert available_at[trading_days[2]].date() < trading_days[2]


def test_yield_curve_ffill_does_not_affect_original_non_ffill_feature():
    conn = _conn()
    start = date(2026, 1, 1)
    trading_days = [start, start + timedelta(days=1), start + timedelta(days=4)]
    _seed_trading_days(conn, trading_days)
    _seed_series(conn, "DGS10", [4.5, 4.6], start)
    _seed_series(conn, "DGS2", [4.0, 4.2], start)

    compute_macro_features(conn)

    old_rows = conn.execute(
        "SELECT feature_date, feature_value FROM feature_store "
        "WHERE feature_name = 'yield_curve_10y2y' ORDER BY feature_date"
    ).fetchall()
    assert len(old_rows) == 2  # only the two real print days; no entry for the gap day
    assert [r[1] for r in old_rows] == pytest.approx([0.5, 0.4])


def test_later_revision_does_not_change_historical_feature_value():
    """The core anti-leakage property for this module: a later-arriving
    revision (a second, later vintage for a past observation_date) must not
    change the already-computed feature value for that date — only the
    EARLIEST vintage is used, matching what was actually knowable then."""
    conn = _conn()
    start = date(2026, 1, 1)
    _seed_series(conn, "DGS10", [4.5, 4.6, 4.4], start, vintage_offset=0)
    _seed_series(conn, "DGS2", [4.0, 4.2, 4.1], start, vintage_offset=0)
    compute_macro_features(conn)

    before = conn.execute(
        "SELECT feature_date, feature_value FROM feature_store "
        "WHERE feature_name = 'yield_curve_10y2y' ORDER BY feature_date"
    ).fetchall()

    # A revised DGS10 value for the FIRST observation_date, arriving 30 days
    # later (a later vintage) with a materially different value.
    revised = pl.DataFrame({
        "series_id": ["DGS10"],
        "observation_date": [start],
        "value": [999.0],
        "vintage_date": [start + timedelta(days=30)],
        "source": ["fred"],
        "ingested_at": [datetime(start.year, start.month, start.day, 20, tzinfo=timezone.utc) + timedelta(days=30)],
    })
    append_rows(conn, "macro_series_daily", revised)

    conn.execute("DELETE FROM feature_store")  # recompute from scratch to compare cleanly
    compute_macro_features(conn)
    after = conn.execute(
        "SELECT feature_date, feature_value FROM feature_store "
        "WHERE feature_name = 'yield_curve_10y2y' ORDER BY feature_date"
    ).fetchall()

    assert before == after  # unaffected by the later revision
