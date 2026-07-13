"""End-to-end route tests against a real temp DB file (FastAPI's route
handlers open their own connection via stockmoney.api.db.ro_connection,
which reads DEFAULT_DB_PATH -- so these tests point that at a throwaway
migrated DB file rather than mocking the connection)."""
from __future__ import annotations

from datetime import date

import duckdb
import pytest
from fastapi.testclient import TestClient

from stockmoney.data.daily_predictions import record_prediction
from stockmoney.data.db import run_migrations


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.duckdb")
    conn = duckdb.connect(db_path)
    run_migrations(conn)
    record_prediction(
        conn, trade_date=date(2026, 7, 9), symbol="NVDA", sector="semiconductor", horizon=5,
        label_end_date=date(2026, 7, 16), regime=1, proba=(0.2, 0.3, 0.5),
        entry_price=180.0, feature_values={"realized_vol_20d": 0.3, "adx_14": 20.0, "xsec_dispersion": 0.01,
                                            "yield_curve_10y2y": 0.5, "dxy_chg_1d": 0.0, "oil_chg_1d": 0.0},
        model_version="test-v1",
    )
    conn.close()

    monkeypatch.setattr("stockmoney.api.db.DEFAULT_DB_PATH", db_path)
    from stockmoney.api.main import app

    return TestClient(app)


def test_health():
    from stockmoney.api.main import app
    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_watchlist_route(client):
    resp = client.get("/api/watchlist")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["core"]) == 31
    assert body["candidates"] == []


def test_opportunities_route(client):
    resp = client.get("/api/opportunities")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["symbol"] == "NVDA"


def test_ticker_detail_route_found(client):
    resp = client.get("/api/ticker/NVDA")
    assert resp.status_code == 200
    assert resp.json()["symbol"] == "NVDA"


def test_ticker_detail_route_not_found(client):
    resp = client.get("/api/ticker/ZZZZ")
    assert resp.status_code == 404


def test_predictions_route(client):
    resp = client.get("/api/predictions")
    assert resp.status_code == 200
    assert "rolling_win_rate" in resp.json()


def test_positions_route_empty(client):
    resp = client.get("/api/positions")
    assert resp.status_code == 200
    assert resp.json() == []


def test_catalysts_route(client):
    resp = client.get("/api/catalysts")
    assert resp.status_code == 200
    assert resp.json()["available"] is True
    assert resp.json()["items"] == []


def test_pipeline_health_route(client):
    resp = client.get("/api/pipeline-health")
    assert resp.status_code == 200
    assert resp.json() == []
