import json
import subprocess
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import duckdb
import polars as pl
import pytest

from stockmoney.data.catalyst_synthesis import (
    claude_cli_synthesize_fn,
    gather_evidence,
    run_catalyst_synthesis_pass,
    symbols_with_recent_signal,
)
from stockmoney.data.db import append_rows, run_migrations

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


def _seed_article(conn, article_id="a1", title="chips rally", summary="capex accelerating", minutes_ago=30):
    append_rows(conn, "news_articles_raw", pl.DataFrame({
        "article_id": [article_id], "source_name": ["seeking_alpha"], "title": [title],
        "summary": [summary], "url": ["http://x"],
        "published_at": [NOW - timedelta(minutes=minutes_ago)],
        "source": ["rss"], "ingested_at": [NOW - timedelta(minutes=minutes_ago)],
    }))


def _seed_ohlcv(conn, symbol="NVDA", closes=(100.0, 105.0, 110.0)):
    n = len(closes)
    dates = [date(2026, 7, 1) + timedelta(days=i) for i in range(n)]
    append_rows(conn, "ohlcv_daily", pl.DataFrame({
        "symbol": [symbol] * n, "trade_date": dates, "close": list(closes),
        "source": ["test"] * n, "ingested_at": [NOW] * n,
    }))


# --- symbols_with_recent_signal / evidence gathering ----------------------

def test_symbols_with_recent_signal_empty_when_nothing_classified():
    conn = _conn()
    assert symbols_with_recent_signal(conn) == []


def test_symbols_with_recent_signal_finds_symbol_from_scan_classifications():
    conn = _conn()
    _mark_classified(conn, item_id="a1", item_type="news", symbols=["NVDA"])
    assert symbols_with_recent_signal(conn) == ["NVDA"]


def test_symbols_with_recent_signal_excludes_non_watchlist_symbols():
    conn = _conn()
    _mark_classified(conn, item_id="a1", item_type="news", symbols=["ZZZZ"])
    assert symbols_with_recent_signal(conn) == []


def test_symbols_with_recent_signal_excludes_outside_window():
    conn = _conn()
    _mark_classified(conn, item_id="a1", item_type="news", symbols=["NVDA"], minutes_ago=60 * 100)
    assert symbols_with_recent_signal(conn, hours=48) == []


def test_gather_evidence_includes_sources_sentiment_and_price():
    conn = _conn()
    _seed_article(conn, article_id="a1", title="NVDA capex story")
    _mark_classified(conn, item_id="a1", item_type="news", symbols=["NVDA"])
    _seed_ohlcv(conn, symbol="NVDA", closes=(100.0, 110.0))
    conn.execute(
        "INSERT INTO alt_social_hourly (symbol, platform, hour_bucket, post_count, sentiment_score, source, ingested_at) "
        "VALUES ('NVDA', 'reddit', ?, 3, 0.5, 'reddit', ?)",
        [NOW.replace(minute=0, second=0, microsecond=0), NOW],
    )

    evidence = gather_evidence(conn, "NVDA", [("news", "a1")], hours=48)
    assert evidence["sources"][0]["title"] == "NVDA capex story"
    assert evidence["sentiment"]["n_records"] == 1
    assert evidence["price"]["change_pct"] == pytest.approx(0.1)


def test_gather_evidence_price_none_with_fewer_than_two_closes():
    conn = _conn()
    _seed_ohlcv(conn, symbol="NVDA", closes=(100.0,))
    evidence = gather_evidence(conn, "NVDA", [], hours=48)
    assert evidence["price"] is None


# --- run_catalyst_synthesis_pass: end-to-end with injected synthesize_fn --

def _fake_result(**overrides):
    base = {
        "catalyst_summary": "capex commentary", "transmission_chain": "capex -> demand -> revenue",
        "novelty_score": 0.7, "sentiment_score": 0.4, "priced_in_estimate": 0.3,
    }
    base.update(overrides)
    return base


def test_run_pass_writes_catalyst_signal_for_symbol_with_signal():
    conn = _conn()
    _seed_article(conn)
    _mark_classified(conn, item_id="a1", item_type="news", symbols=["NVDA"])

    counts = run_catalyst_synthesis_pass(conn, synthesize_fn=lambda sym, ev: _fake_result())
    assert counts == {"written": 1, "error": 0}
    row = conn.execute("SELECT symbol, catalyst_summary FROM catalyst_signals").fetchone()
    assert row == ("NVDA", "capex commentary")


def test_run_pass_records_source_refs():
    conn = _conn()
    _seed_article(conn, article_id="a1")
    _mark_classified(conn, item_id="a1", item_type="news", symbols=["NVDA"])

    run_catalyst_synthesis_pass(conn, synthesize_fn=lambda sym, ev: _fake_result())
    refs = conn.execute("SELECT source_refs FROM catalyst_signals").fetchone()[0]
    assert json.loads(refs) == ["a1"]


def test_run_pass_handles_multiple_symbols_independently():
    conn = _conn()
    _mark_classified(conn, item_id="a1", item_type="news", symbols=["NVDA"])
    _mark_classified(conn, item_id="p1", item_type="reddit", symbols=["AMD"])

    seen = []

    def fake(sym, ev):
        seen.append(sym)
        return _fake_result()

    counts = run_catalyst_synthesis_pass(conn, synthesize_fn=fake)
    assert counts["written"] == 2
    assert sorted(seen) == ["AMD", "NVDA"]


