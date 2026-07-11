"""End-to-end smoke test for scripts/nightly_refresh.py -- the full nightly
chain (ingest -> feature recompute -> dashboard snapshot -> attribution)
wired together against one real file-backed DuckDB.

Network-calling ingestion connectors (yfinance/FRED/options) are stubbed --
this isn't re-testing those connectors (they have their own tests), it's
catching a step silently failing to hand off to the next one. That's exactly
how HANDOFF's "FRED macro 資料落後修復" incident happened: feature
recomputation was never wired into any scheduled job at all, and nothing
exercised the full chain end-to-end to notice.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from stockmoney.data import attribution as A
from stockmoney.data.attribution import RIGHT_REASON_RIGHT
from stockmoney.data.daily_predictions import grade_prediction, record_prediction
from stockmoney.data.db import MARKET_SYMBOL, append_rows, get_connection, run_migrations, sector_symbol
from stockmoney.data.features.base import FeatureValue, write_features

import nightly_refresh

TARGET = "SOXL"
SECTOR = "semiconductor"


def _seed_snapshot_inputs(conn, n=90, seed=0):
    """Enough SOXL feature history for build_dashboard_snapshot's production
    fit + walk-forward split to succeed -- mirrors
    tests/scripts/test_build_dashboard_snapshot.py's seeding."""
    rng = np.random.default_rng(seed)
    start = date(2024, 1, 1)
    dates = [start + timedelta(days=i) for i in range(n)]
    av = [datetime(d.year, d.month, d.day, 21, tzinfo=timezone.utc) for d in dates]

    closes = [100.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + rng.normal(scale=0.01)))

    append_rows(conn, "ohlcv_daily", pl.DataFrame({
        "symbol": [TARGET] * n, "trade_date": dates, "close": closes,
        "source": ["test"] * n, "ingested_at": av,
    }))

    def _fv(sym, vals):
        return [FeatureValue(d, sym, value=float(v), available_at=a) for d, v, a in zip(dates, vals, av)]

    rv, adx, disp = rng.uniform(0.2, 0.6, n), rng.uniform(10, 40, n), rng.uniform(0.005, 0.02, n)
    curve, dxy, oil = rng.normal(size=n), rng.normal(scale=0.01, size=n), rng.normal(scale=0.02, size=n)

    write_features(conn, feature_name="realized_vol_20d", feature_version="v1",
                   source_table="ohlcv_daily", values=_fv(TARGET, rv))
    write_features(conn, feature_name="adx_14", feature_version="v1",
                   source_table="ohlcv_daily", values=_fv(TARGET, adx))
    write_features(conn, feature_name="xsec_dispersion", feature_version="v1",
                   source_table="ohlcv_daily", values=_fv(sector_symbol(SECTOR), disp))
    write_features(conn, feature_name="yield_curve_10y2y_ffill", feature_version="v1",
                   source_table="macro_series_daily", values=_fv(MARKET_SYMBOL, curve))
    write_features(conn, feature_name="dxy_chg_1d_ffill", feature_version="v1",
                   source_table="macro_series_daily", values=_fv(MARKET_SYMBOL, dxy))
    write_features(conn, feature_name="oil_chg_1d_ffill", feature_version="v1",
                   source_table="macro_series_daily", values=_fv(MARKET_SYMBOL, oil))
    return dates


def _put_price(conn, symbol, trade_date, close):
    conn.execute(
        "INSERT INTO ohlcv_daily (symbol, trade_date, close, source, ingested_at) VALUES (?, ?, ?, 'test', ?)",
        [symbol, trade_date, close, datetime.now(timezone.utc)],
    )


def _seed_graded_prediction(conn):
    """A pre-existing, already-graded prediction (as if from a prior day) so
    the attribution step has a real resolved call to attribute, proving
    build_dashboard_snapshot and attribution correctly share one
    daily_predictions table end to end. Needs member OHLCV so
    decompose_return's cross-sections resolve -- decompose_return never
    reads the target's own price (actual_return is already stored)."""
    trade_date = date(2026, 1, 5)
    label_end_date = trade_date + timedelta(days=7)
    for member in A.MARKET_MEMBERS:
        _put_price(conn, member, trade_date, 100.0)
        _put_price(conn, member, label_end_date, 101.0)  # flat +1% cross-section

    pid = record_prediction(
        conn, trade_date=trade_date, symbol=TARGET, sector=SECTOR, horizon=5,
        label_end_date=label_end_date, regime=0, proba=(0.1, 0.8, 0.1),
        entry_price=100.0, feature_values={"realized_vol_20d": 0.3}, model_version="smoke-v1",
    )
    grade_prediction(conn, pid, actual_price=101.0)  # actual_return == 1% -> range -> win


def test_nightly_refresh_full_chain_wires_together(tmp_path, monkeypatch):
    db_path = str(tmp_path / "smoke.duckdb")
    conn = get_connection(db_path)
    run_migrations(conn)
    _seed_snapshot_inputs(conn)
    _seed_graded_prediction(conn)
    conn.close()

    # Stub network-calling ingestion -- this test verifies the chain wires
    # together, not that real ingestion succeeds (those connectors have
    # their own tests).
    monkeypatch.setattr(nightly_refresh, "ingest_watchlist_ohlcv", lambda *a, **k: 0)
    monkeypatch.setattr(nightly_refresh, "ingest_macro_series", lambda *a, **k: 0)
    monkeypatch.setattr(nightly_refresh, "ingest_watchlist_options", lambda *a, **k: 0)

    nightly_refresh.main(db_path=db_path)

    result = get_connection(db_path)
    try:
        # Dashboard snapshot step ran: today's live prediction + cached
        # backtest for the seeded symbol.
        assert result.execute(
            "SELECT count(*) FROM symbol_backtest_snapshot WHERE symbol = ?", [TARGET]
        ).fetchone()[0] == 1
        assert result.execute(
            "SELECT count(*) FROM daily_predictions WHERE symbol = ? AND status = 'pending'", [TARGET]
        ).fetchone()[0] == 1

        # Attribution step ran and picked up the pre-seeded graded prediction.
        verdict = result.execute(
            "SELECT verdict_class FROM attribution_log WHERE symbol = ?", [TARGET]
        ).fetchone()
        assert verdict == (RIGHT_REASON_RIGHT,)
    finally:
        result.close()


def test_nightly_refresh_tolerates_a_failing_ingestion_step(tmp_path, monkeypatch):
    """Each ingestion source is independently wrapped in try/except in
    nightly_refresh.main() -- one source raising must not stop the rest of
    the chain (feature recompute / snapshot / attribution) from running."""
    db_path = str(tmp_path / "smoke.duckdb")
    conn = get_connection(db_path)
    run_migrations(conn)
    _seed_snapshot_inputs(conn)
    conn.close()

    def _boom(*a, **k):
        raise RuntimeError("simulated network failure")

    monkeypatch.setattr(nightly_refresh, "ingest_watchlist_ohlcv", _boom)
    monkeypatch.setattr(nightly_refresh, "ingest_macro_series", lambda *a, **k: 0)
    monkeypatch.setattr(nightly_refresh, "ingest_watchlist_options", lambda *a, **k: 0)

    nightly_refresh.main(db_path=db_path)  # must not raise

    result = get_connection(db_path)
    try:
        assert result.execute(
            "SELECT count(*) FROM symbol_backtest_snapshot WHERE symbol = ?", [TARGET]
        ).fetchone()[0] == 1
    finally:
        result.close()
