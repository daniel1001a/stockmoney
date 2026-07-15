from datetime import date, datetime, timezone

import duckdb
import polars as pl
import pytest

from stockmoney.api import cockpit, queries
from stockmoney.data.db import append_rows, run_migrations


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _seed_ohlcv(conn, symbol, closes: list[float], volumes: list[int], *, start=date(2026, 1, 1)):
    """Seed `len(closes)` consecutive trading days (business-day-ish, just +1
    calendar day per row -- good enough for these window-based unit tests,
    which only care about the last N rows in order)."""
    dates = [date.fromordinal(start.toordinal() + i) for i in range(len(closes))]
    append_rows(conn, "ohlcv_daily", pl.DataFrame({
        "symbol": [symbol] * len(closes), "trade_date": dates,
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": volumes, "source": ["test"] * len(closes),
        "ingested_at": [datetime.now(timezone.utc)] * len(closes),
    }))


def _seed_macro_news(conn, *, item_id, headline, published_at, source_name="TEST", importance=0.65):
    conn.execute(
        """
        INSERT INTO news_items
            (item_id, symbol, item_type, headline, source_name, published_at, importance, available_at, created_at)
        VALUES (?, NULL, 'macro', ?, ?, ?, ?, ?, ?)
        """,
        [item_id, headline, source_name, published_at, importance, published_at, published_at],
    )


def _seed_symbol_news(conn, *, item_id, symbol, headline, published_at, importance=0.65):
    conn.execute(
        """
        INSERT INTO news_items
            (item_id, symbol, item_type, headline, published_at, importance, available_at, created_at)
        VALUES (?, ?, 'headline', ?, ?, ?, ?, ?)
        """,
        [item_id, symbol, headline, published_at, importance, published_at, published_at],
    )


# --- volume_signal -----------------------------------------------------------

def test_volume_signal_insufficient_history():
    rows = [(date(2026, 1, i + 1), 1, 1, 1, 1, 1000) for i in range(10)]  # only 10 days, need 21
    result = cockpit.volume_signal(rows)
    assert result["state"] == "資料不足"
    assert result["ratio"] is None


def test_volume_signal_flags_high_volume():
    baseline = [(date(2026, 1, i + 1), 1, 1, 1, 1, 1_000_000) for i in range(20)]
    today = [(date(2026, 1, 21), 1, 1, 1, 1, 2_000_000)]  # 2x the 20d average
    result = cockpit.volume_signal(baseline + today)
    assert result["state"] == "放量"
    assert result["ratio"] == pytest.approx(2.0)


def test_volume_signal_flags_low_volume():
    baseline = [(date(2026, 1, i + 1), 1, 1, 1, 1, 1_000_000) for i in range(20)]
    today = [(date(2026, 1, 21), 1, 1, 1, 1, 300_000)]  # 0.3x the 20d average
    result = cockpit.volume_signal(baseline + today)
    assert result["state"] == "縮量"


def test_volume_signal_normal_range():
    baseline = [(date(2026, 1, i + 1), 1, 1, 1, 1, 1_000_000) for i in range(20)]
    today = [(date(2026, 1, 21), 1, 1, 1, 1, 1_100_000)]  # 1.1x -- inside the normal band
    result = cockpit.volume_signal(baseline + today)
    assert result["state"] == "量能正常"


# --- sector_linkage_map -------------------------------------------------------

def test_sector_linkage_marks_outlier_as_breaking_from_sector():
    members = [
        {"symbol": "AAA", "sector": "widgets", "tier": "core"},
        {"symbol": "BBB", "sector": "widgets", "tier": "core"},
        {"symbol": "CCC", "sector": "widgets", "tier": "core"},
    ]
    # AAA/BBB move together (~0%); CCC gaps up hard on its own news.
    returns = {
        "AAA": {"ret_1d": 0.001, "ret_5d": None},
        "BBB": {"ret_1d": -0.002, "ret_5d": None},
        "CCC": {"ret_1d": 0.15, "ret_5d": None},
    }
    linkage = cockpit.sector_linkage_map(members, returns)
    assert linkage["CCC"]["state"] == "脫離板塊獨走"
    assert linkage["AAA"]["state"] == "跟隨板塊同步"


def test_sector_linkage_etf_bucket_marked_not_applicable():
    members = [{"symbol": "SOXL", "sector": "semiconductor_etf", "tier": "core"}]
    returns = {"SOXL": {"ret_1d": -0.05, "ret_5d": None}}
    linkage = cockpit.sector_linkage_map(members, returns)
    assert linkage["SOXL"]["state"] == "不適用"
    assert linkage["SOXL"]["z"] is None


