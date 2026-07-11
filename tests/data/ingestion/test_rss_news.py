from datetime import datetime, timezone

import duckdb

from stockmoney.data.db import run_migrations
from stockmoney.data.ingestion.rss_news import fetch_news, ingest_news


class _FakeEntry(dict):
    """feedparser entries behave like dicts with .get()."""


class _FakeFeed:
    def __init__(self, entries):
        self.entries = entries


def _mock_parse(feeds_by_url):
    def _parse(url):
        return _FakeFeed(feeds_by_url.get(url, []))
    return _parse


def test_fetch_news_reshapes_entries_with_id(monkeypatch):
    entries = [
        _FakeEntry(
            id="guid-1", title="Stocks rally", summary="Markets up today",
            link="https://example.com/a",
            published_parsed=(2026, 7, 9, 12, 0, 0, 0, 0, 0),
        )
    ]
    monkeypatch.setattr(
        "feedparser.parse", _mock_parse({"http://feed1": entries})
    )

    df = fetch_news(feeds={"testsrc": "http://feed1"})

    assert df.height == 1
    row = df.row(0, named=True)
    assert row["article_id"] == "guid-1"
    assert row["source_name"] == "testsrc"
    assert row["title"] == "Stocks rally"
    assert row["summary"] == "Markets up today"
    assert row["url"] == "https://example.com/a"
    assert row["published_at"] == datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
    assert row["source"] == "rss"


def test_fetch_news_falls_back_to_link_hash_when_no_id(monkeypatch):
    entries = [
        _FakeEntry(
            title="No guid article", link="https://example.com/b",
            published_parsed=(2026, 7, 9, 12, 0, 0, 0, 0, 0),
        )
    ]
    monkeypatch.setattr("feedparser.parse", _mock_parse({"http://feed2": entries}))

    df = fetch_news(feeds={"nosrc": "http://feed2"})
    assert df.height == 1
    article_id = df.row(0, named=True)["article_id"]
    assert article_id.startswith("nosrc:")
    assert len(article_id) == len("nosrc:") + 16


def test_fetch_news_missing_published_parsed_yields_null_date(monkeypatch):
    entries = [_FakeEntry(id="g", title="t", link="http://x")]
    monkeypatch.setattr("feedparser.parse", _mock_parse({"http://feed3": entries}))

    df = fetch_news(feeds={"s": "http://feed3"})
    assert df.row(0, named=True)["published_at"] is None


def test_fetch_news_one_feed_failing_does_not_block_others(monkeypatch):
    def _parse(url):
        if url == "http://bad":
            raise RuntimeError("network error")
        return _FakeFeed([
            _FakeEntry(id="ok-1", title="fine", link="http://x",
                       published_parsed=(2026, 7, 9, 0, 0, 0, 0, 0, 0))
        ])

    monkeypatch.setattr("feedparser.parse", _parse)

    df = fetch_news(feeds={"bad": "http://bad", "good": "http://feed-ok"})
    assert df.height == 1
    assert df.row(0, named=True)["source_name"] == "good"


def test_fetch_news_all_feeds_failing_raises(monkeypatch):
    def _parse(url):
        raise RuntimeError("network error")

    monkeypatch.setattr("feedparser.parse", _parse)

    import pytest
    with pytest.raises(RuntimeError):
        fetch_news(feeds={"bad1": "http://bad1", "bad2": "http://bad2"})


def test_fetch_news_empty_feeds_returns_empty_frame(monkeypatch):
    monkeypatch.setattr("feedparser.parse", _mock_parse({}))
    df = fetch_news(feeds={"s": "http://empty"})
    assert df.height == 0
    assert df.columns == [
        "article_id", "source_name", "title", "summary", "url", "published_at", "source",
    ]


def test_ingest_news_end_to_end_and_audit(monkeypatch):
    entries = [
        _FakeEntry(id="g1", title="A", link="http://a",
                   published_parsed=(2026, 7, 9, 0, 0, 0, 0, 0, 0)),
        _FakeEntry(id="g2", title="B", link="http://b",
                   published_parsed=(2026, 7, 9, 0, 0, 0, 0, 0, 0)),
    ]
    monkeypatch.setattr("feedparser.parse", _mock_parse({"http://feed": entries}))

    conn = duckdb.connect(":memory:")
    run_migrations(conn)

    n = ingest_news(conn, feeds={"testsrc": "http://feed"})
    assert n == 2
    assert conn.execute("SELECT count(*) FROM news_articles_raw").fetchone()[0] == 2

    run_row = conn.execute(
        "SELECT status, source, target_table, rows_written FROM ingestion_runs"
    ).fetchone()
    assert run_row == ("success", "rss", "news_articles_raw", 2)
