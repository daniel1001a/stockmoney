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

from stockmoney.api import cockpit, queries
from stockmoney.api.db import ro_connection
from stockmoney.api.live_quotes import get_quotes

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


@app.get("/api/quotes")
def quotes() -> dict[str, dict]:
    """Live (yfinance free-tier, ~15min-delayed) price overlay for the whole
    watchlist -- a pure price read, never fed into any model/prediction (see
    live_quotes.py module docstrings). Server-side cached so N open tabs
    polling this on refreshCadence.py's cadence share one upstream fetch."""
    with ro_connection() as conn:
        symbols = [row["symbol"] for row in queries.watchlist_core(conn)]
    return get_quotes(symbols)


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


@app.get("/api/market-summary")
def market_summary() -> dict:
    with ro_connection() as conn:
        return queries.market_summary(conn)


@app.get("/api/news")
def news(limit: int = 60) -> list[dict]:
    with ro_connection() as conn:
        return queries.news_feed(conn, limit=limit)


@app.get("/api/news/{item_id}")
def news_detail(item_id: str) -> dict:
    with ro_connection() as conn:
        result = queries.news_item(conn, item_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"no news item {item_id}")
    return result


@app.get("/api/events")
def events() -> list[dict]:
    with ro_connection() as conn:
        return queries.events(conn)


@app.get("/api/leaderboard")
def leaderboard(window: int = 20) -> list[dict]:
    with ro_connection() as conn:
        return queries.leaderboard(conn, window=window)


@app.get("/api/traders/{trader_id}")
def trader_profile(trader_id: str) -> dict:
    with ro_connection() as conn:
        result = queries.trader_profile(conn, trader_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"no trader {trader_id}")
    return result


# --- Trader League Arena (Worker 1) -----------------------------------------

@app.get("/api/league")
def league(window: int = 20, cost_bps: float = 0.0) -> list[dict]:
    with ro_connection() as conn:
        return queries.league_table(conn, window=window, cost_bps=cost_bps)


@app.get("/api/traders")
def traders() -> list[dict]:
    with ro_connection() as conn:
        return queries.traders(conn)


@app.get("/api/ticker/{symbol}/traders")
def ticker_traders(symbol: str) -> dict:
    with ro_connection() as conn:
        result = queries.latest_trader_predictions(conn, symbol)
    if result is None:
        raise HTTPException(status_code=404, detail=f"no trader predictions for {symbol.upper()}")
    return result


@app.get("/api/divergence")
def divergence(hours: int = 168) -> list[dict]:
    with ro_connection() as conn:
        return queries.recent_divergence(conn, hours=hours)


# --- Honest Lin cockpit (Worker 5) -------------------------------------------
# No direction prediction anywhere below this line -- see cockpit.py's module
# docstring for the 8y walk-forward verdict that motivated this shape
# (CLAUDE.md section 0 / REBUILD_PLAN.md Phase 0-1c).

@app.get("/api/briefing")
def briefing() -> dict:
    with ro_connection() as conn:
        return cockpit.build_briefing(conn)


@app.get("/api/cockpit")
def cockpit_route() -> list[dict]:
    with ro_connection() as conn:
        return cockpit.build_cockpit(conn)


@app.get("/api/scoreboard-summary")
def scoreboard_summary() -> dict:
    return cockpit.scoreboard_summary()