def test_sector_linkage_insufficient_peers():
    members = [{"symbol": "AAA", "sector": "widgets", "tier": "core"}]
    returns = {"AAA": {"ret_1d": 0.01, "ret_5d": None}}
    linkage = cockpit.sector_linkage_map(members, returns)
    assert linkage["AAA"]["state"] == "資料不足"


# --- sector_rotation -----------------------------------------------------------

def test_sector_rotation_ranks_sectors_and_excludes_etfs():
    members = [
        {"symbol": "AAA", "sector": "widgets", "tier": "core"},
        {"symbol": "BBB", "sector": "gadgets", "tier": "core"},
        {"symbol": "ETF1", "sector": "widgets_etf", "tier": "core"},
    ]
    returns = {
        "AAA": {"ret_1d": 0.02, "ret_5d": 0.05},
        "BBB": {"ret_1d": -0.01, "ret_5d": -0.03},
        "ETF1": {"ret_1d": 0.06, "ret_5d": 0.15},  # should not appear -- it's an ETF bucket
    }
    rotation = cockpit.sector_rotation(members, returns)
    sectors = [r["sector"] for r in rotation]
    assert "widgets_etf" not in sectors
    assert rotation[0]["sector"] == "widgets"  # strongest first
    assert rotation[0]["rank"] == 1
    assert rotation[-1]["sector"] == "gadgets"


# --- is_generic_headline / news filtering -------------------------------------

@pytest.mark.parametrize("headline", [
    "AAPL Stock Quote Price and Forecast - CNN",
    "GOOGL Stock Quote Price and Forecast - CNN",
    "some stock QUOTE page",
])
def test_is_generic_headline_true_for_templates(headline):
    assert queries.is_generic_headline(headline) is True


@pytest.mark.parametrize("headline", [
    "TSMC Posts Stronger-Than-Expected June Sales",
    "Meta's Louisiana data center investment to reach $50 billion",
    None,
])
def test_is_generic_headline_false_for_real_news(headline):
    assert queries.is_generic_headline(headline) is False


def test_latest_symbol_news_skips_generic_headline_for_real_one():
    conn = _conn()
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    # Generic template headline is MORE "important" but should still lose to
    # the real headline once filtered.
    _seed_symbol_news(conn, item_id="n1", symbol="AAPL", headline="AAPL Stock Quote Price and Forecast - CNN",
                       published_at=now, importance=0.9)
    _seed_symbol_news(conn, item_id="n2", symbol="AAPL", headline="Apple unveils new AI chip roadmap",
                       published_at=now, importance=0.5)
    news = queries._latest_symbol_news(conn)
    assert news["AAPL"]["headline"] == "Apple unveils new AI chip roadmap"


def test_latest_symbol_news_falls_back_to_none_when_only_generic():
    conn = _conn()
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    _seed_symbol_news(conn, item_id="n1", symbol="AAPL", headline="AAPL Stock Quote Price and Forecast - CNN",
                       published_at=now)
    news = queries._latest_symbol_news(conn)
    assert "AAPL" not in news


# --- build_narrative -----------------------------------------------------------

_MARKET_STUB = {
    "as_of_date": date(2026, 7, 13), "n_symbols": 31, "dominant_regime": "中波動",
    "vix": 15.0, "vix_term_slope": -0.5,
}


def test_build_narrative_uses_real_macro_news_when_available():
    conn = _conn()
    _seed_macro_news(conn, item_id="m1", headline="Fed signals rate pause amid cooling inflation",
                      published_at=datetime.now(timezone.utc))
    result = cockpit.build_narrative(conn, _MARKET_STUB, rotation=[])
    assert result["basis"] == "macro_news"
    assert "Fed signals rate pause" in result["text"]
    assert len(result["macro_items"]) == 1


def test_build_narrative_falls_back_when_no_macro_news():
    conn = _conn()
    result = cockpit.build_narrative(conn, _MARKET_STUB, rotation=[])
    assert result["basis"] == "fallback_regime_sector"
    assert "退回" in result["text"]
    assert result["macro_items"] == []


def test_build_narrative_filters_generic_macro_headline():
    conn = _conn()
    _seed_macro_news(conn, item_id="m1", headline="Market Stock Quote Price and Forecast - CNN",
                      published_at=datetime.now(timezone.utc))
    result = cockpit.build_narrative(conn, _MARKET_STUB, rotation=[])
    assert result["basis"] == "fallback_regime_sector"  # the only macro item was generic, so no real macro news
