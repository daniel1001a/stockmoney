import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from stockmoney.data.db import append_rows, run_migrations
import fetch_catalyst_evidence

NOW = datetime.now(timezone.utc)


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _mark_classified(conn, *, item_id, item_type, symbols, minutes_ago=30):
    conn.execute(
        "INSERT INTO scan_classifications VALUES (?, ?, ?, 'v1', 'sentiment', ?)",
        [item_id, item_type, NOW - timedelta(minutes=minutes_ago), json.dumps(symbols)],
    )


def _seed_article(conn, article_id="a1", title="chips rally", summary="capex accelerating"):
    append_rows(conn, "news_articles_raw", pl.DataFrame({
        "article_id": [article_id], "source_name": ["seeking_alpha"], "title": [title],
        "summary": [summary], "url": ["http://x"], "published_at": [NOW],
        "source": ["rss"], "ingested_at": [NOW],
    }))


def test_prints_empty_symbols_when_nothing_classified(monkeypatch, capsys):
    conn = _conn()
    monkeypatch.setattr(fetch_catalyst_evidence, "get_connection", lambda db_path: conn)

    fetch_catalyst_evidence.main(hours=48, db_path="unused")
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"symbols": []}


def test_prints_evidence_packet_for_symbol_with_recent_signal(monkeypatch, capsys):
    conn = _conn()
    monkeypatch.setattr(fetch_catalyst_evidence, "get_connection", lambda db_path: conn)

    _seed_article(conn)
    _mark_classified(conn, item_id="a1", item_type="news", symbols=["NVDA"])

    fetch_catalyst_evidence.main(hours=48, db_path="unused")
    payload = json.loads(capsys.readouterr().out)

    assert len(payload["symbols"]) == 1
    entry = payload["symbols"][0]
    assert entry["symbol"] == "NVDA"
    assert entry["evidence"]["sources"][0]["title"] == "chips rally"


def test_excludes_symbols_outside_the_signal_window(monkeypatch, capsys):
    conn = _conn()
    monkeypatch.setattr(fetch_catalyst_evidence, "get_connection", lambda db_path: conn)

    _seed_article(conn)
    _mark_classified(conn, item_id="a1", item_type="news", symbols=["NVDA"], minutes_ago=60 * 100)

    fetch_catalyst_evidence.main(hours=48, db_path="unused")
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"symbols": []}
