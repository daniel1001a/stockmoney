from datetime import datetime, timezone

import duckdb
import pytest

from stockmoney.data.db import run_migrations
from stockmoney.data.ingestion.reddit_scanner import fetch_reddit_posts, ingest_reddit_posts


class _FakeEntry(dict):
    """feedparser entries behave like dicts with .get()."""


class _FakeFeed:
    def __init__(self, entries):
        self.entries = entries


def _mock_fetch(feeds_by_url):
    def _fetch(url, user_agent):
        return _FakeFeed(feeds_by_url.get(url, []))
    return _fetch


def _entry(id="t3_p1", author="/u/alice"):
    return _FakeEntry(
        id=id, title="NVDA to the moon", summary="body text", author=author,
        link="https://reddit.com/r/stocks/p1",
        published_parsed=(2026, 7, 10, 0, 0, 0, 0, 0, 0),
    )


def test_fetch_reddit_posts_reshapes_entries():
    fetch_fn = _mock_fetch({"https://www.reddit.com/r/stocks/new/.rss": [_entry()]})
    df = fetch_reddit_posts(["stocks"], limit=25, delay_seconds=0, fetch_fn=fetch_fn)

    assert df.height == 1
    row = df.row(0, named=True)
    assert row["post_id"] == "t3_p1"
    assert row["subreddit"] == "stocks"
    assert row["title"] == "NVDA to the moon"
    assert row["body"] == "body text"
    assert row["author"] == "/u/alice"
    assert row["score"] is None  # RSS doesn't expose engagement metrics
    assert row["num_comments"] is None
    assert row["source"] == "reddit"
    assert row["posted_at"] == datetime(2026, 7, 10, tzinfo=timezone.utc)


def test_fetch_reddit_posts_missing_author_is_null():
    fetch_fn = _mock_fetch({"https://www.reddit.com/r/stocks/new/.rss": [_entry(author=None)]})
    df = fetch_reddit_posts(["stocks"], delay_seconds=0, fetch_fn=fetch_fn)
    assert df.row(0, named=True)["author"] is None


def test_fetch_reddit_posts_one_subreddit_failing_does_not_block_others():
    def _fetch(url, user_agent):
        if "wallstreetbets" in url:
            raise RuntimeError("429 rate limited")
        return _FakeFeed([_entry(id="ok")])

    df = fetch_reddit_posts(["wallstreetbets", "stocks"], delay_seconds=0, fetch_fn=_fetch)
    assert df.height == 1
    assert df.row(0, named=True)["post_id"] == "ok"


def test_fetch_reddit_posts_all_subreddits_failing_raises():
    def _fetch(url, user_agent):
        raise RuntimeError("429 rate limited")

    with pytest.raises(RuntimeError):
        fetch_reddit_posts(["a", "b"], delay_seconds=0, fetch_fn=_fetch)


def test_fetch_reddit_posts_empty_returns_empty_frame():
    fetch_fn = _mock_fetch({"https://www.reddit.com/r/stocks/new/.rss": []})
    df = fetch_reddit_posts(["stocks"], delay_seconds=0, fetch_fn=fetch_fn)
    assert df.height == 0
    assert df.columns == [
        "post_id", "subreddit", "title", "body", "author",
        "posted_at", "score", "num_comments", "url", "source",
    ]


def test_fetch_reddit_posts_respects_limit():
    entries = [_entry(id=f"t3_{i}") for i in range(10)]
    fetch_fn = _mock_fetch({"https://www.reddit.com/r/stocks/new/.rss": entries})
    df = fetch_reddit_posts(["stocks"], limit=3, delay_seconds=0, fetch_fn=fetch_fn)
    assert df.height == 3


def test_ingest_reddit_posts_writes_and_audits():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    fetch_fn = _mock_fetch({
        "https://www.reddit.com/r/stocks/new/.rss": [_entry(id="a"), _entry(id="b")]
    })

    n = ingest_reddit_posts(conn, ["stocks"], delay_seconds=0, fetch_fn=fetch_fn)

    assert n == 2
    assert conn.execute("SELECT count(*) FROM social_posts_raw").fetchone()[0] == 2
    run_row = conn.execute(
        "SELECT status, source, target_table, rows_written FROM ingestion_runs"
    ).fetchone()
    assert run_row == ("success", "reddit", "social_posts_raw", 2)
