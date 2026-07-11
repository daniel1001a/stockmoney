"""Read-only query functions backing the FastAPI routes in `main.py`.

Split out from `main.py` so these are unit-testable against a plain DuckDB
connection without spinning up an HTTP app (same reasoning as
scripts/dashboard.py's small `_*` query functions, just promoted to a
reusable module now that two front ends -- Streamlit and this API -- need
the same data).

Every function here is read-only and takes an already-open connection; it
never opens/closes one itself (see `db.ro_connection` for that, used by the
route handlers) and never fits/backtests a model live -- `opportunities`/
`ticker_detail`/`positions_with_risk` all read from the nightly-computed
`daily_predictions` / `symbol_backtest_snapshot` cache (see
scripts/build_dashboard_snapshot.py), not from stockmoney.models.production
directly, so a request is always just a DuckDB read.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import duckdb

from stockmoney.data.catalyst_signals import get_latest_catalyst_for_symbol, list_recent_catalysts
from stockmoney.data.daily_predictions import win_rate_history
from stockmoney.data.positions import list_open_positions
from stockmoney.data.trader_predictions import predictions_on_date
from stockmoney.data.trader_review import recent_divergence_rows
from stockmoney.data.traders import list_all_traders
from stockmoney.league.league_table import league_table as _compute_league_table
from stockmoney.models.options_risk import MarketSnapshot, assess_position

ROLLING_WIN_RATE_WINDOW = 20
RECENT_PREDICTIONS_LIMIT = 15
PRICE_HISTORY_DAYS = 90

# Per-table max acceptable lag before pipeline_health flags it stale, in
# calendar days. Grounded in what actually runs on a recurring schedule
# (nightly_refresh.py + the OpenClaw stockmoney-scan-ingest cron) -- a table
# with no recurring ingester (e.g. event_news_gdelt, a one-time BigQuery
# backfill, see HANDOFF's "GDELT 歷史回填" section) maps to None so it's
# never flagged, since "stale" has no meaning for something not scheduled to
# refresh. This is the automated version of the manual check that caught the
# FRED DXY lag (HANDOFF's "FRED macro 資料落後修復") -- that incident is
# exactly the failure mode this exists to surface without a human noticing
# by hand.
STALENESS_MAX_LAG_DAYS: dict[str, int | None] = {
    "ohlcv_daily": 3,
    "macro_series_daily": 4,
    "iv_surface_daily": 3,
    "options_derived_daily": 3,
    "put_call_ratio_daily": 3,
    "news_articles_raw": 2,
    "social_posts_raw": 2,
    "event_news_gdelt": None,
}
DEFAULT_MAX_LAG_DAYS = 3


def pipeline_health(conn: duckdb.DuckDBPyConnection, *, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    rows = conn.execute(
        """
        SELECT target_table, status, started_at, rows_written
        FROM ingestion_runs
        QUALIFY row_number() OVER (PARTITION BY target_table ORDER BY started_at DESC) = 1
        ORDER BY target_table
        """
    ).fetchall()
    result = []
    for table, status, started_at, rows_written in rows:
        max_lag = STALENESS_MAX_LAG_DAYS.get(table, DEFAULT_MAX_LAG_DAYS)
        days_since = (now - started_at).days
        is_stale = status != "success" or (max_lag is not None and days_since > max_lag)
        result.append({
            "table": table, "last_status": status, "last_run_at": started_at,
            "rows_written": rows_written, "days_since_last_run": days_since,
            "is_stale": is_stale,
        })
    return result


def watchlist_core(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = conn.execute(
        "SELECT symbol, sector, tier FROM watchlist_members WHERE removed_date IS NULL ORDER BY tier, symbol"
    ).fetchall()
    return [{"symbol": r[0], "sector": r[1], "tier": r[2]} for r in rows]


def watchlist_candidates(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT proposed_date, symbol, theme, rationale, evidence_count, source_refs
        FROM watchlist_candidates WHERE status = 'proposed' ORDER BY proposed_date DESC
        """
    ).fetchall()
    return [
        {
            "proposed_date": r[0], "symbol": r[1], "theme": r[2], "rationale": r[3],
            "evidence_count": r[4], "source_refs": json.loads(r[5]) if r[5] else [],
        }
        for r in rows
    ]


def _latest_prediction_rows(conn: duckdb.DuckDBPyConnection, *, symbol: str | None = None) -> list[tuple]:
    where = "WHERE symbol = ?" if symbol else ""
    params = [symbol.upper()] if symbol else []
    return conn.execute(
        f"""
        WITH latest AS (
            SELECT *, row_number() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rn
            FROM daily_predictions
            {where}
        )
        SELECT symbol, sector, trade_date, horizon, label_end_date, regime, proba_down,
               proba_range, proba_up, predicted_direction, entry_price, target_price_up,
               target_price_down, feature_values, model_version
        FROM latest WHERE rn = 1 ORDER BY symbol
        """,
        params,
    ).fetchall()


