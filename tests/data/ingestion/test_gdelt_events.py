from datetime import date, datetime, timezone

import duckdb
import pytest

from stockmoney.data.db import run_migrations
from stockmoney.data.ingestion import gdelt_events


class _FakeJob:
    def __init__(self, *, total_bytes_processed=0, rows=None):
        self.total_bytes_processed = total_bytes_processed
        self._rows = rows or []

    def result(self):
        return self._rows


class _FakeClient:
    def __init__(self, *, dry_run_bytes=0, rows=None):
        self.dry_run_bytes = dry_run_bytes
        self.rows = rows or []
        self.queries = []

    def query(self, sql, job_config=None):
        self.queries.append((sql, job_config))
        if job_config is not None and job_config.dry_run:
            return _FakeJob(total_bytes_processed=self.dry_run_bytes)
        return _FakeJob(rows=self.rows)


def _fake_row(**overrides):
    row = {
        "GLOBALEVENTID": 123456,
        "SQLDATE": 20240115,
        "Actor1Name": "UNITED STATES",
        "Actor1CountryCode": "USA",
        "Actor2Name": "CHINA",
        "Actor2CountryCode": "CHN",
        "EventCode": "138",
        "EventRootCode": "13",
        "QuadClass": 3,
        "GoldsteinScale": -5.0,
        "NumMentions": 10,
        "NumSources": 3,
        "NumArticles": 7,
        "AvgTone": -3.2,
        "ActionGeo_Lat": 35.0,
        "ActionGeo_Long": 139.0,
        "SOURCEURL": "http://example.com/article",
    }
    row.update(overrides)
    return row


def test_fetch_gdelt_events_shapes_dataframe_correctly():
    client = _FakeClient(dry_run_bytes=1_000_000, rows=[_fake_row()])
    df = gdelt_events.fetch_gdelt_events(date(2024, 1, 1), date(2024, 1, 31), client=client)

    assert df.height == 1
    row = df.row(0, named=True)
    assert row["gdelt_event_id"] == "123456"
    assert row["symbol"] is None
    assert row["event_class"] == "13"
    assert row["geo_lat"] == 35.0
    assert row["geo_lon"] == 139.0
    assert row["tone_score"] == -3.2
    assert row["goldstein_scale"] == -5.0
    assert row["num_mentions"] == 10
    assert row["num_sources"] == 3
    assert row["num_articles"] == 7
    assert row["source"] == "gdelt"


def test_fetch_gdelt_events_ingested_at_derived_from_sqldate_not_now():
    """The single most important test in this module: ingested_at must be
    derived from the event's own SQLDATE (end-of-day UTC, a documented
    conservative approximation -- see gdelt_events.py's module docstring),
    never wall-clock "now". This is what keeps a historical backfill honest
    instead of making 2024 news look knowable today."""
    client = _FakeClient(dry_run_bytes=0, rows=[_fake_row(SQLDATE=20240115)])
    df = gdelt_events.fetch_gdelt_events(date(2024, 1, 1), date(2024, 1, 31), client=client)

    ingested_at = df.row(0, named=True)["ingested_at"]
    assert ingested_at == datetime(2024, 1, 15, 23, 59, 59, tzinfo=timezone.utc)

    event_datetime = df.row(0, named=True)["event_datetime"]
    assert event_datetime == datetime(2024, 1, 15, tzinfo=timezone.utc)


def test_fetch_gdelt_events_raises_when_dry_run_exceeds_threshold():
    client = _FakeClient(dry_run_bytes=10 * 1024**3, rows=[_fake_row()])  # 10 GB
    with pytest.raises(RuntimeError, match="exceeding"):
        gdelt_events.fetch_gdelt_events(
            date(2024, 1, 1), date(2024, 1, 31), client=client, max_bytes_billed=5 * 1024**3
        )
    # The real (billed) query must never run when the dry-run check fails.
    assert len(client.queries) == 1


