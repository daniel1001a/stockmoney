"""Cross-sectional dispersion of idiosyncratic returns within a sector group
(CLAUDE.md section 8). When same-sector single names scatter — high dispersion
— the market is selecting winners/losers on news rather than moving as a bloc;
this feeds regime detection as one of its observation features.

Stored under a sector-group sentinel symbol (e.g. ``__SECTOR:semiconductor``)
so it lives in feature_store alongside per-symbol features without colliding.
"""
from __future__ import annotations

import statistics
from datetime import date, datetime

import duckdb

from stockmoney.data.db import sector_symbol
from stockmoney.data.features.base import FeatureValue, write_features

FEATURE_NAME = "xsec_dispersion"
FEATURE_VERSION = "v1"

# Single names only per sector (excludes leveraged/sector ETFs like SOXL/SOXS/
# SOXX/QQQ, whose basket mechanics would distort a cross-sectional idiosyncratic
# spread). Each group needs >=2 members with OHLCV for the dispersion feature to
# be computable for every symbol mapped to that sector.
SECTOR_MEMBERS: dict[str, list[str]] = {
    "semiconductor": ["NVDA", "AVGO", "AMD", "TSM", "MU", "QCOM", "MRVL", "INTC"],
    "big_tech": ["AAPL", "MSFT", "GOOGL", "META", "AMZN", "TSLA", "NFLX", "ORCL", "CRM", "PLTR"],
    "financials": ["JPM", "BAC", "GS", "MS", "WFC"],
    "energy": ["XOM", "CVX", "COP", "SLB"],
}


def compute_xsec_dispersion(
    conn: duckdb.DuckDBPyConnection, sector: str = "semiconductor"
) -> int:
    members = SECTOR_MEMBERS[sector]
    placeholders = ", ".join(["?"] * len(members))
    rows = conn.execute(
        f"""
        WITH latest AS (
            SELECT symbol, trade_date, close, ingested_at,
                   row_number() OVER (
                       PARTITION BY symbol, trade_date ORDER BY ingested_at DESC
                   ) AS rn
            FROM ohlcv_daily
            WHERE close IS NOT NULL AND symbol IN ({placeholders})
        )
        SELECT symbol, trade_date, close, ingested_at
        FROM latest WHERE rn = 1
        ORDER BY symbol, trade_date
        """,
        members,
    ).fetchall()

    # Per-symbol daily simple returns, keyed by date.
    per_symbol: dict[str, list[tuple[date, float, datetime]]] = {}
    for symbol, trade_date, close, ingested_at in rows:
        per_symbol.setdefault(symbol, []).append((trade_date, close, ingested_at))

    # date -> {symbol: (return, available_at)}
    returns_by_date: dict[date, dict[str, tuple[float, datetime]]] = {}
    for symbol, series in per_symbol.items():
        for i in range(1, len(series)):
            d, close, ingested_at = series[i]
            prev_close = series[i - 1][1]
            if prev_close and prev_close > 0:
                ret = close / prev_close - 1.0
                returns_by_date.setdefault(d, {})[symbol] = (ret, ingested_at)

    values: list[FeatureValue] = []
    group_symbol = sector_symbol(sector)
    for d, sym_returns in returns_by_date.items():
        if len(sym_returns) < 2:  # need a cross-section to disperse
            continue
        rets = [r for r, _ in sym_returns.values()]
        mean = statistics.fmean(rets)
        idiosyncratic = [r - mean for r in rets]
        dispersion = statistics.stdev(idiosyncratic)  # sample std, ddof=1
        # Available only once every contributing close for the day is ingested.
        available_at = max(a for _, a in sym_returns.values())
        values.append(
            FeatureValue(
                feature_date=d,
                symbol=group_symbol,
                value=dispersion,
                available_at=available_at,
            )
        )

    return write_features(
        conn,
        feature_name=FEATURE_NAME,
        feature_version=FEATURE_VERSION,
        source_table="ohlcv_daily",
        values=values,
    )