def _latest_snapshot_rows(conn: duckdb.DuckDBPyConnection) -> dict[str, dict]:
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT *, row_number() OVER (PARTITION BY symbol ORDER BY as_of_date DESC) AS rn
            FROM symbol_backtest_snapshot
        )
        SELECT symbol, as_of_date, overall_n, overall_accuracy, overall_brier, overall_sharpe,
               ev_passed_n, ev_passed_win_rate, ev_blocked_n, ev_blocked_win_rate, ev_of_continuing_now
        FROM latest WHERE rn = 1
        """
    ).fetchall()
    out = {}
    for r in rows:
        out[r[0]] = {
            "as_of_date": r[1], "overall_n": r[2], "overall_accuracy": r[3], "overall_brier": r[4],
            "overall_sharpe": r[5], "ev_passed_n": r[6], "ev_passed_win_rate": r[7],
            "ev_blocked_n": r[8], "ev_blocked_win_rate": r[9], "ev_of_continuing_now": r[10],
        }
    return out


def _prediction_row_to_dict(row: tuple, snapshot: dict | None) -> dict:
    (
        symbol, sector, trade_date, horizon, label_end_date, regime, proba_down, proba_range,
        proba_up, predicted_direction, entry_price, target_price_up, target_price_down,
        feature_values_json, model_version,
    ) = row
    conviction = max(proba_down, proba_range, proba_up)
    return {
        "symbol": symbol, "sector": sector, "trade_date": trade_date, "horizon": horizon,
        "label_end_date": label_end_date, "regime": regime,
        "proba": {"down": proba_down, "range": proba_range, "up": proba_up},
        "predicted_direction": predicted_direction, "conviction": conviction,
        "entry_price": entry_price, "target_price_up": target_price_up,
        "target_price_down": target_price_down, "feature_values": json.loads(feature_values_json),
        "model_version": model_version, "backtest": snapshot,
    }


def _latest_catalyst_headlines(conn: duckdb.DuckDBPyConnection) -> dict[str, str]:
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT symbol, catalyst_summary,
                   row_number() OVER (PARTITION BY symbol ORDER BY as_of_date DESC) AS rn
            FROM catalyst_signals
        )
        SELECT symbol, catalyst_summary FROM latest WHERE rn = 1
        """
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def opportunities(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """Today's (or most-recently-cached) call for every watchlist symbol that
    has one, ranked by conviction. This is a v1 heuristic sort -- CLAUDE.md
    section 12's honesty requirement means this must not be read as a
    validated ranking of trade quality, just "the model's most confident
    calls first". Symbols with no cached prediction yet (e.g.
    scripts/build_dashboard_snapshot.py hasn't run, or the symbol lacks the
    sector feature production.py needs) are simply absent, not zero-filled.

    `catalyst_headline` is the model judgment's side-by-side companion
    (CLAUDE.md section 1: discretion input, never blended into the
    probability itself) -- None until scripts/synthesize_catalysts.py has
    run and found something for that symbol.
    """
    snapshots = _latest_snapshot_rows(conn)
    headlines = _latest_catalyst_headlines(conn)
    rows = _latest_prediction_rows(conn)
    items = []
    for r in rows:
        item = _prediction_row_to_dict(r, snapshots.get(r[0]))
        item["catalyst_headline"] = headlines.get(r[0])
        items.append(item)
    return sorted(items, key=lambda it: it["conviction"], reverse=True)


def ticker_detail(conn: duckdb.DuckDBPyConnection, symbol: str) -> dict | None:
    symbol = symbol.upper()
    rows = _latest_prediction_rows(conn, symbol=symbol)
    if not rows:
        return None
    snapshot = _latest_snapshot_rows(conn).get(symbol)
    prediction = _prediction_row_to_dict(rows[0], snapshot)

    history = conn.execute(
        """
        SELECT prediction_id, trade_date, predicted_direction, status, outcome, label_end_date
        FROM daily_predictions WHERE symbol = ? ORDER BY trade_date DESC LIMIT ?
        """,
        [symbol, RECENT_PREDICTIONS_LIMIT],
    ).fetchall()
    prediction["history"] = [
        {
            "prediction_id": h[0], "trade_date": h[1], "predicted_direction": h[2],
            "status": h[3], "outcome": h[4], "label_end_date": h[5],
        }
        for h in history
    ]

    prices = conn.execute(
        """
        WITH latest AS (
            SELECT trade_date, close,
                   row_number() OVER (PARTITION BY trade_date ORDER BY ingested_at DESC) AS rn
            FROM ohlcv_daily WHERE symbol = ? AND close IS NOT NULL
        )
        SELECT trade_date, close FROM latest WHERE rn = 1
        ORDER BY trade_date DESC LIMIT ?
        """,
        [symbol, PRICE_HISTORY_DAYS],
    ).fetchall()
    prediction["price_history"] = [{"trade_date": p[0], "close": p[1]} for p in reversed(prices)]

    catalyst = get_latest_catalyst_for_symbol(conn, symbol)
    prediction["catalyst"] = (
        {
            "as_of_date": catalyst.as_of_date, "catalyst_summary": catalyst.catalyst_summary,
            "transmission_chain": catalyst.transmission_chain, "novelty_score": catalyst.novelty_score,
            "sentiment_score": catalyst.sentiment_score, "priced_in_estimate": catalyst.priced_in_estimate,
            "source_refs": catalyst.source_refs,
        }
        if catalyst is not None else None
    )

    return prediction


def predictions_overview(conn: duckdb.DuckDBPyConnection) -> dict:
    history = win_rate_history(conn)
    rolling: list[dict] = []
    wins_seen: list[int] = []
    for label_end_date, outcome in history:
        wins_seen.append(1 if outcome == "win" else 0)
        window = wins_seen[-ROLLING_WIN_RATE_WINDOW:]
        rolling.append({"label_end_date": label_end_date, "rolling_win_rate": sum(window) / len(window)})

    outcome_counts = conn.execute(
        "SELECT outcome, count(*) FROM daily_predictions "
        "WHERE status = 'graded' AND predicted_direction != 'range' GROUP BY outcome"
    ).fetchall()

    recent = conn.execute(
        """
        SELECT prediction_id, trade_date, symbol, sector, regime, proba_down, proba_range,
               proba_up, predicted_direction, entry_price, target_price_up, target_price_down,
               feature_values, model_version, status, outcome, label_end_date
        FROM daily_predictions ORDER BY trade_date DESC, symbol LIMIT ?
        """,
        [RECENT_PREDICTIONS_LIMIT],
    ).fetchall()
    recent_cols = [
        "prediction_id", "trade_date", "symbol", "sector", "regime", "proba_down", "proba_range",
        "proba_up", "predicted_direction", "entry_price", "target_price_up", "target_price_down",
        "feature_values", "model_version", "status", "outcome", "label_end_date",
    ]
    recent_out = []
    for r in recent:
        d = dict(zip(recent_cols, r))
        d["feature_values"] = json.loads(d["feature_values"])
        recent_out.append(d)

    return {
        "rolling_win_rate": rolling,
        "outcome_counts": {o: n for o, n in outcome_counts},
        "recent": recent_out,
    }


def positions_with_risk(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """Open option positions + risk-control lights, using cached regime/EV
    from symbol_backtest_snapshot/daily_predictions (nightly-refreshed)
    instead of fitting production models live per request. current_premium
    still can't be filled in here (CLAUDE.md/HANDOFF: no historical
    options-quote store) -- callers needing a live risk light for a specific
    contract's current premium should treat this endpoint's light as
    "as of last night's close", not real-time.
    """
    positions = list_open_positions(conn)
    if not positions:
        return []

    snapshots = _latest_snapshot_rows(conn)
    regime_by_symbol = {
        r[0]: r[1]
        for r in conn.execute(
            """
            WITH latest AS (
                SELECT symbol, regime, row_number() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rn
                FROM daily_predictions
            )
            SELECT symbol, regime FROM latest WHERE rn = 1
            """
        ).fetchall()
    }
    latest_price_rows = {
        r[0]: (r[1], r[2])
        for r in conn.execute(
            """
            WITH latest AS (
                SELECT symbol, trade_date, close,
                       row_number() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rn
                FROM ohlcv_daily WHERE close IS NOT NULL
            )
            SELECT symbol, trade_date, close FROM latest WHERE rn = 1
            """
        ).fetchall()
    }

    out = []
    for position in positions:
        price_row = latest_price_rows.get(position.symbol)
        as_of_date, underlying_price = price_row if price_row else (position.entry_date, position.entry_underlying_price)
        snapshot = snapshots.get(position.symbol)
        market_snapshot = MarketSnapshot(
            as_of_date=as_of_date,
            underlying_price=underlying_price,
            current_premium=None,  # not tracked; see docstring
            current_regime=regime_by_symbol.get(position.symbol),
            ev_of_continuing=snapshot["ev_of_continuing_now"] if snapshot else None,
        )
        assessment = assess_position(position, market_snapshot)
        out.append({
            "position_id": position.position_id, "symbol": position.symbol,
            "option_right": position.option_right, "side": position.side,
            "entry_date": position.entry_date, "entry_underlying_price": position.entry_underlying_price,
            "entry_premium": position.entry_premium, "current_underlying_price": underlying_price,
            "as_of_date": as_of_date, "light": assessment.light,
            "triggers": [{"kind": t.kind, "light": t.light, "detail": t.detail} for t in assessment.triggers],
            "notes": assessment.notes,
        })
    return out


def catalysts(conn: duckdb.DuckDBPyConnection, *, hours: int = 48) -> dict:
    """The "消息雷達" (catalyst radar) view's data source: recent
    transmission-chain theses from stockmoney.data.catalyst_synthesis
    (Sonnet's deep pass), ranked novelty x |sentiment| first. `available` is
    always True now that catalyst_signals is wired up (Phase 1) -- `items`
    being empty is an honest "no fresh catalysts found yet" state (e.g.
    scripts/classify_scan.py / scripts/synthesize_catalysts.py haven't been
    run with a real API key yet), distinguishable from the Phase 0 stub by
    the frontend checking `available`, not by items alone."""
    signals = list_recent_catalysts(conn, hours=hours)
    return {
        "available": True,
        "items": [
            {
                "signal_id": s.signal_id, "symbol": s.symbol, "as_of_date": s.as_of_date,
                "catalyst_summary": s.catalyst_summary, "transmission_chain": s.transmission_chain,
                "novelty_score": s.novelty_score, "sentiment_score": s.sentiment_score,
                "priced_in_estimate": s.priced_in_estimate, "source_refs": s.source_refs,
                "model_version": s.model_version, "available_at": s.available_at,
            }
            for s in signals
        ],
    }


# --- Trader League Arena (Worker 1) -----------------------------------------
# Read-only contract for the Worker-2 React frontend. All three read only
# already-computed trader_predictions / trader_review outputs -- no live fit,
# same discipline as the rest of this module.

def league_table(conn: duckdb.DuckDBPyConnection, *, window: int = 20, cost_bps: float = 0.0) -> list[dict]:
    """Per-trader scorecard (rolling + per-regime): hit rate / Brier / avg PnL
    / high-conviction precision. The "誰最近準" league standings."""
    return _compute_league_table(conn, window=window, cost_bps=cost_bps)


def traders(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """The trader roster (active + retired) for the league view's legend."""
    return [
        {
            "trader_id": t.trader_id, "name": t.name, "philosophy": t.philosophy,
            "engine_key": t.engine_key, "active": t.active,
            "added_date": t.added_date, "removed_date": t.removed_date,
        }
        for t in list_all_traders(conn)
    ]


def _prediction_row(p) -> dict:
    return {
        "trader_id": p.trader_id, "trade_date": p.trade_date, "direction": p.direction,
        "conviction": p.conviction, "rationale": p.rationale, "invalidation": p.invalidation,
        "regime": p.regime, "horizon": p.horizon, "label_end_date": p.label_end_date,
        "status": p.status, "outcome": p.outcome, "method_version": p.method_version,
    }


def latest_trader_predictions(conn: duckdb.DuckDBPyConnection, symbol: str) -> dict | None:
    """Each trader's most recent call for one symbol (the latest league day it
    was covered), plus a consensus/divergence summary. Powers the per-ticker
    "各交易員的判斷 + 共識 + 分歧" panel. None if the symbol has no calls yet."""
    row = conn.execute(
        "SELECT max(trade_date) FROM trader_predictions WHERE symbol = ?", [symbol.upper()]
    ).fetchone()
    if row is None or row[0] is None:
        return None
    trade_date = row[0]
    preds = predictions_on_date(conn, trade_date, symbol=symbol)
    directions = {p.direction for p in preds}
    return {
        "symbol": symbol.upper(),
        "trade_date": trade_date,
        "traders": [_prediction_row(p) for p in preds],
        "consensus": {
            "agree": len(directions) == 1,
            "directions": sorted(directions),
            "n_traders": len(preds),
        },
    }


def recent_divergence(conn: duckdb.DuckDBPyConnection, *, hours: int = 168) -> list[dict]:
    """Recent cross-trader disagreements (CLAUDE.md section 8), disagreements
    first. `was_right` is filled once graded (who ultimately called it)."""
    return recent_divergence_rows(conn, hours=hours)