def test_fetch_gdelt_events_empty_results_returns_empty_schema():
    client = _FakeClient(dry_run_bytes=0, rows=[])
    df = gdelt_events.fetch_gdelt_events(date(2024, 1, 1), date(2024, 1, 31), client=client)
    assert df.height == 0
    assert "gdelt_event_id" in df.columns
    assert "ingested_at" in df.columns


def test_ingest_gdelt_events_end_to_end_persists_honest_ingested_at():
    """End-to-end proof that run_ingestion -> append_rows does NOT override
    the per-row ingested_at this connector sets -- the exact mechanism the
    whole backfill's look-ahead safety depends on."""
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    client = _FakeClient(dry_run_bytes=0, rows=[_fake_row(GLOBALEVENTID=999, SQLDATE=20180301)])

    n = gdelt_events.ingest_gdelt_events(conn, date(2018, 3, 1), date(2018, 3, 31), client=client)
    assert n == 1

    row = conn.execute(
        "SELECT gdelt_event_id, event_datetime, ingested_at FROM event_news_gdelt"
    ).fetchone()
    assert row[0] == "999"
    assert row[2] == datetime(2018, 3, 1, 23, 59, 59, tzinfo=timezone.utc)

    audit = conn.execute(
        "SELECT status, source, target_table, rows_written FROM ingestion_runs"
    ).fetchone()
    assert audit == ("success", "gdelt", "event_news_gdelt", 1)


def test_missing_project_id_raises(monkeypatch):
    monkeypatch.delenv("GDELT_BQ_PROJECT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setattr(gdelt_events, "load_dotenv", lambda: None)
    with pytest.raises(RuntimeError, match="GDELT_BQ_PROJECT_ID"):
        gdelt_events._bq_client()


# --- daily aggregate (bulk-history backfill) path --------------------------

def _fake_agg_row(**overrides):
    row = {
        "SQLDATE": 20180301,
        "avg_tone": -1.5,
        "avg_goldstein": -2.0,
        "total_mentions": 42,
        "total_sources": 8,
        "total_articles": 15,
    }
    row.update(overrides)
    return row


def test_fetch_gdelt_daily_aggregates_shapes_one_synthetic_row_per_day():
    client = _FakeClient(dry_run_bytes=1_000_000, rows=[_fake_agg_row()])
    df = gdelt_events.fetch_gdelt_daily_aggregates(date(2018, 3, 1), date(2018, 3, 31), client=client)

    assert df.height == 1
    row = df.row(0, named=True)
    assert row["gdelt_event_id"] == "daily-agg-20180301"
    assert row["event_class"] is None
    assert row["geo_lat"] is None
    assert row["tone_score"] == -1.5
    assert row["goldstein_scale"] == -2.0
    assert row["num_mentions"] == 42
    assert row["num_sources"] == 8
    assert row["num_articles"] == 15
    assert row["source"] == "gdelt"
    assert row["ingested_at"] == datetime(2018, 3, 1, 23, 59, 59, tzinfo=timezone.utc)


def test_fetch_gdelt_daily_aggregates_raises_when_dry_run_exceeds_threshold():
    client = _FakeClient(dry_run_bytes=100 * 1024**3, rows=[_fake_agg_row()])  # 100 GB
    with pytest.raises(RuntimeError, match="exceeding"):
        gdelt_events.fetch_gdelt_daily_aggregates(
            date(2015, 1, 1), date(2026, 7, 10), client=client, max_bytes_billed=60 * 1024**3
        )
    assert len(client.queries) == 1  # never ran the real (billed) query


def test_ingest_gdelt_daily_aggregates_end_to_end():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    client = _FakeClient(dry_run_bytes=0, rows=[
        _fake_agg_row(SQLDATE=20180301),
        _fake_agg_row(SQLDATE=20180302, avg_tone=0.0),
    ])

    n = gdelt_events.ingest_gdelt_daily_aggregates(conn, date(2018, 3, 1), date(2018, 3, 31), client=client)
    assert n == 2

    rows = conn.execute("SELECT gdelt_event_id, tone_score FROM event_news_gdelt ORDER BY gdelt_event_id").fetchall()
    assert rows == [("daily-agg-20180301", -1.5), ("daily-agg-20180302", 0.0)]
