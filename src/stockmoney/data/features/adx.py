"""Wilder's 14-period ADX (Average Directional Index) — trend strength, one of
the three regime-detection observation features (CLAUDE.md section 4).

The Wilder computation is a pure function (`wilder_adx`) so it can be unit
tested against known values in isolation from the database.
"""
from __future__ import annotations

from datetime import date, datetime

import duckdb

from stockmoney.data.features.base import FeatureValue, write_features

FEATURE_NAME = "adx_14"
FEATURE_VERSION = "v1"
PERIOD = 14


def wilder_adx(
    highs: list[float], lows: list[float], closes: list[float], period: int = PERIOD
) -> list[tuple[int, float]]:
    """Return ``(index, adx)`` pairs for every bar where ADX is defined.

    Standard Wilder method: the first ADX appears at index ``2*period - 1``
    (``period`` bars to seed the directional indicators, then ``period`` more
    to seed the ADX smoothing).
    """
    n = period
    length = len(highs)
    if length < 2 * n:
        return []

    tr = [0.0] * length
    plus_dm = [0.0] * length
    minus_dm = [0.0] * length
    for i in range(1, length):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )

    # Wilder-smoothed TR / +DM / -DM, seeded with the sum over the first n bars.
    atr = sum(tr[1 : n + 1])
    plus_s = sum(plus_dm[1 : n + 1])
    minus_s = sum(minus_dm[1 : n + 1])

    dx: list[tuple[int, float]] = []
    for i in range(n, length):
        if i > n:
            atr = atr - atr / n + tr[i]
            plus_s = plus_s - plus_s / n + plus_dm[i]
            minus_s = minus_s - minus_s / n + minus_dm[i]
        if atr == 0:
            dx.append((i, 0.0))
            continue
        plus_di = 100.0 * plus_s / atr
        minus_di = 100.0 * minus_s / atr
        denom = plus_di + minus_di
        dx.append((i, 100.0 * abs(plus_di - minus_di) / denom if denom else 0.0))

    # ADX = Wilder-smoothed DX. Seed with the mean of the first n DX values.
    out: list[tuple[int, float]] = []
    adx = sum(v for _, v in dx[:n]) / n
    first_idx = dx[n - 1][0]
    out.append((first_idx, adx))
    for k in range(n, len(dx)):
        idx, dx_val = dx[k]
        adx = (adx * (n - 1) + dx_val) / n
        out.append((idx, adx))
    return out


def compute_adx_14(conn: duckdb.DuckDBPyConnection, symbols: list[str] | None = None) -> int:
    """Compute adx_14 for ``symbols`` (default: every symbol in ohlcv_daily),
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
            SELECT symbol, trade_date, high, low, close, ingested_at,
                   row_number() OVER (
                       PARTITION BY symbol, trade_date ORDER BY ingested_at DESC
                   ) AS rn
            FROM ohlcv_daily
            WHERE high IS NOT NULL AND low IS NOT NULL AND close IS NOT NULL {where}
        )
        SELECT symbol, trade_date, high, low, close, ingested_at
        FROM latest WHERE rn = 1
        ORDER BY symbol, trade_date
        """,
        params,
    ).fetchall()

    by_symbol: dict[str, list[tuple[date, float, float, float, datetime]]] = {}
    for symbol, trade_date, high, low, close, ingested_at in rows:
        by_symbol.setdefault(symbol, []).append((trade_date, high, low, close, ingested_at))

    values: list[FeatureValue] = []
    for symbol, series in by_symbol.items():
        highs = [r[1] for r in series]
        lows = [r[2] for r in series]
        closes = [r[3] for r in series]
        for idx, adx in wilder_adx(highs, lows, closes):
            trade_date, _, _, _, ingested_at = series[idx]
            values.append(
                FeatureValue(
                    feature_date=trade_date,
                    symbol=symbol,
                    value=adx,
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
