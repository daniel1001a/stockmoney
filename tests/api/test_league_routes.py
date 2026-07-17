"""Route tests for the Trader League read-only endpoints (Worker 1's contract
for the Worker-2 frontend). Same throwaway-DB pattern as test_main.py."""
from __future__ import annotations

from datetime import date

import duckdb
import pytest
from fastapi.testclient import TestClient

from stockmoney.data import trader_predictions as tp
from stockmoney.data.db import run_migrations


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.duckdb")
    conn = duckdb.connect(db_path)
    run_migrations(conn)
    # two traders disagree on SOXL; chartist graded a win
    chart = tp.record_trader_prediction(
        conn, trader_id="chartist", method_version="chartist:gmm-logistic-v2",
        trade_date=date(2026, 6, 1), symbol="SOXL", sector="semiconductor", horizon=5,
        label_end_date=date(2026, 6, 8), direction="up", conviction=0.7, rationale="r",
        invalidation="i", entry_price=100.0, grade_vol=0.4, engine_payload={}, regime=0,
    )
    tp.record_trader_prediction(
        conn, trader_id="analyst", method_version="analyst:catalyst-v1",
        trade_date=date(2026, 6, 1), symbol="SOXL", sector="semiconductor", horizon=5,
        label_end_date=date(2026, 6, 8), direction="down", conviction=0.6, rationale="r",
        invalidation="i", entry_price=100.0, grade_vol=0.4, engine_payload={}, regime=0,
    )
    tp.grade_trader_prediction(conn, chart, actual_price=130.0)
    conn.close()

    monkeypatch.setattr("stockmoney.api.db.DEFAULT_DB_PATH", db_path)
    from stockmoney.api.main import app
    return TestClient(app)


def test_league_route(client):
    resp = client.get("/api/league")
    assert resp.status_code == 200
    rows = {r["trader_id"]: r for r in resp.json()}
    assert rows["chartist"]["overall"]["hit_rate"] == 1.0
    assert rows["analyst"]["overall"]["n_graded"] == 0  # honest empty


def test_league_equity_route(client):
    resp = client.get("/api/league/equity")
    assert resp.status_code == 200
    rows = {r["trader_id"]: r for r in resp.json()}
    chart_points = rows["chartist"]["points"]
    assert len(chart_points) == 1
    assert chart_points[0]["symbol"] == "SOXL"
    assert chart_points[0]["cum_pnl"] == pytest.approx(0.30)
    assert rows["analyst"]["points"] == []  # ungraded -> honest empty, not omitted


def test_traders_route(client):
    resp = client.get("/api/traders")
    assert resp.status_code == 200
    assert {t["trader_id"] for t in resp.json()} == {
        "chartist", "analyst", "reversion", "flow", "sentiment",
    }


def test_ticker_traders_route(client):
    resp = client.get("/api/ticker/SOXL/traders")
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "SOXL"
    assert body["consensus"]["agree"] is False           # up vs down
    assert sorted(body["consensus"]["directions"]) == ["down", "up"]
    assert body["consensus"]["n_traders"] == 2


def test_ticker_traders_route_404_for_uncovered(client):
    assert client.get("/api/ticker/ZZZZ/traders").status_code == 404


def test_divergence_route(client):
    # divergence is populated by the review engine; without it the endpoint
    # still returns a valid (empty) list, not an error.
    resp = client.get("/api/divergence")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_trader_trades_route_empty(client):
    # trader_trades hasn't been populated in this throwaway DB -- must degrade
    # to an empty list, not error.
    resp = client.get("/api/trader-trades")
    assert resp.status_code == 200
    assert resp.json() == []


def test_trader_trades_route(client):
    from datetime import datetime, timezone

    import stockmoney.api.db as api_db

    # Re-open the same on-disk DB the fixture wrote to via the monkeypatched path.
    conn = duckdb.connect(api_db.DEFAULT_DB_PATH)
    now = datetime.now(timezone.utc)
    conn.execute(
        """INSERT INTO trader_trades
        (trade_id, trader_id, symbol, option_right, side, strike, expiry_date, contracts,
         entry_at, entry_underlying, entry_premium, exit_at, exit_underlying, exit_premium,
         realized_pnl, status, thesis, exit_reason, linked_prediction_id, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            "trade-1", "chartist", "SOXL", "call", "long", 45.0, "2026-07-10", 1,
            now, 44.0, 2.1, None, None, None, None, "open", "趨勢延續", None, None, now,
        ],
    )
    conn.close()

    resp = client.get("/api/trader-trades?limit=10")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["trade_id"] == "trade-1"
    assert row["trader_id"] == "chartist"
    assert row["trader_name"]  # joined from traders table
    assert row["symbol"] == "SOXL"
    assert row["status"] == "open"
    assert row["thesis"] == "趨勢延續"
