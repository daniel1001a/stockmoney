"""Nightly incremental data refresh: OHLCV + FRED (short trailing window) +
today's options-chain snapshot + feature recomputation. Designed to run
unattended (OpenClaw cron): pure deterministic Python, no LLM calls, no
untrusted content processed here.

Each source is wrapped independently so one failing source (e.g. a holiday
with no options chain, a transient network error) doesn't block the others —
every attempt is still recorded in `ingestion_runs` by the underlying
connectors regardless of success/failure.

Uses short trailing windows (not the full history) since the one-time 8-year
backfill already ran; `ohlcv_daily`/`macro_series_daily` are append-only
ledgers, so a few days of window overlap on each run is expected and cheap,
not wasteful re-fetching of history.

Feature recomputation (scripts/compute_features.py) runs after fresh
prices/macro are in -- otherwise realized_vol_20d/adx_14/xsec_dispersion (and
therefore stockmoney.models.production's daily predictions) would keep
lagging ohlcv_daily indefinitely, which is exactly what happened before this
step was wired in here.

Dashboard snapshot build (scripts/build_dashboard_snapshot.py) runs after
features are current -- it records today's live prediction for every
watchlist symbol and caches their walk-forward backtest summaries, which is
what lets the FastAPI backend (src/stockmoney/api) answer requests with a
DuckDB read instead of fitting/backtesting live on every call.

Attribution (stockmoney.data.attribution) runs last, the read-only review
side-branch (CLAUDE.md section 11): it attributes every *graded* prediction
into attribution_log and proposes feature candidates. It's idempotent and
depends only on already-resolved predictions, so it safely no-ops when
nothing new has been graded (grading itself stays a deliberate manual step,
see scripts/daily_prediction_cli.py).

Usage:
    uv run python scripts/nightly_refresh.py
"""
from __future__ import annotations

from datetime import date, timedelta

from build_dashboard_snapshot import run_snapshot_build
from compute_features import run_feature_recompute

from stockmoney.data.attribution import run_attribution
from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations
from stockmoney.data.ingestion.fred_macro import ingest_macro_series
from stockmoney.data.ingestion.options_chain import ingest_watchlist_options
from stockmoney.data.ingestion.yfinance_ohlcv import ingest_watchlist_ohlcv

OHLCV_LOOKBACK_DAYS = 10
MACRO_LOOKBACK_DAYS = 30


def main(db_path: str = DEFAULT_DB_PATH) -> None:
    conn = get_connection(db_path)
    run_migrations(conn)

    today = date.today()
    print(f"[{today}] nightly refresh starting")

    try:
        n = ingest_watchlist_ohlcv(conn, today - timedelta(days=OHLCV_LOOKBACK_DAYS), today)
        print(f"  ohlcv_daily: +{n} rows")
    except Exception as exc:
        print(f"  ohlcv_daily FAILED: {exc}")

    try:
        n = ingest_macro_series(conn, today - timedelta(days=MACRO_LOOKBACK_DAYS), today)
        print(f"  macro_series_daily: +{n} rows")
    except Exception as exc:
        print(f"  macro_series_daily FAILED: {exc}")

    try:
        written = ingest_watchlist_options(conn, trade_date=today)
        print(f"  options snapshot: {written}")
    except Exception as exc:
        print(f"  options snapshot FAILED: {exc}")

    run_feature_recompute(conn)

    try:
        summary = run_snapshot_build(conn)
        print(f"  dashboard snapshot: {summary}")
    except Exception as exc:
        print(f"  dashboard snapshot FAILED: {exc}")

    try:
        summary = run_attribution(conn)
        print(f"  attribution: {summary}")
    except Exception as exc:
        print(f"  attribution FAILED: {exc}")

    conn.close()
    print(f"[{today}] nightly refresh done")


if __name__ == "__main__":
    main()
