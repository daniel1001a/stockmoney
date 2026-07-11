from datetime import date, datetime, timedelta, timezone

import duckdb
import pytest

from stockmoney.data.catalyst_signals import (
    get_latest_catalyst_for_symbol,
    list_recent_catalysts,
    record_catalyst_signal,
)
from stockmoney.data.db import run_migrations


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _record(conn, **overrides):
    kwargs = dict(
        symbol="NVDA", as_of_date=date(2026, 7, 10),
        catalyst_summary="Datacenter capex commentary accelerating",
        transmission_chain="hyperscaler capex guidance -> GPU demand -> NVDA revenue",
        novelty_score=0.8, sentiment_score=0.6, priced_in_estimate=0.2,
        source_refs=["a1", "p1"], model_version="catalyst-sonnet-v1",
    )
    kwargs.update(overrides)
    return record_catalyst_signal(conn, **kwargs)


def test_record_catalyst_signal_writes_row():
    conn = _conn()
    signal_id = _record(conn)
    row = conn.execute(
        "SELECT symbol, catalyst_summary, novelty_score FROM catalyst_signals WHERE signal_id = ?",
        [signal_id],
    ).fetchone()
    assert row == ("NVDA", "Datacenter capex commentary accelerating", 0.8)


def test_record_catalyst_signal_rejects_unknown_symbol():
    conn = _conn()
    with pytest.raises(ValueError, match="not an active watchlist member"):
        _record(conn, symbol="ZZZZ")
    assert conn.execute("SELECT count(*) FROM catalyst_signals").fetchone()[0] == 0


def test_record_catalyst_signal_idempotent_per_symbol_and_date():
    conn = _conn()
    id1 = _record(conn)
    id2 = _record(conn, catalyst_summary="a different summary")
    assert id1 == id2
    assert conn.execute("SELECT count(*) FROM catalyst_signals").fetchone()[0] == 1
    # the second call's differing content was NOT written -- first write wins.
    stored = conn.execute("SELECT catalyst_summary FROM catalyst_signals WHERE signal_id = ?", [id1]).fetchone()[0]
    assert stored == "Datacenter capex commentary accelerating"


def test_record_catalyst_signal_different_dates_are_separate_rows():
    conn = _conn()
    _record(conn, as_of_date=date(2026, 7, 9))
    _record(conn, as_of_date=date(2026, 7, 10))
    assert conn.execute("SELECT count(*) FROM catalyst_signals").fetchone()[0] == 2


def test_record_catalyst_signal_source_refs_stored_as_json():
    conn = _conn()
    signal_id = _record(conn, source_refs=["x", "y", "z"])
    stored = conn.execute("SELECT source_refs FROM catalyst_signals WHERE signal_id = ?", [signal_id]).fetchone()[0]
    assert stored == '["x", "y", "z"]'


def test_get_latest_catalyst_for_symbol_none_when_absent():
    conn = _conn()
    assert get_latest_catalyst_for_symbol(conn, "NVDA") is None


def test_get_latest_catalyst_for_symbol_returns_most_recent():
    conn = _conn()
    _record(conn, as_of_date=date(2026, 7, 8), catalyst_summary="old")
    _record(conn, as_of_date=date(2026, 7, 10), catalyst_summary="new")
    latest = get_latest_catalyst_for_symbol(conn, "nvda")
    assert latest.catalyst_summary == "new"


def test_list_recent_catalysts_excludes_outside_window():
    conn = _conn()
    signal_id = _record(conn)
    # backdate available_at outside the 48h window
    conn.execute(
        "UPDATE catalyst_signals SET available_at = ? WHERE signal_id = ?",
        [datetime.now(timezone.utc) - timedelta(hours=100), signal_id],
    )
    assert list_recent_catalysts(conn, hours=48) == []


def test_list_recent_catalysts_sorted_by_novelty_times_abs_sentiment():
    conn = _conn()
    _record(conn, symbol="NVDA", novelty_score=0.9, sentiment_score=0.5)  # score 0.45
    _record(conn, symbol="AMD", novelty_score=0.9, sentiment_score=-0.9)  # score 0.81
    results = list_recent_catalysts(conn, hours=48)
    assert [r.symbol for r in results] == ["AMD", "NVDA"]


def test_list_recent_catalysts_handles_null_scores_without_crashing():
    conn = _conn()
    _record(conn, novelty_score=None, sentiment_score=None)
    results = list_recent_catalysts(conn, hours=48)
    assert len(results) == 1
