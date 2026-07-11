"""20-day rolling z-score of daily trading volume — a participation/liquidity
signal orthogonal to the price-only features already in FEATURE_COLUMNS
(realized_vol_20d, adx_14, xsec_dispersion all derive from close/high/low
only). A volume surge relative to its own trailing history often precedes or
accompanies a genuine regime shift (news-driven interest), independent of
what price itself is doing that day.

New module-A feature candidate (CLAUDE.md section 16: additional features are
the honest next lever after LightGBM tested worse than the logistic baseline
in backtest_direction_models.py).
"""
from __future__ import annotations

import statistics
from datetime import date, datetime

import duckdb

from stockmoney.data.features.base import FeatureValue, write_features

FEATURE_NAME = "volume_zscore_20d"
FEATURE_VERSION = "v1"
WINDOW = 20


def rolling_volume_zscore(volumes: list[float], window: int = WINDOW) -> list[tuple[int, float]]:
    """Return ``(index, zscore)`` pairs for every bar with a full trailing
    window (inclusive of that bar itself). Zero variance in the window
    (e.g. a run of identical/zero volumes) yields zscore 0.0 rather than a
    division error."""
    n = len(volumes)
    if n < window:
        return []

    out: list[tuple[int, float]] = []
    for i in range(window - 1, n):
        w = volumes[i - window + 1 : i + 1]
        mean = statistics.fmean(w)
        stdev = statistics.pstdev(w)
        z = 0.0 if stdev == 0 else (volumes[i] - mean) / stdev
        out.append((i, z))
    return out


def compute_volume_zscore_20d(conn: duckdb.DuckDBPyConnection, symbols: list[str] | None = None) -> int:
    """Compute volume_zscore_20d for ``symbols`` (default: every symbol in
    ohlcv_daily), using only the latest ingested version of each
    (symbol, trade_date)."""
    where = ""
    params: list = []
    if symbols:
        placeholders = ", ".join(["?"] * len(symbols))
        where = f"AND symbol IN ({placeholders})"
        params = list(symbols)

    rows = conn.execute(
        f"""
        WITH latest AS (
            SELECT symbol, trade_date, volume, ingested_at,
                   row_number() OVER (
                       PARTITION BY symbol, trade_date ORDER BY ingested_at DESC
                   ) AS rn
            FROM ohlcv_daily
            WHERE volume IS NOT NULL {where}
        )
        SELECT symbol, trade_date, volume, ingested_at
        FROM latest WHERE rn = 1
        ORDER BY symbol, trade_date
        """,
        params,
    ).fetchall()

    by_symbol: dict[str, list[tuple[date, float, datetime]]] = {}
    for symbol, trade_date, volume, ingested_at in rows:
        by_symbol.setdefault(symbol, []).append((trade_date, float(volume), ingested_at))

    values: list[FeatureValue] = []
    for symbol, series in by_symbol.items():
        volumes = [r[1] for r in series]
        for idx, z in rolling_volume_zscore(volumes):
            trade_date, _, ingested_at = series[idx]
            values.append(
                FeatureValue(
                    feature_date=trade_date,
                    symbol=symbol,
                    value=z,
                    available_at=ingested_at,
                )
            )

    return write_features(
        conn,
        feature_name=FEATURE_NAME,
        feature_version=FEATURE_VERSION,
        source_table="ohlcv_daily",
        values=values,
    )
