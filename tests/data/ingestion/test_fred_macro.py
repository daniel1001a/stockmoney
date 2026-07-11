from datetime import date

import duckdb
import pytest

from stockmoney.data.db import run_migrations
from stockmoney.data.ingestion import fred_macro


class _FakeResponse:
    def __init__(self, observations):
        self._observations = observations

    def raise_for_status(self):
        pass

    def json(self):
        return {"observations": self._observations}


def _fake_requests_get(series_payloads):
    def _get(url, params, timeout):
        sid = params["series_id"]
        return _FakeResponse(series_payloads[sid])
    return _get


def test_fetch_macro_series_parses_and_skips_missing(monkeypatch):
    monkeypatch.setattr(fred_macro, "_fred_api_key", lambda: "fake-key")
    payloads = {
        "DGS10": [
            {"date": "2026-07-01", "value": "4.48"},
            {"date": "2026-07-02", "value": "."},   # FRED missing-value marker
            {"date": "2026-07-03", "value": "4.55"},
        ],
    }
    monkeypatch.setattr(fred_macro.requests, "get", _fake_requests_get(payloads))

    df = fred_macro.fetch_macro_series(
        ["DGS10"], date(2026, 7, 1), date(2026, 7, 3), vintage_date=date(2026, 7, 9)
    )

    assert df.height == 2  # missing row skipped
    assert df["series_id"].to_list() == ["DGS10", "DGS10"]
    assert df["value"].to_list() == [4.48, 4.55]
    assert df["vintage_date"].unique().to_list() == [date(2026, 7, 9)]
    assert df["source"].unique().to_list() == ["fred"]


def test_fetch_macro_series_all_missing_returns_empty(monkeypatch):
    monkeypatch.setattr(fred_macro, "_fred_api_key", lambda: "fake-key")
    payloads = {"DGS10": [{"date": "2026-07-02", "value": "."}]}
    monkeypatch.setattr(fred_macro.requests, "get", _fake_requests_get(payloads))

    df = fred_macro.fetch_macro_series(["DGS10"], date(2026, 7, 1), date(2026, 7, 3))
    assert df.height == 0
    assert df.columns == ["series_id", "observation_date", "value", "vintage_date", "source"]


def test_ingest_macro_series_end_to_end_and_audit(monkeypatch):
    monkeypatch.setattr(fred_macro, "_fred_api_key", lambda: "fake-key")
    payloads = {
        "DGS10": [{"date": "2026-07-01", "value": "4.48"}],
        "DGS2": [{"date": "2026-07-01", "value": "4.10"}],
    }
    monkeypatch.setattr(fred_macro.requests, "get", _fake_requests_get(payloads))

    conn = duckdb.connect(":memory:")
    run_migrations(conn)

    n = fred_macro.ingest_macro_series(
        conn, date(2026, 7, 1), date(2026, 7, 1), series_ids=["DGS10", "DGS2"]
    )
    assert n == 2
    assert conn.execute("SELECT count(*) FROM macro_series_daily").fetchone()[0] == 2

    run_row = conn.execute(
        "SELECT status, source, target_table, rows_written FROM ingestion_runs"
    ).fetchone()
    assert run_row == ("success", "fred", "macro_series_daily", 2)


def test_ingest_macro_series_same_day_rerun_is_idempotent(monkeypatch):
    """macro_series_daily's PK is (series_id, observation_date, vintage_date)
    -- daily-granularity, not ingested_at-versioned like the other raw
    tables -- so re-running the ingest twice on the same day (e.g. a manual
    run plus that day's cron, or a cron retry) must be a safe no-op on the
    second call, not a PRIMARY KEY constraint crash."""
    monkeypatch.setattr(fred_macro, "_fred_api_key", lambda: "fake-key")
    payloads = {
        "DGS10": [{"date": "2026-07-01", "value": "4.48"}],
        "DGS2": [{"date": "2026-07-01", "value": "4.10"}],
    }
    monkeypatch.setattr(fred_macro.requests, "get", _fake_requests_get(payloads))

    conn = duckdb.connect(":memory:")
    run_migrations(conn)

    first = fred_macro.ingest_macro_series(
        conn, date(2026, 7, 1), date(2026, 7, 1), series_ids=["DGS10", "DGS2"]
    )
    second = fred_macro.ingest_macro_series(  # must not raise
        conn, date(2026, 7, 1), date(2026, 7, 1), series_ids=["DGS10", "DGS2"]
    )

    assert first == 2
    assert second == 0
    assert conn.execute("SELECT count(*) FROM macro_series_daily").fetchone()[0] == 2

    statuses = conn.execute(
        "SELECT status, rows_written FROM ingestion_runs ORDER BY started_at"
    ).fetchall()
    assert statuses == [("success", 2), ("success", 0)]


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    # Ensure load_dotenv doesn't repopulate it from a real .env during the test.
    monkeypatch.setattr(fred_macro, "load_dotenv", lambda: None)
    with pytest.raises(RuntimeError, match="FRED_API_KEY"):
        fred_macro.fetch_macro_series(["DGS10"], date(2026, 7, 1), date(2026, 7, 3))
