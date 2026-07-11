"""Read-only FastAPI backend for the React frontend
(~/.claude/plans/frontend-catalyst-rebuild.md Track B / B1). Replaces
scripts/dashboard.py's role as the data source; scripts/dashboard.py itself
stays running until the React frontend reaches parity (Phase 3 retires it).

CLAUDE.md section 0's hard boundary applies here too: every route is GET-only
and read-only. This process never places, modifies, or cancels an order, and
never writes to the database -- it only reads what
scripts/build_dashboard_snapshot.py / scripts/nightly_refresh.py /
scripts/classify_scan.py already computed and wrote.

Usage (dev):
    uv run uvicorn stockmoney.api.main:app --reload --port 8000
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from stockmoney.api import queries
from stockmoney.api.db import ro_connection

app = FastAPI(title="stockmoney API", description="Read-only decision-support data. Never places orders.")

# Dev-only: the Vite frontend runs on a different port during development.
# Tighten this (or drop it entirely behind a same-origin build) before this
# is ever exposed beyond localhost.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/pipeline-health")
def pipeline_health() -> list[dict]:
    with ro_connection() as conn:
        return queries.pipeline_health(conn)


@app.get("/api/watchlist")
def watchlist() -> dict:
    with ro_connection() as conn:
        return {
            "core": queries.watchlist_core(conn),
            "candidates": queries.watchlist_candidates(conn),
        }


@app.get("/api/opportunities")
def opportunities() -> list[dict]:
    with ro_connection() as conn:
        return queries.opportunities(conn)


@app.get("/api/ticker/{symbol}")
def ticker_detail(symbol: str) -> dict:
    with ro_connection() as conn:
        result = queries.ticker_detail(conn, symbol)
    if result is None:
        raise HTTPException(status_code=404, detail=f"no prediction data for {symbol.upper()}")
    return result


@app.get("/api/predictions")
def predictions() -> dict:
    with ro_connection() as conn:
        return queries.predictions_overview(conn)


@app.get("/api/positions")
def positions() -> list[dict]:
    with ro_connection() as conn:
        return queries.positions_with_risk(conn)


@app.get("/api/catalysts")
def catalysts() -> dict:
    with ro_connection() as conn:
        return queries.catalysts(conn)
