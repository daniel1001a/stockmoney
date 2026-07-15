"""Backfill daily OHLCV for the "Lin universe" high-volatility / crypto-concept
momentum names (COIN, MSTR, IREN, and optionally the too-short NBIS/CRCL) that
are NOT in the core watchlist and NOT in stockmoney_live.duckdb.

Writes leak-safe parquet (one file per symbol) to data/highvol_ohlcv/, in the
same long/tidy shape as ohlcv_daily (symbol, trade_date, open, high, low,
close, adj_close, volume, source) so it can be loaded by
highvol_strategies.py without touching the live, read-only DB per this
project's rule (STOCKMONEY_DB is read-only; new data goes to parquet).

This does NOT write to stockmoney_live.duckdb. It reuses
stockmoney.data.ingestion.yfinance_ohlcv.fetch_ohlcv (the same fetch/reshape
logic the core watchlist backfill uses) rather than reimplementing the
yfinance call and column mapping.

Usage:
    .venv/bin/python -m scripts.backfill_highvol_ohlcv
    .venv/bin/python -m scripts.backfill_highvol_ohlcv --symbols COIN MSTR IREN NBIS CRCL
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from stockmoney.data.ingestion.yfinance_ohlcv import fetch_ohlcv

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "highvol_ohlcv"

# Core three: yfinance coverage confirmed usable (COIN 2021-04+, MSTR 1998+
# but options-relevant history really starts ~2020 with the BTC-treasury
# pivot, IREN 2021-11+). NBIS (2024-10+) and CRCL (2025-06+) are included by
# default but are short enough (~1.7y / ~1y) that highvol_strategies.py must
# treat them as caveat-only, not scoreboard-grade.
DEFAULT_SYMBOLS = ["COIN", "MSTR", "IREN", "NBIS", "CRCL"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument(
        "--start", type=str, default="1990-01-01",
        help="Earliest date to request; yfinance clips to actual listing history.",
    )
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.today()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Backfilling high-vol OHLCV: {args.symbols} -> {OUT_DIR}")
    df = fetch_ohlcv(args.symbols, start, end)

    if df.height == 0:
        print("!! fetch_ohlcv returned 0 rows for all symbols -- aborting, nothing written.")
        return

    for sym in args.symbols:
        sub = df.filter(df["symbol"] == sym).sort("trade_date")
        if sub.height == 0:
            print(f"  {sym}: 0 rows (yfinance returned nothing) -- SKIPPED, no file written")
            continue
        out_path = OUT_DIR / f"{sym}.parquet"
        sub.write_parquet(out_path)
        first = sub["trade_date"].min()
        last = sub["trade_date"].max()
        span_years = (last - first).days / 365.25
        print(f"  {sym}: {sub.height} rows, {first} -> {last} ({span_years:.1f}y) -> {out_path}")

    print("\nDone. Real row counts/date spans printed above (not fabricated).")


if __name__ == "__main__":
    main()
