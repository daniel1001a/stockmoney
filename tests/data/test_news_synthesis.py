"""Tests for the RSS -> news_items classification/upsert bridge.

Covers the heuristics (symbol tagging, type/sentiment) and the two disciplines
that matter: symbol-scoped sources are trusted over text matching, and
non-watchlist non-macro noise is dropped.
"""
from __future__ import annotations

from datetime import datetime, timezone

import duckdb

from stockmoney.data.db import run_migrations
from stockmoney.data.news_synthesis import (
    COMPANY_ALIASES,
    build_news_items,
    classify_type,
    score_sentiment,
    tag_symbol,
    upsert_news_items,
)

NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


def test_tag_symbol_matches_company_names():
    assert tag_symbol("Nvidia stock jumps on AI demand") == "NVDA"
    assert tag_symbol("Broadcom lands new ASIC customer") == "AVGO"
    assert tag_symbol("Gold prices climb on safe-haven bid") is None


def test_every_active_watchlist_symbol_has_a_news_alias():
    """Regression guard: the 20-symbol watchlist expansion (migration 038)
    shipped with COMPANY_ALIASES still only covering the original 11, so every
    new symbol silently got ZERO tagged news despite having a live
    prediction -- caught only by manually inspecting the live DB. A symbol
    on the active watchlist with no alias entry can never be tagged by
    tag_symbol, so this must never regress again."""
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    symbols = {
        r[0]
        for r in conn.execute(
            "SELECT symbol FROM watchlist_members WHERE removed_date IS NULL"
        ).fetchall()
    }
    conn.close()
    missing = symbols - set(COMPANY_ALIASES)
    assert not missing, f"watchlist symbols with no news alias: {sorted(missing)}"


def test_tag_symbol_excludes_bank_cited_as_rating_source():
    """Real headlines pulled from the live DB after the watchlist expansion:
    a sell-side bank's own name matched as an alias even when the bank is
    only the SOURCE of a rating on some other company, mis-tagging analyst
    sentiment onto the bank's own stock (JPM/GS) instead of leaving it
    untagged (the other company isn't on the watchlist, so None is correct)."""
    assert tag_symbol("American Express is a buy despite its expensive valuation, JPMorgan says") is None
    assert tag_symbol("Fed hike risk could test stocks despite strong earnings outlook, Goldman Sachs says") is None
    assert tag_symbol(
        "A hedge-fund trade blamed for a massive market blowup in 2024 has made a big comeback, Goldman Sachs says"
    ) is None


def test_tag_symbol_still_tags_genuine_bank_news():
    # Without a trailing "says"/"said", the bank IS the subject -- must still tag.
    assert tag_symbol("Goldman Sachs picks its favorite Chinese AI models") == "GS"
    assert tag_symbol("JPMorgan quarterly profit beats estimates") == "JPM"


def test_tag_symbol_says_exclusion_does_not_suppress_product_company_self_announcements():
    # The "<name> says" exclusion is scoped to rating-source banks only --
    # a product company announcing its own news must still be tagged.
    assert tag_symbol("Nvidia says it will ship Blackwell chips ahead of schedule") == "NVDA"


def test_classify_type_priority_and_macro():
    assert classify_type("Analyst upgrades Apple with a new price target", "AAPL") == "analyst_rating"
    assert classify_type("Nvidia quarterly revenue beats estimates", "NVDA") == "earnings"
    assert classify_type("Fed signals slower rate cuts amid inflation", None) == "macro"
    assert classify_type("Apple unveils new store design", "AAPL") == "headline"


def test_score_sentiment_direction():
    assert score_sentiment("stock surges to record high") > 0
    assert score_sentiment("shares plunge as company warns on guidance") < 0
    assert score_sentiment("company holds annual meeting") is None


def test_build_drops_irrelevant_keeps_symbol_and_macro():
    articles = [
        {"article_id": "a1", "title": "Nvidia stock rallies", "summary": "", "url": "u1",
         "source_name": "x", "published_at": NOW},
        {"article_id": "a2", "title": "Local bakery wins award", "summary": "", "url": "u2",
         "source_name": "x", "published_at": NOW},
        {"article_id": "a3", "title": "Fed holds rates as inflation cools", "summary": "", "url": "u3",
         "source_name": "x", "published_at": NOW},
        {"article_id": "a4", "title": "Something generic", "summary": "", "url": "u4",
         "source_name": "gnews", "published_at": NOW, "symbol_hint": "AMZN"},
    ]
    items = build_news_items(articles, now=NOW)
    by_id = {it.item_id: it for it in items}
    assert "rss:a1" in by_id and by_id["rss:a1"].symbol == "NVDA"
    assert "rss:a2" not in by_id  # irrelevant, dropped
    assert by_id["rss:a3"].item_type == "macro" and by_id["rss:a3"].symbol is None
    assert by_id["rss:a4"].symbol == "AMZN"  # symbol_hint trusted over text


def test_upsert_is_idempotent():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    articles = [{"article_id": "a1", "title": "Apple beats on revenue", "summary": "", "url": "u1",
                 "source_name": "x", "published_at": NOW}]
    items = build_news_items(articles, now=NOW)
    upsert_news_items(conn, items)
    upsert_news_items(conn, items)  # second run must not duplicate
    assert conn.execute("SELECT count(*) FROM news_items").fetchone()[0] == 1
