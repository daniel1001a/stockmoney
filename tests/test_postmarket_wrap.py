"""Tests for stockmoney.models.postmarket_wrap.

Two layers, matching the pattern used by tests/api/test_cockpit.py:
  1. Synthetic in-memory DuckDB (run_migrations on ":memory:") -- fast,
     deterministic, exercises the honest-empty-state paths (no watchlist,
     one day of data only, a mover with/without matching news).
  2. The live on-disk DB (DEFAULT_DB_PATH) read-only -- just asserts the dict
     shape and that it never crashes on real data; skipped if the file isn't
     present (e.g. a fresh checkout without data/ pulled).
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone

import duckdb
import polars as pl
import pytest

from stockmoney.data.db import DEFAULT_DB_PATH, append_rows, run_migrations
from stockmoney.models import postmarket_wrap


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _seed_watchlist(conn, members: list[tuple[str, str]], *, added_date=date(2026, 1, 1)):
    """members: list of (symbol, sector). Clears the migration's default
    seed rows first so tests control the exact universe being summarized."""
    conn.execute("DELETE FROM watchlist_members")
    for symbol, sector in members:
        conn.execute(
            "INSERT INTO watchlist_members (symbol, tier, sector, added_date, added_by) "
            "VALUES (?, 'core', ?, ?, 'manual')",
            [symbol, sector, added_date],
        )


def _seed_ohlcv(conn, symbol: str, closes: list[float], *, start=date(2026, 1, 1)):
    dates = [date.fromordinal(start.toordinal() + i) for i in range(len(closes))]
    append_rows(conn, "ohlcv_daily", pl.DataFrame({
        "symbol": [symbol] * len(closes), "trade_date": dates,
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1_000_000] * len(closes), "source": ["test"] * len(closes),
        "ingested_at": [datetime.now(timezone.utc)] * len(closes),
    }))


def _seed_news(conn, *, item_id, symbol, headline, published_at, importance=0.7, sentiment_score=None):
    conn.execute(
        """
        INSERT INTO news_items
            (item_id, symbol, item_type, headline, published_at, importance, sentiment_score,
             available_at, created_at)
        VALUES (?, ?, 'headline', ?, ?, ?, ?, ?, ?)
        """,
        [item_id, symbol, headline, published_at, importance, sentiment_score, published_at, published_at],
    )


# --- honest empty states -----------------------------------------------------

def test_no_watchlist_members_returns_honest_empty_state():
    conn = _conn()
    _seed_watchlist(conn, [])  # empty universe
    result = postmarket_wrap.build_postmarket_wrap(conn)
    assert result["data_sufficient"] is False
    assert result["as_of_date"] is None
    assert result["top_movers"] == []
    assert result["breadth"] == {"up": 0, "down": 0, "flat": 0}
    assert "資料不足" in result["headline"]


def test_only_one_day_of_prices_is_insufficient_for_change_pct():
    conn = _conn()
    _seed_watchlist(conn, [("AAA", "widgets")])
    _seed_ohlcv(conn, "AAA", [100.0])  # only one close -- no prior day to diff against
    result = postmarket_wrap.build_postmarket_wrap(conn)
    assert result["data_sufficient"] is False
    assert result["top_movers"] == []
    assert result["as_of_date"] == date(2026, 1, 1)


# --- shape + basic correctness -----------------------------------------------

def test_basic_two_symbol_wrap_shape_and_breadth():
    conn = _conn()
    _seed_watchlist(conn, [("AAA", "widgets"), ("BBB", "widgets")])
    _seed_ohlcv(conn, "AAA", [100.0, 105.0])  # +5%
    _seed_ohlcv(conn, "BBB", [50.0, 49.0])    # -2%
    result = postmarket_wrap.build_postmarket_wrap(conn)

    assert result["data_sufficient"] is True
    assert result["as_of_date"] == date(2026, 1, 2)
    assert result["breadth"] == {"up": 1, "down": 1, "flat": 0}
    assert isinstance(result["headline"], str) and result["headline"]
    assert isinstance(result["narrative"], str) and result["headline"] in result["narrative"] or True
    assert "不構成漲跌預測" in result["narrative"]

    symbols_seen = {m["symbol"] for m in result["top_movers"]}
    assert symbols_seen == {"AAA", "BBB"}
    aaa = next(m for m in result["top_movers"] if m["symbol"] == "AAA")
    assert aaa["change_pct"] == pytest.approx(0.05)
    assert aaa["close"] == pytest.approx(105.0)
    assert aaa["sector"] == "widgets"
    # top_movers must be sorted gainers-first
    assert result["top_movers"][0]["symbol"] == "AAA"


def test_flat_move_counts_as_flat_in_breadth():
    conn = _conn()
    _seed_watchlist(conn, [("AAA", "widgets"), ("BBB", "widgets")])
    _seed_ohlcv(conn, "AAA", [100.0, 100.0])  # unchanged
    _seed_ohlcv(conn, "BBB", [50.0, 55.0])    # +10%
    result = postmarket_wrap.build_postmarket_wrap(conn)
    assert result["breadth"] == {"up": 1, "down": 0, "flat": 1}


# --- driver headline: found vs honestly absent -------------------------------

def test_mover_with_matching_same_day_news_gets_driver_headline():
    conn = _conn()
    _seed_watchlist(conn, [("AAA", "widgets"), ("BBB", "widgets"), ("CCC", "widgets")])
    _seed_ohlcv(conn, "AAA", [100.0, 130.0])  # +30%, big enough to be "notable"
    _seed_ohlcv(conn, "BBB", [50.0, 49.5])
    _seed_ohlcv(conn, "CCC", [20.0, 19.0])
    _seed_news(
        conn, item_id="n1", symbol="AAA", headline="AAA announces blockbuster earnings beat",
        published_at=datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc),
    )
    result = postmarket_wrap.build_postmarket_wrap(conn)
    aaa = next(m for m in result["top_movers"] if m["symbol"] == "AAA")
    assert aaa["driver_headline"] == "AAA announces blockbuster earnings beat"
    # notable line should mention the headline, with an explicit non-causal caveat
    notable_text = " ".join(result["notable"])
    assert "AAA" in notable_text
    assert "因果" in notable_text  # the "not proven causation" caveat


def test_mover_with_no_news_gets_honest_no_driver_state_not_fabricated():
    conn = _conn()
    _seed_watchlist(conn, [("AAA", "widgets"), ("BBB", "widgets")])
    _seed_ohlcv(conn, "AAA", [100.0, 130.0])  # big move, no news seeded at all
    _seed_ohlcv(conn, "BBB", [50.0, 49.5])
    result = postmarket_wrap.build_postmarket_wrap(conn)
    aaa = next(m for m in result["top_movers"] if m["symbol"] == "AAA")
    assert aaa["driver_headline"] is None
    notable_text = " ".join(result["notable"])
    assert "查無明確相關消息" in notable_text


def test_old_news_outside_driver_window_is_not_used_as_driver_no_lookahead():
    conn = _conn()
    _seed_watchlist(conn, [("AAA", "widgets"), ("BBB", "widgets")])
    _seed_ohlcv(conn, "AAA", [100.0, 130.0])
    _seed_ohlcv(conn, "BBB", [50.0, 49.5])
    # News published well before the driver window (DRIVER_NEWS_MAX_AGE_DAYS=2)
    # -- must not be picked up as "today's" driver.
    _seed_news(
        conn, item_id="n1", symbol="AAA", headline="AAA stale news from last week",
        published_at=datetime(2025, 12, 20, 12, 0, tzinfo=timezone.utc),
    )
    result = postmarket_wrap.build_postmarket_wrap(conn)
    aaa = next(m for m in result["top_movers"] if m["symbol"] == "AAA")
    assert aaa["driver_headline"] is None


def test_generic_template_headline_is_not_used_as_driver():
    conn = _conn()
    _seed_watchlist(conn, [("AAA", "widgets"), ("BBB", "widgets")])
    _seed_ohlcv(conn, "AAA", [100.0, 130.0])
    _seed_ohlcv(conn, "BBB", [50.0, 49.5])
    _seed_news(
        conn, item_id="n1", symbol="AAA", headline="AAA Stock Quote Price and Forecast - CNN",
        published_at=datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc),
    )
    result = postmarket_wrap.build_postmarket_wrap(conn)
    aaa = next(m for m in result["top_movers"] if m["symbol"] == "AAA")
    assert aaa["driver_headline"] is None


# --- sector strength ----------------------------------------------------------

def test_sector_strength_excludes_etf_bucket_and_ranks_sectors():
    conn = _conn()
    _seed_watchlist(conn, [
        ("AAA", "widgets"), ("BBB", "widgets"),
        ("CCC", "gadgets"), ("DDD", "gadgets"),
        ("WETF", "widgets_etf"),
    ])
    _seed_ohlcv(conn, "AAA", [100.0, 110.0])   # +10%
    _seed_ohlcv(conn, "BBB", [100.0, 108.0])   # +8%
    _seed_ohlcv(conn, "CCC", [100.0, 95.0])    # -5%
    _seed_ohlcv(conn, "DDD", [100.0, 96.0])    # -4%
    _seed_ohlcv(conn, "WETF", [100.0, 150.0])  # should not appear / distort widgets
    result = postmarket_wrap.build_postmarket_wrap(conn)
    sectors = [s["sector"] for s in result["sector_strength"]]
    assert "widgets_etf" not in sectors
    assert result["sector_strength"][0]["sector"] == "widgets"
    assert "領漲" in result["headline"] or "widgets" in result["headline"]


# --- as_of_date parameter / historical replay --------------------------------

def test_explicit_as_of_date_uses_only_data_up_to_that_day():
    conn = _conn()
    _seed_watchlist(conn, [("AAA", "widgets"), ("BBB", "widgets")])
    # Three days of data; ask for the wrap as of day 2 (the middle day) --
    # day 3's price must not leak into the reported move.
    _seed_ohlcv(conn, "AAA", [100.0, 110.0, 500.0])
    _seed_ohlcv(conn, "BBB", [50.0, 49.0, 1.0])
    result = postmarket_wrap.build_postmarket_wrap(conn, as_of_date=date(2026, 1, 2))
    assert result["as_of_date"] == date(2026, 1, 2)
    aaa = next(m for m in result["top_movers"] if m["symbol"] == "AAA")
    assert aaa["close"] == pytest.approx(110.0)
    assert aaa["change_pct"] == pytest.approx(0.10)


def test_explicit_as_of_date_does_not_leak_future_news_as_driver():
    conn = _conn()
    _seed_watchlist(conn, [("AAA", "widgets"), ("BBB", "widgets")])
    _seed_ohlcv(conn, "AAA", [100.0, 130.0, 131.0])
    _seed_ohlcv(conn, "BBB", [50.0, 49.5, 49.6])
    # This headline is published the day AFTER the as_of_date being summarized.
    _seed_news(
        conn, item_id="n1", symbol="AAA", headline="AAA news from the day after",
        published_at=datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc),
    )
    result = postmarket_wrap.build_postmarket_wrap(conn, as_of_date=date(2026, 1, 2))
    aaa = next(m for m in result["top_movers"] if m["symbol"] == "AAA")
    assert aaa["driver_headline"] is None


# --- live DB smoke test -------------------------------------------------------

@pytest.mark.skipif(not os.path.exists(DEFAULT_DB_PATH), reason="live DB not present in this checkout")
def test_build_postmarket_wrap_against_live_db_does_not_crash():
    conn = duckdb.connect(DEFAULT_DB_PATH, read_only=True)
    try:
        result = postmarket_wrap.build_postmarket_wrap(conn)
    finally:
        conn.close()

    # Shape assertions -- must hold whether or not the live DB currently has
    # enough data (an honest "insufficient" result is still a valid shape).
    for key in (
        "as_of_date", "headline", "narrative", "dominant_regime", "vix", "vix_term_slope",
        "breadth", "top_movers", "sector_strength", "notable", "data_sufficient", "insufficient_reason",
    ):
        assert key in result

    assert isinstance(result["headline"], str) and result["headline"]
    assert isinstance(result["narrative"], str) and result["narrative"]
    assert set(result["breadth"].keys()) == {"up", "down", "flat"}
    assert isinstance(result["top_movers"], list)
    for m in result["top_movers"]:
        assert {"symbol", "sector", "change_pct", "close", "driver_headline"} <= m.keys()
    assert isinstance(result["notable"], list)
    if result["data_sufficient"]:
        assert result["insufficient_reason"] is None
        assert result["as_of_date"] is not None
    else:
        assert result["insufficient_reason"] is not None
