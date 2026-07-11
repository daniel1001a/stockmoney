import io
import json
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from stockmoney.data.db import run_migrations
import record_catalyst_signal

VALID_PAYLOAD = {
    "symbol": "NVDA",
    "catalyst_summary": "New export rule eases chip sales to allied nations.",
    "transmission_chain": "Rule change -> higher addressable market -> more NVDA unit sales.",
    "novelty_score": 0.7,
    "sentiment_score": 0.4,
    "priced_in_estimate": 0.3,
    "source_refs": ["a1"],
}


def _conn(db_path=":memory:"):
    conn = duckdb.connect(db_path)
    run_migrations(conn)
    return conn


def _run(monkeypatch, conn, payload, capsys):
    monkeypatch.setattr(record_catalyst_signal, "get_connection", lambda db_path: conn)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    exit_code = record_catalyst_signal.main(db_path="unused")
    return exit_code, json.loads(capsys.readouterr().out)


def test_records_a_valid_catalyst_signal(tmp_path, monkeypatch, capsys):
    # record_catalyst_signal.main() closes its connection in a `finally`
    # (matching record_classification.py), so verifying persisted state needs
    # a fresh connection to a real file afterward, not the same in-memory db.
    db_path = str(tmp_path / "test.duckdb")
    conn = _conn(db_path)
    exit_code, out = _run(monkeypatch, conn, VALID_PAYLOAD, capsys)

    assert exit_code == 0
    assert out["ok"] is True
    assert out["signal_id"]

    verify_conn = duckdb.connect(db_path)
    row = verify_conn.execute(
        "SELECT symbol, catalyst_summary, model_version FROM catalyst_signals"
    ).fetchone()
    verify_conn.close()
    assert row == (
        "NVDA",
        VALID_PAYLOAD["catalyst_summary"],
        record_catalyst_signal.MODEL_VERSION,
    )


def test_rejects_symbol_not_on_the_watchlist(monkeypatch, capsys):
    conn = _conn()
    payload = {**VALID_PAYLOAD, "symbol": "ZZZZ"}
    exit_code, out = _run(monkeypatch, conn, payload, capsys)

    assert exit_code == 1
    assert out["ok"] is False
    assert "ZZZZ" in out["error"]


def test_rejects_missing_required_fields(monkeypatch, capsys):
    conn = _conn()
    payload = {"symbol": "NVDA"}
    exit_code, out = _run(monkeypatch, conn, payload, capsys)

    assert exit_code == 1
    assert out["ok"] is False
    assert "catalyst_summary" in out["error"]


def test_rejects_invalid_json(monkeypatch, capsys):
    conn = _conn()
    monkeypatch.setattr(record_catalyst_signal, "get_connection", lambda db_path: conn)
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))

    exit_code = record_catalyst_signal.main(db_path="unused")
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert out["ok"] is False
