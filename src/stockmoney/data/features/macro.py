"""Market-wide macro features derived from FRED series (CLAUDE.md section 2
"總體經濟" row): yield curve slope, and 1-day momentum in the dollar index and
oil. Written under the MARKET_SYMBOL sentinel since these aren't per-symbol.

Point-in-time note: macro_series_daily can hold multiple vintages of the same
(series_id, observation_date) if a value is later revised. Using the latest
vintage to featurize a historical date would leak the revision into a sample
that, at the time, only had the first-released number — so every computation
here uses the EARLIEST vintage per observation_date, matching what would
actually have been knowable back then. For the four near-real-time series used
today (Treasury yields, DXY, WTI) revisions are rare in practice, but the
correct behavior is enforced regardless.

Publication-lag note (found 2026-07-10): DTWEXBGS in particular sometimes
goes 5+ trading days between prints (a real characteristic of the Fed's own
H.10 release schedule, not an ingestion bug -- ingest_macro_series's 30-day
lookback window was already wide enough). Since feature_matrix.build_feature_
matrix requires every FEATURE_COLUMNS entry non-null for a row to count, that
single slow series was silently stalling "today" for every watchlist symbol.
The *_ffill momentum features below fix this: the raw level is carried
forward onto every trading day (source_table stays macro_series_daily,
available_at stays pinned to when the carried value was first actually
known, so the look-ahead guarantee is unaffected), so the day-over-day change
reads 0 on no-print days and the true cumulative move on the day a fresh
print lands. The original dxy_chg_1d/oil_chg_1d feature names are left
untouched in feature_store (immutable, per write_features's contract) but are
no longer read by feature_matrix.FEATURE_COLUMNS -- see that module.
"""
from __future__ import annotations

from datetime import date, datetime

import duckdb

from stockmoney.data.db import MARKET_SYMBOL
from stockmoney.data.features.base import FeatureValue, write_features

FEATURE_VERSION = "v1"
YIELD_CURVE_FEATURE = "yield_curve_10y2y"
MOMENTUM_FEATURES = {"DTWEXBGS": "dxy_chg_1d", "DCOILWTICO": "oil_chg_1d"}
FFILL_MOMENTUM_FEATURES = {"DTWEXBGS": "dxy_chg_1d_ffill", "DCOILWTICO": "oil_chg_1d_ffill"}
FFILL_FEATURE_VERSION = "v1"
SERIES_IDS = ["DGS10", "DGS2", "DTWEXBGS", "DCOILWTICO"]


def _earliest_vintage_by_series(conn: duckdb.DuckDBPyConnection) -> dict[str, list[tuple]]:
    rows = conn.execute(
        """
        WITH earliest AS (
            SELECT series_id, observation_date, value, ingested_at,
                   row_number() OVER (
                       PARTITION BY series_id, observation_date ORDER BY vintage_date ASC
                   ) AS rn
            FROM macro_series_daily
            WHERE series_id = ANY(?) AND value IS NOT NULL
        )
        SELECT series_id, observation_date, value, ingested_at
        FROM earliest WHERE rn = 1
        ORDER BY series_id, observation_date
        """,
        [SERIES_IDS],
    ).fetchall()

    by_series: dict[str, list[tuple]] = {}
    for series_id, obs_date, value, ingested_at in rows:
        by_series.setdefault(series_id, []).append((obs_date, value, ingested_at))
    return by_series


def _trading_days(conn: duckdb.DuckDBPyConnection) -> list[date]:
    return [
        row[0]
        for row in conn.execute("SELECT DISTINCT trade_date FROM ohlcv_daily ORDER BY trade_date").fetchall()
    ]


def _forward_fill_onto_calendar(
    trading_days: list[date], series: list[tuple[date, float, datetime]]
) -> list[tuple[date, float, datetime]]:
    """Carry a lower-frequency-than-daily FRED series' latest known value
    forward onto every trading day, so a slow-to-print series doesn't stall
    dependent features on days where nothing new is actually known.
    `available_at` stays pinned to when the carried-forward value was first
    known (never the trading day itself, unless that day is a fresh print),
    so `available_at <= feature_date` still holds -- the look-ahead
    guarantee is unaffected."""
    by_date = {d: (v, ia) for d, v, ia in series}
    out: list[tuple[date, float, datetime]] = []
    last: tuple[float, datetime] | None = None
    for d in trading_days:
        if d in by_date:
            last = by_date[d]
        if last is not None:
            out.append((d, last[0], last[1]))
    return out


def compute_macro_features(conn: duckdb.DuckDBPyConnection) -> int:
    by_series = _earliest_vintage_by_series(conn)
    total = 0

    dgs10 = {d: (v, ia) for d, v, ia in by_series.get("DGS10", [])}
    dgs2 = {d: (v, ia) for d, v, ia in by_series.get("DGS2", [])}
    curve_values = []
    for d in sorted(set(dgs10) & set(dgs2)):
        v10, ia10 = dgs10[d]
        v2, ia2 = dgs2[d]
        curve_values.append(
            FeatureValue(feature_date=d, symbol=MARKET_SYMBOL, value=v10 - v2, available_at=max(ia10, ia2))
        )
    total += write_features(
        conn, feature_name=YIELD_CURVE_FEATURE, feature_version=FEATURE_VERSION,
        source_table="macro_series_daily", values=curve_values,
    )

    for series_id, feature_name in MOMENTUM_FEATURES.items():
        series = by_series.get(series_id, [])
        values = []
        for i in range(1, len(series)):
            d, v, ia = series[i]
            prev_v = series[i - 1][1]
            if prev_v:
                values.append(
                    FeatureValue(feature_date=d, symbol=MARKET_SYMBOL, value=v / prev_v - 1.0, available_at=ia)
                )
        total += write_features(
            conn, feature_name=feature_name, feature_version=FEATURE_VERSION,
            source_table="macro_series_daily", values=values,
        )

    trading_days = _trading_days(conn)
    for series_id, feature_name in FFILL_MOMENTUM_FEATURES.items():
        filled = _forward_fill_onto_calendar(trading_days, by_series.get(series_id, []))
        values = []
        for i in range(1, len(filled)):
            d, v, ia = filled[i]
            prev_v = filled[i - 1][1]
            if prev_v:
                values.append(
                    FeatureValue(feature_date=d, symbol=MARKET_SYMBOL, value=v / prev_v - 1.0, available_at=ia)
                )
        total += write_features(
            conn, feature_name=feature_name, feature_version=FFILL_FEATURE_VERSION,
            source_table="macro_series_daily", values=values,
        )

    return total
