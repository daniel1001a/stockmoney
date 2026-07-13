from datetime import date, timedelta

import duckdb
import pandas as pd
import pytest

from stockmoney.data.db import run_migrations
from stockmoney.data.ingestion import options_chain

TRADE_DATE = date(2026, 7, 9)


def _chain_df(rows):
    return pd.DataFrame(
        rows, columns=["strike", "impliedVolatility", "openInterest", "volume"]
    )


class _FakeChain:
    def __init__(self, calls, puts):
        self.calls = calls
        self.puts = puts


class _FakeTicker:
    """Same synthetic chain for any symbol: one in-window expiry (~30 DTE),
    one too-short and one too-long expiry that must be filtered out."""

    def __init__(self, symbol):
        self.symbol = symbol
        in_window = (TRADE_DATE + timedelta(days=30)).isoformat()
        too_short = (TRADE_DATE + timedelta(days=2)).isoformat()
        too_long = (TRADE_DATE + timedelta(days=400)).isoformat()
        self.options = (too_short, in_window, too_long)
        self.fast_info = {"lastPrice": 100.0}

    def option_chain(self, expiry):
        calls = _chain_df([
            (80.0, 0.60, 100, 10.0),
            (100.0, 0.50, 200, 20.0),
            (130.0, 0.55, 50, float("nan")),  # NaN volume must be treated as 0
        ])
        puts = _chain_df([
            (70.0, 0.65, 40, 5.0),
            (100.0, 0.50, 150, 15.0),
            (120.0, 0.70, 30, 3.0),
        ])
        return _FakeChain(calls, puts)


def test_fetch_options_snapshot_shapes_and_buckets(monkeypatch):
    monkeypatch.setattr("yfinance.Ticker", _FakeTicker)

    frames = options_chain.fetch_options_snapshot(["NVDA"], trade_date=TRADE_DATE)

    iv = frames["iv_surface_daily"]
    pcr = frames["put_call_ratio_daily"]
    derived = frames["options_derived_daily"]

    # Only the single in-window expiry survives DTE filtering.
    assert set(iv["expiry_date"].unique().to_list()) == {TRADE_DATE + timedelta(days=30)}
    assert set(iv["delta_bucket"].to_list()) == {"50", "25c", "25p"}

    # 50-delta bucket is clearly the ATM strike (100) → IV 0.50.
    atm = iv.filter((iv["delta_bucket"] == "50")).row(0, named=True)
    assert atm["implied_vol"] == pytest.approx(0.50)

    # put/call aggregates: NaN call volume counted as 0 → call_volume = 10+20+0.
    pcr_row = pcr.row(0, named=True)
    assert pcr_row["call_volume"] == 30
    assert pcr_row["put_volume"] == 23
    assert pcr_row["call_oi"] == 350
    assert pcr_row["put_oi"] == 220

    metrics = set(derived["metric_name"].to_list())
    assert {"gex_estimate", "skew_25delta"} <= metrics

    # skew_25delta must equal iv(25p) - iv(25c) from the same surface.
    iv_25c = iv.filter(iv["delta_bucket"] == "25c").row(0, named=True)["implied_vol"]
    iv_25p = iv.filter(iv["delta_bucket"] == "25p").row(0, named=True)["implied_vol"]
    skew = derived.filter(derived["metric_name"] == "skew_25delta").row(0, named=True)
    assert skew["metric_value"] == pytest.approx(iv_25p - iv_25c)
    assert skew["method_version"] == options_chain.METHOD_VERSION


def test_symbol_with_no_options_is_skipped(monkeypatch):
    class _NoOptions(_FakeTicker):
        def __init__(self, symbol):
            super().__init__(symbol)
            self.options = ()

    monkeypatch.setattr("yfinance.Ticker", _NoOptions)
    frames = options_chain.fetch_options_snapshot(["NVDA"], trade_date=TRADE_DATE)
    assert frames["iv_surface_daily"].height == 0
    assert frames["put_call_ratio_daily"].height == 0


def test_ingest_watchlist_options_end_to_end(monkeypatch):
    monkeypatch.setattr("yfinance.Ticker", _FakeTicker)

    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    n_symbols = conn.execute(
        "SELECT count(DISTINCT symbol) FROM watchlist_members WHERE removed_date IS NULL"
    ).fetchone()[0]

    written = options_chain.ingest_watchlist_options(conn, trade_date=TRADE_DATE)

    assert written["iv_surface_daily"] == n_symbols * 3   # 3 buckets each
    assert written["put_call_ratio_daily"] == n_symbols
    assert written["options_derived_daily"] == n_symbols * 2  # gex + skew

    # Each of the three target tables gets its own audit row from one pull.
    audit = conn.execute(
        "SELECT target_table, status FROM ingestion_runs "
        "WHERE source = 'yfinance_options' ORDER BY target_table"
    ).fetchall()
    assert audit == [
        ("iv_surface_daily", "success"),
        ("options_derived_daily", "success"),
        ("put_call_ratio_daily", "success"),
    ]


def test_ingest_watchlist_options_defaults_to_latest_ohlcv_trading_day(monkeypatch):
    """Without an explicit trade_date, the snapshot must be stamped with the
    latest REAL trading day already in ohlcv_daily -- never wall-clock
    date.today() -- so a run on a weekend/holiday can't orphan this
    non-backfillable data under a date no other table ever has a row for."""
    monkeypatch.setattr("yfinance.Ticker", _FakeTicker)

    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    real_trading_day = date(2026, 7, 10)  # e.g. the Friday before a "today" that's a Sunday
    conn.execute(
        "INSERT INTO ohlcv_daily (symbol, trade_date, close, source, ingested_at) "
        "VALUES ('NVDA', ?, 100.0, 'test', now())",
        [real_trading_day],
    )

    options_chain.ingest_watchlist_options(conn)  # no trade_date passed

    stamped = conn.execute("SELECT DISTINCT trade_date FROM iv_surface_daily").fetchall()
    assert stamped == [(real_trading_day,)]
