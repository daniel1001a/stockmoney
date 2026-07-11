"""Candidate market-wide "geopolitical/news sentiment" feature derived from
backfilled GDELT events (stockmoney.data.ingestion.gdelt_events). Written
under MARKET_SYMBOL, matching macro.py's scoping -- CLAUDE.md frames GDELT as
market-wide "事件-新聞," not per-symbol (see feature_matrix.py's symbol_by_
feature mapping for the same pattern applied to yield_curve/dxy/oil).

This is EXPLICITLY a candidate, not a promoted feature: per CLAUDE.md section
12, gdelt_avgtone_1d/gdelt_goldstein_1d must pass backtest_feature_ablation.py's
bootstrap significance test before being added to feature_matrix.FEATURE_
COLUMNS. Until then they sit here, computed and ready to test, exactly like
rsi_14/volume_zscore_20d before they were tested and rejected. Don't add them
to FEATURE_COLUMNS as part of routine maintenance -- that's a deliberate,
tested decision, not a wiring step.

Forward-fill, CAPPED -- a deliberate deviation from macro.py's unlimited-carry
pattern: macro.py forward-fills a low-frequency-but-real series (DXY has a
true value every day, just not always freshly reported). GDELT is different --
a quiet news day plausibly IS informative (genuinely neutral), not "value
unknown." So: carry the last real daily aggregate forward for at most
FORWARD_FILL_CAP_DAYS trading days, then read neutral (0.0) beyond that,
rather than carrying stale sentiment indefinitely. available_at for a
forward-filled day pins to the last contributing event's ingested_at (same
rule as macro.py); for a neutral-fill day, available_at is that trading day's
own end-of-day (UTC) -- "no relevant news happened" is only fully knowable by
end of that day, mirroring gdelt_events.py's own end-of-day convention for
ingested_at.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import duckdb

from stockmoney.data.db import MARKET_SYMBOL
from stockmoney.data.features.base import FeatureValue, write_features

# v1 -> v2 (2026-07-11): v1 was computed and written across the full OHLCV
# history while event_news_gdelt was still empty (pre-backfill graceful-
# degradation testing), so every v1 row is the neutral 0.0 fallback.
# write_features's immutability contract (never change a value under an
# already-written key) means those stale all-neutral rows would otherwise
# permanently block the real backfilled values from ever being written.
# Safe to bump: this candidate feature was never added to
# feature_matrix.FEATURE_COLUMNS, so nothing has trained on v1's values.
FEATURE_VERSION = "v2"
AVGTONE_FEATURE = "gdelt_avgtone_1d"
GOLDSTEIN_FEATURE = "gdelt_goldstein_1d"
FORWARD_FILL_CAP_DAYS = 3


def _trading_days(conn: duckdb.DuckDBPyConnection) -> list[date]:
    return [
        row[0]
        for row in conn.execute("SELECT DISTINCT trade_date FROM ohlcv_daily ORDER BY trade_date").fetchall()
    ]


def _daily_aggregates(conn: duckdb.DuckDBPyConnection) -> dict[date, tuple[float, float | None, datetime]]:
    """One entry per calendar day with contributing GDELT events:
    mention-weighted average tone_score and goldstein_scale, plus the day's
    available_at (max ingested_at among contributing events -- the
    aggregate isn't knowable until its last contributing event was
    ingested, same combining rule as macro.py's yield-curve feature)."""
    rows = conn.execute(
        """
        SELECT
            CAST(event_datetime AT TIME ZONE 'UTC' AS DATE) AS event_date,
            SUM(tone_score * COALESCE(num_mentions, 1)) / SUM(COALESCE(num_mentions, 1)) AS avg_tone,
            SUM(goldstein_scale * COALESCE(num_mentions, 1))
                / NULLIF(SUM(CASE WHEN goldstein_scale IS NOT NULL THEN COALESCE(num_mentions, 1) END), 0) AS avg_goldstein,
            max(ingested_at) AS available_at
        FROM event_news_gdelt
        WHERE tone_score IS NOT NULL
        GROUP BY CAST(event_datetime AT TIME ZONE 'UTC' AS DATE)
        ORDER BY event_date
        """
    ).fetchall()
    return {d: (tone, goldstein, available_at) for d, tone, goldstein, available_at in rows}


def _capped_forward_fill_and_neutral(
    trading_days: list[date],
    daily: dict[date, tuple[float, float | None, datetime]],
    *,
    metric_idx: int,
    cap_days: int = FORWARD_FILL_CAP_DAYS,
) -> list[tuple[date, float, datetime]]:
    out: list[tuple[date, float, datetime]] = []
    last_value: float | None = None
    last_available_at: datetime | None = None
    days_since_real: int | None = None

    for d in trading_days:
        entry = daily.get(d)
        value = entry[metric_idx] if entry else None
        if value is not None:
            last_value = value
            last_available_at = entry[2]
            days_since_real = 0
        elif days_since_real is not None:
            days_since_real += 1

        if last_value is not None and days_since_real is not None and days_since_real <= cap_days:
            out.append((d, last_value, last_available_at))
        else:
            out.append((d, 0.0, datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc)))
    return out


def compute_gdelt_sentiment(conn: duckdb.DuckDBPyConnection) -> int:
    daily = _daily_aggregates(conn)
    trading_days = _trading_days(conn)

    total = 0
    for feature_name, metric_idx in [(AVGTONE_FEATURE, 0), (GOLDSTEIN_FEATURE, 1)]:
        filled = _capped_forward_fill_and_neutral(trading_days, daily, metric_idx=metric_idx)
        values = [
            FeatureValue(feature_date=d, symbol=MARKET_SYMBOL, value=v, available_at=ia)
            for d, v, ia in filled
        ]
        total += write_features(
            conn, feature_name=feature_name, feature_version=FEATURE_VERSION,
            source_table="event_news_gdelt", values=values,
        )
    return total
