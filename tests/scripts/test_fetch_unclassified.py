import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from stockmoney.data.db import append_rows, run_migrations
import fetch_unclassified


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def test_dedupes_reingested_article_to_latest_version(monkeypatch, capsys):
    conn = _conn()
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(fetch_unclassified, "get_connection", lambda db_path: conn)

    # Same article_id ingested twice (simulating two ingest runs in one night).
    for i in range(2):
        append_rows(conn, "news_articles_raw", pl.DataFrame({
            "article_id": ["a1"],
            "source_name": ["wsj"],
            "title": [f"version {i}"],
            "summary": [None],
            "url": ["http://x"],
            "published_at": [now],
            "source": ["rss"],
            "ingested_at": [now + timedelta(minutes=i)],
        }))

    fetch_unclassified.main(hours=6, db_path="unused")
    out = capsys.readouterr().out
    import json
    payload = json.loads(out)
    assert len(payload["news_articles"]) == 1
    assert payload["news_articles"][0]["title"] == "version 1"  # latest ingested_at wins


def test_excludes_content_outside_window(monkeypatch, capsys):
    conn = _conn()
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(fetch_unclassified, "get_connection", lambda db_path: conn)

    append_rows(conn, "news_articles_raw", pl.DataFrame({
        "article_id": ["old"], "source_name": ["wsj"], "title": ["stale"],
        "summary": [None], "url": ["http://x"], "published_at": [now],
        "source": ["rss"], "ingested_at": [now - timedelta(hours=10)],
    }))
    append_rows(conn, "news_articles_raw", pl.DataFrame({
        "article_id": ["new"], "source_name": ["wsj"], "title": ["fresh"],
        "summary": [None], "url": ["http://y"], "published_at": [now],
        "source": ["rss"], "ingested_at": [now],
    }))

    fetch_unclassified.main(hours=6, db_path="unused")
    import json
    payload = json.loads(capsys.readouterr().out)
    titles = [a["title"] for a in payload["news_articles"]]
    assert titles == ["fresh"]
