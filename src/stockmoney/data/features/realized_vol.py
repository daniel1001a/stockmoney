from __future__ import annotations

import math
from datetime import date, datetime

import duckdb

from stockmoney.data.features.base import FeatureValue, write_features

FEATURE_NAME = "realized_vol_20d"
FEATURE_VERSION = "v1"
WINDOW = 20


def compute_realized_vol_20d(conn: duckdb.DuckDBPyConnection) -> int:
    """20-trading-day annualized realized volatility of close-to-close log returns.

    ``ohlcv_daily`` is append-only, so the same (symbol, trade_date) can have
    multiple ingested versions (e.g. an intraday partial close later replaced
    by the final print); this uses only the latest ingested row per day.
    ``available_at`` is stamped with that row's ``ingested_at`` so a backtest
    can never see a vol figure before the underlying close price it depends
    on was actually ingested.
    """
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT symbol, trade_date, close, ingested_at,
                   row_number() OVER (
                       PARTITION BY symbol, trade_date ORDER BY ingested_at DESC
                   ) AS rn
            FROM ohlcv_daily
        )
        SELECT symbol, trade_date, close, ingested_at
        FROM latest
        WHERE rn = 1 AND close IS NOT NULL
        ORDER BY symbol, trade_date
        """
    ).fetchall()

    by_symbol: dict[str, list[tuple[date, float, datetime]]] = {}
    for symbol, trade_date, close, ingested_at in rows:
        by_symbol.setdefault(symbol, []).append((trade_date, close, ingested_at))

    values: list[FeatureValue] = []
    for symbol, series in by_symbol.items():
        closes = [c for _, c, _ in series]
        for i in range(WINDOW, len(series)):
            window = closes[i - WINDOW : i + 1]
            log_returns = [
                math.log(window[j] / window[j - 1])
                for j in range(1, len(window))
                if window[j - 1] > 0 and window[j] > 0
            ]
            if len(log_returns) < WINDOW - 1:
                continue
            mean = sum(log_returns) / len(log_returns)
            variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
            annualized_vol = math.sqrt(variance * 252)
            trade_date, _, ingested_at = series[i]
            values.append(
                FeatureValue(
                    feature_date=trade_date,
                    symbol=symbol,
                    value=annualized_vol,
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
