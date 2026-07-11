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


def test_traders_route(client):
    resp = client.get("/api/traders")
    assert resp.status_code == 200
    assert {t["trader_id"] for t in resp.json()} == {"chartist", "analyst"}


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
