import json
from datetime import datetime, timezone

import duckdb
import pytest

from stockmoney.data.classification import record_candidate, record_sentiment
from stockmoney.data.db import run_migrations


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def test_record_sentiment_writes_row_for_known_symbol():
    conn = _conn()
    record_sentiment(conn, {
        "symbol": "nvda", "platform": "reddit", "hour": "2026-07-10T00:00:00Z",
        "sentiment_score": 0.6, "post_count": 3,
    })
    row = conn.execute(
        "SELECT symbol, platform, sentiment_score, post_count FROM alt_social_hourly"
    ).fetchone()
    assert row == ("NVDA", "reddit", 0.6, 3)  # symbol upper-cased


def test_record_sentiment_rejects_unknown_symbol():
    conn = _conn()
    with pytest.raises(ValueError, match="not an active watchlist member"):
        record_sentiment(conn, {
            "symbol": "ZZZZ", "platform": "reddit", "hour": "2026-07-10T00:00:00Z",
            "sentiment_score": 0.1, "post_count": 1,
        })
    assert conn.execute("SELECT count(*) FROM alt_social_hourly").fetchone()[0] == 0


def test_record_sentiment_rejects_removed_watchlist_member():
    conn = _conn()
    conn.execute(
        "UPDATE watchlist_members SET removed_date = CURRENT_DATE WHERE symbol = 'NVDA'"
    )
    with pytest.raises(ValueError, match="not an active watchlist member"):
        record_sentiment(conn, {
            "symbol": "NVDA", "platform": "reddit", "hour": "2026-07-10T00:00:00Z",
            "sentiment_score": 0.1, "post_count": 1,
        })


def test_record_sentiment_missing_fields_raises():
    conn = _conn()
    with pytest.raises(ValueError, match="missing fields"):
        record_sentiment(conn, {"symbol": "NVDA"})


def test_record_sentiment_never_writes_to_watchlist_members():
    conn = _conn()
    before = conn.execute("SELECT count(*) FROM watchlist_members").fetchone()[0]
    record_sentiment(conn, {
        "symbol": "NVDA", "platform": "reddit", "hour": "2026-07-10T00:00:00Z",
        "sentiment_score": 0.6, "post_count": 3,
    })
    after = conn.execute("SELECT count(*) FROM watchlist_members").fetchone()[0]
    assert before == after


def test_record_sentiment_without_item_id_does_not_touch_scan_classifications():
    conn = _conn()
    record_sentiment(conn, {
        "symbol": "NVDA", "platform": "reddit", "hour": "2026-07-10T00:00:00Z",
        "sentiment_score": 0.6, "post_count": 3,
    })
    assert conn.execute("SELECT count(*) FROM scan_classifications").fetchone()[0] == 0


def test_record_sentiment_with_item_id_indexes_symbol_evidence():
    conn = _conn()
    record_sentiment(conn, {
        "symbol": "NVDA", "platform": "reddit", "hour": "2026-07-10T00:00:00Z",
        "sentiment_score": 0.6, "post_count": 1, "item_id": "post_1",
    })
    row = conn.execute(
        "SELECT item_id, item_type, symbols FROM scan_classifications"
    ).fetchone()
    assert row == ("post_1", "reddit", '["NVDA"]')


def test_record_sentiment_maps_rss_platform_to_news_item_type():
    conn = _conn()
    record_sentiment(conn, {
        "symbol": "NVDA", "platform": "rss", "hour": "2026-07-10T00:00:00Z",
        "sentiment_score": 0.6, "post_count": 1, "item_id": "article_1",
    })
    row = conn.execute("SELECT item_type FROM scan_classifications").fetchone()
    assert row == ("news",)


def test_record_sentiment_merges_multiple_symbols_for_the_same_item():
    conn = _conn()
    record_sentiment(conn, {
        "symbol": "NVDA", "platform": "reddit", "hour": "2026-07-10T00:00:00Z",
        "sentiment_score": 0.6, "post_count": 1, "item_id": "post_1",
    })
    record_sentiment(conn, {
        "symbol": "AMD", "platform": "reddit", "hour": "2026-07-10T00:00:00Z",
        "sentiment_score": 0.2, "post_count": 1, "item_id": "post_1",
    })
    rows = conn.execute(
        "SELECT symbols FROM scan_classifications WHERE item_id = 'post_1'"
    ).fetchall()
    assert len(rows) == 1  # one row, not two -- merged not duplicated
    assert sorted(json.loads(rows[0][0])) == ["AMD", "NVDA"]


def test_record_candidate_with_theme_only():
    conn = _conn()
    record_candidate(conn, {
        "theme": "thermal_management",
        "rationale": "Rising mention volume for datacenter cooling across 3 sources",
        "evidence_count": 12,
        "source_refs": ["post_1", "article_2"],
    })
    row = conn.execute(
        "SELECT symbol, theme, rationale, evidence_count, status FROM watchlist_candidates"
    ).fetchone()
    assert row == (
        None, "thermal_management",
        "Rising mention volume for datacenter cooling across 3 sources",
        12, "proposed",
    )


def test_record_candidate_with_symbol():
    conn = _conn()
    record_candidate(conn, {"symbol": "pltr", "rationale": "Surging discussion volume"})
    row = conn.execute("SELECT symbol, theme FROM watchlist_candidates").fetchone()
    assert row == ("PLTR", None)


def test_record_candidate_requires_symbol_or_theme():
    conn = _conn()
    with pytest.raises(ValueError, match="symbol.*or.*theme"):
        record_candidate(conn, {"rationale": "vague feeling, no evidence"})


def test_record_candidate_requires_rationale():
    conn = _conn()
    with pytest.raises(ValueError, match="rationale"):
        record_candidate(conn, {"symbol": "PLTR"})


def test_record_candidate_never_writes_to_watchlist_members():
    conn = _conn()
    before = conn.execute("SELECT count(*) FROM watchlist_members").fetchone()[0]
    record_candidate(conn, {"symbol": "RKLB", "rationale": "New space-sector chatter"})
    after = conn.execute("SELECT count(*) FROM watchlist_members").fetchone()[0]
    assert before == after
    # RKLB must not appear in watchlist_members even though it was "proposed".
    assert conn.execute(
        "SELECT count(*) FROM watchlist_members WHERE symbol = 'RKLB'"
    ).fetchone()[0] == 0


def test_record_candidate_source_refs_stored_as_json():
    conn = _conn()
    record_candidate(conn, {
        "theme": "memory_cycle", "rationale": "DRAM pricing threads spiking",
        "source_refs": ["a", "b", "c"],
    })
    stored = conn.execute("SELECT source_refs FROM watchlist_candidates").fetchone()[0]
    assert stored == '["a", "b", "c"]'
