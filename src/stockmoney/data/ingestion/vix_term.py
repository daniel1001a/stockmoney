"""VIX term-structure ingestion from free CBOE volatility indices via yfinance.

Per-underlying option chains from yfinance are snapshot-only (no history), so
IV-rank / 25-delta-skew / GEX / put-call features can't be backtested for free
(CLAUDE.md section 2's paid ORATS/CBOE upgrade path). The VIX *term structure*,
however, is published as a set of constant-maturity indices with full daily
history that yfinance serves for free:

    ^VIX9D  -> 9-day implied vol
    ^VIX    -> 30-day implied vol
    ^VIX3M  -> 3-month (90-day) implied vol
    ^VIX6M  -> 6-month (180-day) implied vol

The SHAPE of this curve (contango vs backwardation) is a genuine options-
microstructure signal that is orthogonal to price and to realized vol: when the
front end trades above the back end (backwardation), the market is pricing
near-term stress. This ingester lands those four indices in
`vix_term_structure_daily` so the feature layer can derive term-structure
slopes. Free, historical, no key.
"""
from __future__ import annotations

import warnings
from datetime import date

import duckdb
import polars as pl

from stockmoney.data.ingestion.base import capture_yfinance_errors, run_ingestion

# yfinance index symbol -> tenor in days (matches vix_term_structure_daily.tenor_days)
VIX_TENORS: dict[str, int] = {"^VIX9D": 9, "^VIX": 30, "^VIX3M": 90, "^VIX6M": 180}

_SCHEMA = {
    "trade_date": pl.Date,
    "tenor_days": pl.Int32,
    "vix_value": pl.Float64,
    "source": pl.Utf8,
}


def fetch_vix_term(start: date, end: date) -> pl.DataFrame:
    """Daily close of each constant-maturity VIX index over [start, end],
    reshaped long into vix_term_structure_daily's schema. One index failing to
    download doesn't discard the others (unattended-run resilience)."""
    warnings.filterwarnings("ignore")
    import yfinance as yf

    rows = []
    with capture_yfinance_errors() as errors:
        for ysymbol, tenor in VIX_TENORS.items():
            try:
                df = yf.download(
                    ysymbol, start=start.isoformat(), end=(end + _one_day()).isoformat(),
                    interval="1d", auto_adjust=False, progress=False,
                )
            except Exception as exc:
                errors.append(str(exc))
                continue
            if df.empty or "Close" not in df:
                continue
            close = df["Close"]
            if hasattr(close, "columns"):  # single-ticker download -> 1-col DataFrame
                close = close.iloc[:, 0]
            close = close.dropna()
            for ts, val in close.items():
                d = ts.date()
                if d < start or d > end:
                    continue
                rows.append({"trade_date": d, "tenor_days": tenor, "vix_value": round(float(val), 4), "source": "yfinance"})
    if not rows:
        if errors:
            raise RuntimeError(
                f"yfinance fetched 0 usable rows for all {len(VIX_TENORS)} VIX tenors "
                f"({start}..{end}) and logged {len(errors)} failure(s) -- treating as a "
                f"fetch failure, not a genuine gap (e.g. {errors[0]!r})"
            )
        return pl.DataFrame(schema=_SCHEMA)
    return pl.DataFrame(rows, schema=_SCHEMA)


def _one_day():
    from datetime import timedelta

    return timedelta(days=1)


def ingest_vix_term(conn: duckdb.DuckDBPyConnection, start: date, end: date) -> int:
    result = run_ingestion(
        conn,
        source="yfinance",
        target_table="vix_term_structure_daily",
        fetch_fn=lambda: fetch_vix_term(start, end),
        window_start=start,
        window_end=end,
    )
    return result.rows_written