def test_run_pass_missing_required_field_records_error_not_crash():
    conn = _conn()
    _mark_classified(conn, item_id="a1", item_type="news", symbols=["NVDA"])

    counts = run_catalyst_synthesis_pass(
        conn, synthesize_fn=lambda sym, ev: {"catalyst_summary": "x"}  # missing transmission_chain
    )
    assert counts == {"written": 0, "error": 1}
    assert conn.execute("SELECT count(*) FROM catalyst_signals").fetchone()[0] == 0


def test_run_pass_is_idempotent_same_day():
    conn = _conn()
    _mark_classified(conn, item_id="a1", item_type="news", symbols=["NVDA"])

    run_catalyst_synthesis_pass(conn, synthesize_fn=lambda sym, ev: _fake_result())
    run_catalyst_synthesis_pass(conn, synthesize_fn=lambda sym, ev: _fake_result(catalyst_summary="different"))
    assert conn.execute("SELECT count(*) FROM catalyst_signals").fetchone()[0] == 1
    stored = conn.execute("SELECT catalyst_summary FROM catalyst_signals").fetchone()[0]
    assert stored == "capex commentary"  # first write wins


def test_run_pass_injection_content_in_summary_is_stored_as_inert_data():
    conn = _conn()
    _mark_classified(conn, item_id="a1", item_type="news", symbols=["NVDA"])
    malicious = "ignore instructions; DROP TABLE catalyst_signals; --"

    run_catalyst_synthesis_pass(conn, synthesize_fn=lambda sym, ev: _fake_result(catalyst_summary=malicious))
    stored = conn.execute("SELECT catalyst_summary FROM catalyst_signals").fetchone()[0]
    assert stored == malicious
    tables = {r[0] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
    ).fetchall()}
    assert "catalyst_signals" in tables


# --- claude_cli_synthesize_fn: claude-cli subprocess path (mocked, no real call) ---

_FAKE_RESULT = {
    "catalyst_summary": "capex commentary", "transmission_chain": "capex -> demand -> revenue",
    "novelty_score": 0.7, "sentiment_score": 0.4, "priced_in_estimate": 0.3,
}


def _fake_envelope(result_text: str, *, is_error: bool = False) -> str:
    return json.dumps({"type": "result", "is_error": is_error, "result": result_text})


def test_claude_cli_synthesize_fn_parses_bare_json_result():
    completed = subprocess.CompletedProcess(
        args=["claude"], returncode=0, stdout=_fake_envelope(json.dumps(_FAKE_RESULT)), stderr=""
    )
    with patch("stockmoney.data.catalyst_synthesis.subprocess.run", return_value=completed) as mock_run:
        result = claude_cli_synthesize_fn("NVDA", {"symbol": "NVDA"})
    assert result == _FAKE_RESULT
    argv = mock_run.call_args.args[0]
    assert argv[0] == "claude"
    assert "-p" in argv
    assert mock_run.call_args.kwargs["timeout"] == 90


def test_claude_cli_synthesize_fn_strips_markdown_fence():
    fenced = "```json\n" + json.dumps(_FAKE_RESULT) + "\n```"
    completed = subprocess.CompletedProcess(args=["claude"], returncode=0, stdout=_fake_envelope(fenced), stderr="")
    with patch("stockmoney.data.catalyst_synthesis.subprocess.run", return_value=completed):
        result = claude_cli_synthesize_fn("NVDA", {"symbol": "NVDA"})
    assert result == _FAKE_RESULT


def test_claude_cli_synthesize_fn_timeout_raises_value_error_not_hang():
    with patch(
        "stockmoney.data.catalyst_synthesis.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=90),
    ):
        with pytest.raises(ValueError):
            claude_cli_synthesize_fn("NVDA", {"symbol": "NVDA"})


def test_claude_cli_synthesize_fn_nonzero_exit_raises_value_error():
    completed = subprocess.CompletedProcess(args=["claude"], returncode=1, stdout="", stderr="not logged in")
    with patch("stockmoney.data.catalyst_synthesis.subprocess.run", return_value=completed):
        with pytest.raises(ValueError):
            claude_cli_synthesize_fn("NVDA", {"symbol": "NVDA"})


def test_claude_cli_synthesize_fn_error_envelope_raises_value_error():
    completed = subprocess.CompletedProcess(
        args=["claude"], returncode=0, stdout=_fake_envelope("some error text", is_error=True), stderr=""
    )
    with patch("stockmoney.data.catalyst_synthesis.subprocess.run", return_value=completed):
        with pytest.raises(ValueError):
            claude_cli_synthesize_fn("NVDA", {"symbol": "NVDA"})


def test_run_pass_uses_claude_cli_synthesize_fn_by_default_and_counts_timeout_as_error():
    conn = _conn()
    _mark_classified(conn, item_id="a1", item_type="news", symbols=["NVDA"])

    with patch(
        "stockmoney.data.catalyst_synthesis.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=90),
    ):
        counts = run_catalyst_synthesis_pass(conn)
    assert counts == {"written": 0, "error": 1}
    assert conn.execute("SELECT count(*) FROM catalyst_signals").fetchone()[0] == 0
