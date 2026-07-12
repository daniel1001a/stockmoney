"""VIX term-structure shape features (options-microstructure, market-wide).

Derived purely from the constant-maturity VIX indices in
`vix_term_structure_daily` (see data/ingestion/vix_term.py). Two candidate
features, both describing the SHAPE of the implied-vol curve rather than its
level, so they are orthogonal to realized_vol_20d (which is a level):

  vix_term_slope      = VIX3M / VIX9D - 1   (front-to-mid: contango vs backwardation)
  vix_term_slope_back = VIX6M / VIX3M - 1   (mid-to-back)

Positive = contango (calm, the normal state); negative = backwardation, i.e.
the market pricing near-term stress above longer-dated -- historically a
regime marker for equity drawdowns and vol mean-reversion.

Look-ahead discipline (CLAUDE.md section 2): each feature_date's value uses
ONLY that same day's index closes (no windows, no future). available_at is the
source row's ingested_at, exactly like realized_vol / macro features, so a
backtest can never read a slope before its underlying closes were ingested.
Written under MARKET_SYMBOL (market-wide, like the macro features).
"""
from __future__ import annotations

from datetime import date, datetime

import duckdb

from stockmoney.data.db import MARKET_SYMBOL
from stockmoney.data.features.base import FeatureValue, write_features

FEATURE_VERSION = "v1"
SLOPE_FEATURE = "vix_term_slope"
SLOPE_BACK_FEATURE = "vix_term_slope_back"


def _latest_by_date_tenor(conn: duckdb.DuckDBPyConnection) -> dict[date, dict[int, tuple[float, datetime]]]:
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT trade_date, tenor_days, vix_value, ingested_at,
                   row_number() OVER (
                       PARTITION BY trade_date, tenor_days ORDER BY ingested_at DESC
                   ) AS rn
            FROM vix_term_structure_daily
            WHERE vix_value IS NOT NULL
        )
        SELECT trade_date, tenor_days, vix_value, ingested_at FROM latest WHERE rn = 1
        """
    ).fetchall()
    out: dict[date, dict[int, tuple[float, datetime]]] = {}
    for trade_date, tenor, value, ingested_at in rows:
        out.setdefault(trade_date, {})[tenor] = (value, ingested_at)
    return out


def compute_vix_term_features(conn: duckdb.DuckDBPyConnection) -> int:
    by_date = _latest_by_date_tenor(conn)
    slope_values: list[FeatureValue] = []
    slope_back_values: list[FeatureValue] = []

    for d, tenors in by_date.items():
        if all(t in tenors for t in (9, 90)):
            v9, ia9 = tenors[9]
            v90, ia90 = tenors[90]
            if v9 > 0:
                slope_values.append(FeatureValue(
                    feature_date=d, symbol=MARKET_SYMBOL, value=v90 / v9 - 1.0,
                    available_at=max(ia9, ia90),
                ))
        if all(t in tenors for t in (90, 180)):
            v90, ia90 = tenors[90]
            v180, ia180 = tenors[180]
            if v90 > 0:
                slope_back_values.append(FeatureValue(
                    feature_date=d, symbol=MARKET_SYMBOL, value=v180 / v90 - 1.0,
                    available_at=max(ia90, ia180),
                ))

    total = write_features(
        conn, feature_name=SLOPE_FEATURE, feature_version=FEATURE_VERSION,
        source_table="vix_term_structure_daily", values=slope_values,
    )
    total += write_features(
        conn, feature_name=SLOPE_BACK_FEATURE, feature_version=FEATURE_VERSION,
        source_table="vix_term_structure_daily", values=slope_back_values,
    )
    return total
