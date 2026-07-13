"""Build a fully-LIVE database: real prices + real macro + real model calls.

Unlike scripts/seed_demo_data.py (synthetic fixture), this runs the genuine
production pipeline on real data so the frontend shows real model output:

  yfinance OHLCV (≈2y)  ->  FRED macro  ->  VIX term structure (≈2.5y)  ->
  today's options-chain snapshot  ->  feature_store  ->  per-symbol GMM regime
  + logistic direction + walk-forward backtest snapshot  ->  league trader
  calls  ->  live RSS/Google-News feed.

Runs into a SEPARATE db by default (data/stockmoney_live.duckdb) so it never
clobbers the demo. Point the API at it with STOCKMONEY_DB / by copying it over
data/stockmoney.duckdb once you're happy.

Honest caveats (see also IMPROVEMENT_PLAN.md):
  * Signal is currently WEAK -- OOS directional accuracy ≈0.35-0.45, barely
    above the ~1/3 random baseline. VIX-term-structure shape was tested
    (models/backtest_vix_term_ablation.py) and did NOT pass the significance
    gate (95% CI straddles 0) -- not promoted. The per-underlying options
    candidates (gex_estimate/skew_25delta/put_call_ratio) can't be tested at
    all yet: yfinance option chains are snapshot-only, no free history, so
    every run of this script only adds ONE more day to that series. Either
    run this daily for months, or subscribe to ORATS/CBOE DataShop to unlock
    those candidates immediately (CLAUDE.md section 16's paid-upgrade path).
  * Regime cluster ids from a live GMM fit are arbitrary; the plain-language
    regime_label mapping in api/queries.py is a demo convention and should be
    replaced with per-cluster characterisation before real regimes are trusted.
  * The Arena's virtual-options portfolios (trader_portfolios/trader_trades)
    have no live trading engine yet (IMPROVEMENT_PLAN S3), so this script does
    NOT populate them -- the leaderboard needs that engine to go live.

Usage:
    .venv/bin/python scripts/build_live.py
    .venv/bin/python scripts/build_live.py --db data/stockmoney.duckdb --years 3
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # compute_features / build_dashboard_snapshot

from build_dashboard_snapshot import run_snapshot_build  # noqa: E402
from compute_features import run_feature_recompute  # noqa: E402

from stockmoney.data.db import get_connection, run_migrations  # noqa: E402
from stockmoney.data.ingestion.fred_macro import ingest_macro_series  # noqa: E402
from stockmoney.data.ingestion.options_chain import ingest_watchlist_options  # noqa: E402
from stockmoney.data.ingestion.vix_term import ingest_vix_term  # noqa: E402
from stockmoney.data.ingestion.yfinance_ohlcv import ingest_watchlist_ohlcv  # noqa: E402
from stockmoney.data.news_synthesis import refresh_news_items  # noqa: E402
from stockmoney.league.orchestration import run_predictions  # noqa: E402


def _step(name: str, fn):
    t = time.time()
    try:
        result = fn()
        print(f"  [OK]   {name}: {result}  ({time.time() - t:.1f}s)", flush=True)
        return result
    except Exception as exc:  # one failing source never aborts the whole build
        print(f"  [FAIL] {name}: {exc!r}", flush=True)
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/stockmoney_live.duckdb")
    parser.add_argument("--years", type=float, default=2.2)
    parser.add_argument("--end", default="2026-07-10", help="last trading day to fetch through")
    args = parser.parse_args()

    end = date.fromisoformat(args.end)
    start = end - timedelta(days=int(args.years * 365))
    print(f"building LIVE db {args.db} over {start}..{end}")

    conn = get_connection(args.db)
    run_migrations(conn)

    _step("ohlcv (yfinance)", lambda: ingest_watchlist_ohlcv(conn, start, end))
    _step("macro (FRED)", lambda: ingest_macro_series(conn, start, end))
    _step("vix term structure", lambda: ingest_vix_term(conn, start, end))
    # Options chains are snapshot-only (no free history) -- this banks TODAY's
    # snapshot as the first day of a series that can never be backfilled, so it
    # runs on every build_live invocation even though the other steps above
    # cover the full historical window. No explicit trade_date: it resolves
    # to the latest real trading day just ingested above, not wall-clock date.
    _step("options snapshot (starts today's series)", lambda: ingest_watchlist_options(conn))
    _step("feature recompute", lambda: run_feature_recompute(conn) or "done")
    _step("model snapshot", lambda: run_snapshot_build(conn))
    _step("league predict", lambda: {k: v for k, v in (run_predictions(conn) or {}).items() if k != "skips"})
    _step("live news", lambda: refresh_news_items(conn))

    conn.close()
    print("LIVE build done. Point the API at it, or copy over data/stockmoney.duckdb.")


if __name__ == "__main__":
    main()
