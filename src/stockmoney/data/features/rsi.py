"""Wilder's 14-period RSI (Relative Strength Index) — a momentum/overbought-
oversold oscillator, complementary to adx_14 (which measures trend strength
but not direction-of-momentum saturation). New module-A feature candidate
(CLAUDE.md section 16: additional features are the honest next lever after
LightGBM tested worse than the logistic baseline in backtest_direction_models.py).

The Wilder computation is a pure function (`wilder_rsi`) so it can be unit
tested against known values in isolation from the database, same pattern as
`adx.wilder_adx`.
"""
from __future__ import annotations

from datetime import date, datetime

import duckdb

from stockmoney.data.features.base import FeatureValue, write_features

FEATURE_NAME = "rsi_14"
FEATURE_VERSION = "v1"
PERIOD = 14


def wilder_rsi(closes: list[float], period: int = PERIOD) -> list[tuple[int, float]]:
    """Return ``(index, rsi)`` pairs for every bar where RSI is defined.

    Standard Wilder method: `period` price changes seed the first average
    gain/loss (so the first RSI appears at index `period`), then each
    subsequent bar Wilder-smooths the average gain/loss forward.
    """
    n = period
    length = len(closes)
    if length < n + 1:
        return []

    gains = [0.0] * length
    losses = [0.0] * length
    for i in range(1, length):
        delta = closes[i] - closes[i - 1]
        gains[i] = delta if delta > 0 else 0.0
        losses[i] = -delta if delta < 0 else 0.0

    avg_gain = sum(gains[1 : n + 1]) / n
    avg_loss = sum(losses[1 : n + 1]) / n

    out: list[tuple[int, float]] = []

    def _rsi(avg_gain: float, avg_loss: float) -> float:
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    out.append((n, _rsi(avg_gain, avg_loss)))
    for i in range(n + 1, length):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n
        out.append((i, _rsi(avg_gain, avg_loss)))
    return out


def compute_rsi_14(conn: duckdb.DuckDBPyConnection, symbols: list[str] | None = None) -> int:
    """Compute rsi_14 for ``symbols`` (default: every symbol in ohlcv_daily),
    using only the latest ingested version of each (symbol, trade_date)."""
    where = ""
    params: list = []
    if symbols:
        placeholders = ", ".join(["?"] * len(symbols))
        where = f"AND symbol IN ({placeholders})"
        params = list(symbols)

    rows = conn.execute(
        f"""
        WITH latest AS (
            SELECT symbol, trade_date, close, ingested_at,
                   row_number() OVER (
                       PARTITION BY symbol, trade_date ORDER BY ingested_at DESC
                   ) AS rn
            FROM ohlcv_daily
            WHERE close IS NOT NULL {where}
        )
        SELECT symbol, trade_date, close, ingested_at
        FROM latest WHERE rn = 1
        ORDER BY symbol, trade_date
        """,
        params,
    ).fetchall()

    by_symbol: dict[str, list[tuple[date, float, datetime]]] = {}
    for symbol, trade_date, close, ingested_at in rows:
        by_symbol.setdefault(symbol, []).append((trade_date, close, ingested_at))

    values: list[FeatureValue] = []
    for symbol, series in by_symbol.items():
        closes = [r[1] for r in series]
        for idx, rsi in wilder_rsi(closes):
            trade_date, _, ingested_at = series[idx]
            values.append(
                FeatureValue(
                    feature_date=trade_date,
                    symbol=symbol,
                    value=rsi,
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
