from datetime import date, datetime, timedelta, timezone

import duckdb
import polars as pl
import pytest

from stockmoney.data.db import MARKET_SYMBOL, append_rows, run_migrations
from stockmoney.data.features.gdelt_sentiment import (
    AVGTONE_FEATURE,
    GOLDSTEIN_FEATURE,
    compute_gdelt_sentiment,
)

NOW = datetime.now(timezone.utc)


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _seed_trading_days(conn, days: list[date]):
    n = len(days)
    append_rows(conn, "ohlcv_daily", pl.DataFrame({
        "symbol": ["NVDA"] * n, "trade_date": days, "close": [100.0] * n,
        "source": ["test"] * n, "ingested_at": [datetime(d.year, d.month, d.day, tzinfo=timezone.utc) for d in days],
    }))


def _seed_event(
    conn, *, event_id: str, event_date: date, tone: float, goldstein: float | None = -2.0,
    num_mentions: int = 1, ingested_at: datetime | None = None,
):
    ia = ingested_at or datetime(event_date.year, event_date.month, event_date.day, 23, 59, 59, tzinfo=timezone.utc)
    append_rows(conn, "event_news_gdelt", pl.DataFrame({
        "gdelt_event_id": [event_id], "event_datetime": [datetime(event_date.year, event_date.month, event_date.day, tzinfo=timezone.utc)],
        "symbol": [None], "event_class": ["14"], "geo_lat": [None], "geo_lon": [None],
        "tone_score": [tone], "goldstein_scale": [goldstein], "num_mentions": [num_mentions],
        "num_sources": [1], "num_articles": [1], "source": ["gdelt"], "ingested_at": [ia],
    }))


def test_mention_weighted_tone_on_a_real_event_day():
    conn = _conn()
    d0 = date(2024, 1, 1)
    _seed_trading_days(conn, [d0])
    _seed_event(conn, event_id="a", event_date=d0, tone=10.0, num_mentions=1)
    _seed_event(conn, event_id="b", event_date=d0, tone=-2.0, num_mentions=3)

    compute_gdelt_sentiment(conn)

    value = conn.execute(
        "SELECT feature_value FROM feature_store WHERE feature_name = ? AND feature_date = ?",
        [AVGTONE_FEATURE, d0],
    ).fetchone()[0]
    # weighted: (10*1 + -2*3) / (1+3) = 4/4 = 1.0
    assert value == pytest.approx(1.0)


def test_goldstein_feature_also_computed():
    conn = _conn()
    d0 = date(2024, 1, 1)
    _seed_trading_days(conn, [d0])
    _seed_event(conn, event_id="a", event_date=d0, tone=5.0, goldstein=-8.0, num_mentions=2)

    compute_gdelt_sentiment(conn)

    value = conn.execute(
        "SELECT feature_value FROM feature_store WHERE feature_name = ? AND feature_date = ?",
        [GOLDSTEIN_FEATURE, d0],
    ).fetchone()[0]
    assert value == pytest.approx(-8.0)


def test_forward_fill_carries_within_cap_then_reads_neutral_beyond_it():
    """Cap is 3 trading days: an event on d0 should still be carried forward
    through d3 (days_since_real == 3, inclusive), then read neutral (0.0)
    starting d4 (days_since_real == 4) -- a quiet news day is itself
    informative, not "stale carry it forever" like macro.py's DXY pattern."""
    conn = _conn()
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(6)]  # d0..d5
    _seed_trading_days(conn, days)
    event_ingested_at = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    _seed_event(conn, event_id="a", event_date=days[0], tone=7.5, num_mentions=1, ingested_at=event_ingested_at)

    compute_gdelt_sentiment(conn)

    rows = dict(conn.execute(
        "SELECT feature_date, feature_value FROM feature_store WHERE feature_name = ?", [AVGTONE_FEATURE]
    ).fetchall())
    assert rows[days[0]] == pytest.approx(7.5)
    assert rows[days[1]] == pytest.approx(7.5)   # days_since_real=1, within cap
    assert rows[days[2]] == pytest.approx(7.5)   # days_since_real=2, within cap
    assert rows[days[3]] == pytest.approx(7.5)   # days_since_real=3, boundary, still within cap
    assert rows[days[4]] == pytest.approx(0.0)   # days_since_real=4, exceeds cap -- neutral
    assert rows[days[5]] == pytest.approx(0.0)


def test_available_at_pinned_correctly_for_carried_vs_neutral_days():
    conn = _conn()
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(6)]
    _seed_trading_days(conn, days)
    event_ingested_at = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    _seed_event(conn, event_id="a", event_date=days[0], tone=1.0, ingested_at=event_ingested_at)

    compute_gdelt_sentiment(conn)

    available_at = dict(conn.execute(
        "SELECT feature_date, available_at FROM feature_store WHERE feature_name = ?", [AVGTONE_FEATURE]
    ).fetchall())
    # Carried-forward day (day 2, within cap): available_at pinned to the
    # original event's ingested_at, never advances to the trading day itself.
    assert available_at[days[2]] == event_ingested_at
    # Neutral day (day 4, beyond cap): available_at is that day's own end-of-day.
    assert available_at[days[4]] == datetime(days[4].year, days[4].month, days[4].day, 23, 59, 59, tzinfo=timezone.utc)


def test_no_events_at_all_reads_neutral_for_every_trading_day():
    conn = _conn()
    days = [date(2024, 1, 1), date(2024, 1, 2)]
    _seed_trading_days(conn, days)

    n = compute_gdelt_sentiment(conn)
    assert n == 4  # 2 trading days x 2 features (avgtone + goldstein)

    rows = conn.execute(
        "SELECT feature_value FROM feature_store WHERE feature_name = ?", [AVGTONE_FEATURE]
    ).fetchall()
    assert all(v == pytest.approx(0.0) for (v,) in rows)


def test_written_symbol_is_market_symbol():
    conn = _conn()
    d0 = date(2024, 1, 1)
    _seed_trading_days(conn, [d0])
    _seed_event(conn, event_id="a", event_date=d0, tone=1.0)

    compute_gdelt_sentiment(conn)

    symbol = conn.execute(
        "SELECT DISTINCT symbol FROM feature_store WHERE feature_name = ?", [AVGTONE_FEATURE]
    ).fetchone()[0]
    assert symbol == MARKET_SYMBOL
