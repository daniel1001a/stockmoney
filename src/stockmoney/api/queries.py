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
import re
from datetime import date, datetime, timedelta, timezone

import duckdb

from stockmoney.data.catalyst_signals import get_latest_catalyst_for_symbol, list_recent_catalysts
from stockmoney.data.daily_predictions import win_rate_history
from stockmoney.data.positions import list_open_positions
from stockmoney.data.trader_predictions import predictions_on_date
from stockmoney.data.trader_review import recent_divergence_rows
from stockmoney.data.traders import list_all_traders
from stockmoney.league import ledger as _ledger
from stockmoney.league.league_table import league_equity_curves as _compute_equity_curves
from stockmoney.league.league_table import league_table as _compute_league_table
from stockmoney.league.league_table import overall_stats as _compute_overall_stats
from stockmoney.models.options_risk import MarketSnapshot, assess_position
from stockmoney.models.regime import REGIME_OBS_COLUMNS, describe_regimes

ROLLING_WIN_RATE_WINDOW = 20
RECENT_PREDICTIONS_LIMIT = 15
PRICE_HISTORY_DAYS = 90

DIRECTION_CN = {"up": "看漲", "down": "看跌", "range": "區間"}


def regime_label(regime: int | None, label_map: dict[int, str] | None = None) -> str:
    """Human name for a regime cluster id.

    Cluster ids are arbitrary and unstable across fits, so the label must be
    derived from the cluster's centroid (vol / trend strength), not a fixed
    id->name table -- see models.regime.describe_regimes. ``label_map`` is that
    per-fit mapping, built by ``regime_label_map`` from the latest predictions'
    stored feature values. Falls back to a bare "regime N" only when the map has
    no entry (e.g. a historical id from an older fit).
    """
    if regime is None:
        return "未分類"
    if label_map and regime in label_map:
        return label_map[regime]
    return f"regime {regime}"


def _latest_by_symbol(
    conn: duckdb.DuckDBPyConnection, table: str, value_cols: str, order_col: str, *, where: str = "",
) -> dict:
    """The freshest `value_cols` per symbol from `table`, ordered by
    `order_col` desc -- the "one row per key, newest wins" idiom this module
    (and cockpit.py) used to retype by hand at every call site. Returns
    {symbol: value} for a single value_cols column, {symbol: (v1, v2, ...)}
    for several. table/value_cols/order_col/where are always internal
    literals, never request input -- the same trusted f-string-composition
    pattern _NEWS_COLS uses further down this module."""
    rows = conn.execute(
        f"""
        WITH latest AS (
            SELECT symbol, {value_cols},
                   row_number() OVER (PARTITION BY symbol ORDER BY {order_col} DESC) AS rn
            FROM {table}
            {where}
        )
        SELECT symbol, {value_cols} FROM latest WHERE rn = 1
        """
    ).fetchall()
    if "," in value_cols:
        return {r[0]: tuple(r[1:]) for r in rows}
    return {r[0]: r[1] for r in rows}


def latest_regime_by_symbol(conn: duckdb.DuckDBPyConnection) -> dict[str, int | None]:
    """Each watchlist symbol's most recent regime id from daily_predictions.
    Shared by positions_with_risk below and cockpit.build_cockpit -- both need
    "today's regime per symbol" and used to each retype this query by hand
    (one of them via a private reach-in into this module)."""
    return _latest_by_symbol(conn, "daily_predictions", "regime", "trade_date")


def regime_label_map(conn: duckdb.DuckDBPyConnection) -> dict[int, str]:
    """Build {regime_id: label} from the empirical centroid of each cluster.

    Reads the most recent fit's predictions (latest ``model_version``) and
    averages the three regime-observation features
    (realized_vol_20d / adx_14 / xsec_dispersion, stored in each row's
    ``feature_values`` JSON) per regime id -- the empirical centroid of that
    cluster -- then hands them to models.regime.describe_regimes. Scoping to one
    model_version avoids pooling clusters from fits with different feature
    semantics; ids are stable within a version.
    """
    row = conn.execute(
        "SELECT model_version FROM daily_predictions ORDER BY trade_date DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return {}
    latest_version = row[0]
    rows = conn.execute(
        "SELECT regime, feature_values FROM daily_predictions WHERE model_version = ?",
        [latest_version],
    ).fetchall()

    sums: dict[int, list[float]] = {}
    counts: dict[int, int] = {}
    for regime, fv_json in rows:
        try:
            fv = json.loads(fv_json)
            obs = [float(fv[c]) for c in REGIME_OBS_COLUMNS]
        except (KeyError, TypeError, ValueError):
            continue
        if regime not in sums:
            sums[regime] = [0.0, 0.0, 0.0]
            counts[regime] = 0
        for i in range(3):
            sums[regime][i] += obs[i]
        counts[regime] += 1

    centroids = {
        r: tuple(sums[r][i] / counts[r] for i in range(3))
        for r in sums
        if counts[r] > 0
    }
    return describe_regimes(centroids)


def _plain_thesis(
    direction: str, regime: int | None, conviction: float, label_map: dict[int, str] | None = None
) -> str:
    return (
        f"{regime_label(regime, label_map)}格局下,模型偏向"
        f"{DIRECTION_CN.get(direction, direction)},信心 {round(conviction * 100)}%。"
    )

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


def _prediction_row_to_dict(
    row: tuple, snapshot: dict | None, label_map: dict[int, str] | None = None
) -> dict:
    (
        symbol, sector, trade_date, horizon, label_end_date, regime, proba_down, proba_range,
        proba_up, predicted_direction, entry_price, target_price_up, target_price_down,
        feature_values_json, model_version,
    ) = row
    conviction = max(proba_down, proba_range, proba_up)
    # A 'range' call has no tradeable directional edge: we trade short-dated
    # options, where a flat underlying just bleeds theta (CLAUDE.md section 0/6).
    # So "how confident is the model in a MONEY-MAKING move" is the probability
    # of the *directional* call, NOT max(...) -- a high-conviction 'range' is
    # confidence in going nowhere, useless for an options entry. directional_
    # conviction is None for 'range' precisely so it can't masquerade as an
    # opportunity in the ranking below.
    actionable = predicted_direction in ("up", "down")
    directional_conviction = (
        proba_up if predicted_direction == "up"
        else proba_down if predicted_direction == "down"
        else None
    )
    label = regime_label(regime, label_map)
    return {
        "symbol": symbol, "sector": sector, "trade_date": trade_date, "horizon": horizon,
        "label_end_date": label_end_date, "regime": regime,
        "regime_label": label,
        # Regimes measure vol/trend STRENGTH, not direction (Wave A2). A
        # directional bet inside a *trending* regime is with-the-current; the
        # same call inside a low-vol/range regime is likely chop-noise -- the
        # "up in an uptrend vs up in choppy-but-currently-up" distinction the
        # human should weigh (surfaced, never blended into the probability).
        "regime_is_trending": "趨勢" in (label or ""),
        "thesis": _plain_thesis(predicted_direction, regime, conviction, label_map),
        "proba": {"down": proba_down, "range": proba_range, "up": proba_up},
        "predicted_direction": predicted_direction, "conviction": conviction,
        "actionable": actionable, "directional_conviction": directional_conviction,
        "entry_price": entry_price, "target_price_up": target_price_up,
        "target_price_down": target_price_down, "feature_values": json.loads(feature_values_json),
        "model_version": model_version, "backtest": snapshot,
    }


def _latest_catalyst_headlines(conn: duckdb.DuckDBPyConnection) -> dict[str, str]:
    return _latest_by_symbol(conn, "catalyst_signals", "catalyst_summary", "as_of_date")


# Only surface news this fresh on the opportunity board. Older items are stale
# for a same-day trading decision (news decays fast) and made the board feel
# dead when a symbol's only catalyst_signal was weeks old -- see news_feed's
# same freshness bound.
OPPORTUNITY_NEWS_MAX_AGE_DAYS = 10

# Auto-generated ticker-quote boilerplate a data vendor emits for every symbol
# every day (e.g. "AAPL Stock Quote Price and Forecast - CNN") -- it carries no
# actual news, just a templated page title. Dashboard v2 item 2
# (WORKER6_AUTONOMOUS_SPEC.md): filter these out wherever a headline is meant
# to explain "why" something is happening, so a real story isn't crowded out
# by a template. Kept as a short, explicit pattern list rather than a broad
# "forecast" ban -- a real analyst-forecast headline (e.g. "TSMC Posts
# Stronger-Than-Expected Sales") should still get through.
_GENERIC_HEADLINE_PATTERNS = (
    re.compile(r"stock quote", re.IGNORECASE),
    re.compile(r"price and forecast", re.IGNORECASE),
)


def is_generic_headline(headline: str | None) -> bool:
    """True for template/no-information headlines (see _GENERIC_HEADLINE_PATTERNS
    above). Used by both the SQL filter in latest_symbol_news below and
    cockpit.py's macro-narrative headline picker, so "what counts as generic"
    is defined in exactly one place."""
    if not headline:
        return False
    return any(p.search(headline) for p in _GENERIC_HEADLINE_PATTERNS)


def latest_symbol_news(
    conn: duckdb.DuckDBPyConnection, *, max_age_days: int = OPPORTUNITY_NEWS_MAX_AGE_DAYS
) -> dict[str, dict]:
    """The single most relevant RECENT headline per symbol, so every opportunity
    card can show a live news line -- not just the few symbols that happen to
    have an LLM catalyst_signal. Ranked by importance then recency; bounded to
    the last `max_age_days` so a stale headline never masquerades as today's
    reason to trade. Generic template headlines (is_generic_headline) are
    excluded from the ranking itself -- not just hidden after the fact -- so a
    symbol whose only recent item is a "Stock Quote" boilerplate falls back to
    no headline rather than showing a useless one."""
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT symbol, item_id, headline, sentiment_score, importance, published_at,
                   row_number() OVER (
                       PARTITION BY symbol
                       ORDER BY coalesce(importance, 0) DESC, published_at DESC
                   ) AS rn
            FROM news_items
            WHERE symbol IS NOT NULL
              AND published_at >= now() - (? * INTERVAL 1 DAY)
              AND headline NOT ILIKE '%stock quote%'
              AND headline NOT ILIKE '%price and forecast%'
        )
        SELECT symbol, item_id, headline, sentiment_score, importance, published_at
        FROM ranked WHERE rn = 1
        """,
        [max_age_days],
    ).fetchall()
    return {
        r[0]: {
            "item_id": r[1], "headline": r[2], "sentiment_score": r[3],
            "importance": r[4], "published_at": r[5],
        }
        for r in rows
    }


# "機構情緒" = sell-side analyst ratings/price-target actions we already ingest
# as news_items(item_type='analyst_rating') — real data, never fabricated. No
# free source for actual institutional order flow or insider positioning
# exists (that's the CLAUDE.md-honest answer, not a gap to paper over), so this
# is explicitly scoped to "what sell-side analysts are saying", not a broader
# "institutional sentiment" claim.
ANALYST_SENTIMENT_WINDOW_DAYS = 30
# Below this many sentiment-scored ratings in the window, don't report a
# direction at all -- 1 headline swinging from -1 to +1 is noise, not a signal.
ANALYST_SENTIMENT_MIN_RATINGS = 2


def analyst_sentiment_for_symbol(
    conn: duckdb.DuckDBPyConnection, symbol: str, *, window_days: int = ANALYST_SENTIMENT_WINDOW_DAYS
) -> dict:
    """Aggregate recent analyst-rating news into one honest read: average
    sentiment (None, not 0, when there isn't enough data -- 0 would silently
    claim "neutral"), the raw count backing it, and the single latest rating
    headline/link for a human to read themselves."""
    rows = conn.execute(
        """
        SELECT sentiment_score, headline, url, published_at
        FROM news_items
        WHERE symbol = ? AND item_type = 'analyst_rating'
          AND published_at >= now() - (? * INTERVAL 1 DAY)
        ORDER BY published_at DESC
        """,
        [symbol.upper(), window_days],
    ).fetchall()
    scored = [r[0] for r in rows if r[0] is not None]
    n_scored = len(scored)
    sufficient = n_scored >= ANALYST_SENTIMENT_MIN_RATINGS
    latest = rows[0] if rows else None
    return {
        "n_ratings": len(rows),
        "n_scored": n_scored,
        "avg_sentiment": (sum(scored) / n_scored) if sufficient else None,
        "sufficient_data": sufficient,
        "latest_headline": latest[1] if latest else None,
        "latest_url": latest[2] if latest else None,
        "latest_published_at": latest[3] if latest else None,
    }


def market_analyst_sentiment(
    conn: duckdb.DuckDBPyConnection, *, window_days: int = ANALYST_SENTIMENT_WINDOW_DAYS
) -> dict:
    """Watchlist-wide read for the morning-briefing strip: how many symbols
    currently have enough analyst-rating coverage to call bullish/bearish/
    neutral, and the tally. Symbols below ANALYST_SENTIMENT_MIN_RATINGS are
    excluded entirely rather than counted as neutral -- "no data" and "neutral
    rating" are different facts."""
    rows = conn.execute(
        """
        SELECT symbol, avg(sentiment_score) AS avg_s, count(*) AS n
        FROM news_items
        WHERE item_type = 'analyst_rating' AND sentiment_score IS NOT NULL
          AND symbol IS NOT NULL
          AND published_at >= now() - (? * INTERVAL 1 DAY)
        GROUP BY symbol
        HAVING count(*) >= ?
        """,
        [window_days, ANALYST_SENTIMENT_MIN_RATINGS],
    ).fetchall()
    bullish = sum(1 for _, avg_s, _ in rows if avg_s > 0.15)
    bearish = sum(1 for _, avg_s, _ in rows if avg_s < -0.15)
    neutral = len(rows) - bullish - bearish
    return {
        "n_symbols_covered": len(rows),
        "bullish": bullish, "neutral": neutral, "bearish": bearish,
    }


def opportunities(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """Today's (or most-recently-cached) call for every watchlist symbol that
    has one, ranked so the money-making calls come first.

    "精選" means "most likely to actually make money after analysis", NOT "most
    confident about anything". We trade short-dated options, so a call only
    earns unless the underlying MOVES: a 'range' prediction -- however high its
    softmax confidence -- is a no-trade (theta bleed), so it must never outrank
    a genuine directional call. The sort is therefore, in order:
      1. actionable (a directional up/down call) before 'range',
      2. then by directional_conviction (confidence in the DIRECTIONAL move,
         not max(...)), so among tradeable calls the strongest edge leads.
    This is a heuristic ordering of the model's own confidence, still NOT a
    validated ranking of realized trade quality (CLAUDE.md section 12) -- the
    backtested win rate rides along in each item's `backtest` for the human to
    weigh. 'range' rows are still returned (the UI shows "analysed, no edge
    today"), just always last -- an empty opportunity board hides the fact that
    the watchlist WAS scanned.

    `catalyst_headline` is the model judgment's side-by-side companion
    (CLAUDE.md section 1: discretion input, never blended into the
    probability itself) -- None until scripts/synthesize_catalysts.py has
    run and found something for that symbol.
    """
    snapshots = _latest_snapshot_rows(conn)
    headlines = _latest_catalyst_headlines(conn)
    symbol_news = latest_symbol_news(conn)
    rows = _latest_prediction_rows(conn)
    label_map = regime_label_map(conn)
    items = []
    for r in rows:
        item = _prediction_row_to_dict(r, snapshots.get(r[0]), label_map)
        item["catalyst_headline"] = headlines.get(r[0])
        # Every symbol with recent news gets a live headline (with a link +
        # sentiment), so the board reflects the news radar instead of only the
        # handful of symbols that have an LLM catalyst_signal.
        item["top_news"] = symbol_news.get(r[0])
        items.append(item)
    return sorted(
        items,
        key=lambda it: (
            it["actionable"],
            it["directional_conviction"] if it["actionable"] else -1.0,
            it["conviction"],
        ),
        reverse=True,
    )


def ticker_detail(conn: duckdb.DuckDBPyConnection, symbol: str) -> dict | None:
    symbol = symbol.upper()
    rows = _latest_prediction_rows(conn, symbol=symbol)
    if not rows:
        return None
    snapshot = _latest_snapshot_rows(conn).get(symbol)
    prediction = _prediction_row_to_dict(rows[0], snapshot, regime_label_map(conn))

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

    prediction["news"] = news_for_symbol(conn, symbol, limit=8)
    prediction["analyst_sentiment"] = analyst_sentiment_for_symbol(conn, symbol)

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
    regime_by_symbol = latest_regime_by_symbol(conn)
    latest_price_rows = _latest_by_symbol(
        conn, "ohlcv_daily", "trade_date, close", "trade_date", where="WHERE close IS NOT NULL"
    )

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


def league_overall(conn: duckdb.DuckDBPyConnection, *, window: int | None = None) -> dict:
    """The app's single pooled scorecard across every trader's graded calls --
    "what's our overall hit rate/results", not any one trader's. See
    league_table.overall_stats."""
    return _compute_overall_stats(conn, window=window)


def league_equity(conn: duckdb.DuckDBPyConnection, *, cost_bps: float = 0.0) -> list[dict]:
    """Per-trader cumulative P&L series (資金曲線) over settled predictions,
    ordered by trade_date -- powers the Arena equity-curve chart. See
    league_table.league_equity_curves for the no-look-ahead / directional-vs-
    option P&L accounting this wraps."""
    return _compute_equity_curves(conn, cost_bps=cost_bps)


def recent_trader_trades(conn: duckdb.DuckDBPyConnection, *, limit: int = 40) -> list[dict]:
    """Read-only trade tape across ALL traders for the Arena "交易動態 (Live
    Board)" feed -- most-recent-first, open + closed, joined to the trader's
    display name/philosophy. Degrades to an empty list if trader_trades hasn't
    been populated yet (honest empty state, not an error)."""
    try:
        rows = conn.execute(
            """
            SELECT tt.trade_id, tt.trader_id, tr.name, tr.philosophy,
                   tt.symbol, tt.option_right, tt.side, tt.strike, tt.expiry_date,
                   tt.contracts, tt.entry_at, tt.entry_underlying, tt.entry_premium,
                   tt.exit_at, tt.exit_underlying, tt.exit_premium, tt.realized_pnl,
                   tt.status, tt.thesis, tt.exit_reason
            FROM trader_trades tt
            JOIN traders tr ON tr.trader_id = tt.trader_id
            ORDER BY coalesce(tt.entry_at, tt.created_at) DESC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
    except duckdb.Error:
        return []
    cols = [
        "trade_id", "trader_id", "trader_name", "philosophy",
        "symbol", "option_right", "side", "strike", "expiry_date",
        "contracts", "entry_at", "entry_underlying", "entry_premium",
        "exit_at", "exit_underlying", "exit_premium", "realized_pnl",
        "status", "thesis", "exit_reason",
    ]
    return [dict(zip(cols, r)) for r in rows]


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


# --- Redesign: news feed, events, market summary, trader arena ---------------
# All read-only, all reading nightly/seed-written tables. Nothing here fits a
# model or writes -- same discipline as the rest of this module.

EVENT_TYPE_CN = {"earnings": "財報", "fomc": "FOMC 利率決議", "cpi": "CPI 通膨", "nfp": "非農就業"}


def _news_row_to_dict(r: tuple) -> dict:
    (item_id, symbol, item_type, headline, summary, url, source_name, published_at,
     sentiment, importance, novelty, priced_in, chain, refs) = r
    return {
        "item_id": item_id, "symbol": symbol, "item_type": item_type, "headline": headline,
        "summary": summary, "url": url, "source_name": source_name, "published_at": published_at,
        "sentiment_score": sentiment, "importance": importance, "novelty_score": novelty,
        "priced_in_estimate": priced_in, "transmission_chain": chain,
        "source_refs": json.loads(refs) if refs else [],
    }


_NEWS_COLS = (
    "item_id, symbol, item_type, headline, summary, url, source_name, published_at, "
    "sentiment_score, importance, novelty_score, priced_in_estimate, transmission_chain, source_refs"
)


# News decays fast: a week-old "breaking" headline is noise on a trading feed.
# Both the radar and the per-symbol list bound to this window so stale items age
# out on their own instead of needing manual cleanup.
NEWS_FEED_MAX_AGE_DAYS = 14


def news_feed(
    conn: duckdb.DuckDBPyConnection, *, limit: int = 60, max_age_days: int = NEWS_FEED_MAX_AGE_DAYS
) -> list[dict]:
    """The 消息雷達 feed: every kind of market-relevant news in one normalised
    stream, newest first, bounded to the last `max_age_days` so the radar stays
    current (stale news is worse than no news on a same-day trading surface).
    The frontend filters by type/symbol/search on top of this."""
    rows = conn.execute(
        f"SELECT {_NEWS_COLS} FROM news_items "
        "WHERE published_at >= now() - (? * INTERVAL 1 DAY) "
        "ORDER BY published_at DESC LIMIT ?",
        [max_age_days, limit],
    ).fetchall()
    return [_news_row_to_dict(r) for r in rows]


def news_item(conn: duckdb.DuckDBPyConnection, item_id: str) -> dict | None:
    rows = conn.execute(
        f"SELECT {_NEWS_COLS} FROM news_items WHERE item_id = ?", [item_id]
    ).fetchall()
    return _news_row_to_dict(rows[0]) if rows else None


def news_for_symbol(
    conn: duckdb.DuckDBPyConnection, symbol: str, *, limit: int = 8,
    max_age_days: int = NEWS_FEED_MAX_AGE_DAYS,
) -> list[dict]:
    """Per-ticker news list for the detail page (Robinhood-style): the symbol's
    own items plus market-wide macro items that move everything, bounded to the
    last `max_age_days` (same freshness rule as the radar)."""
    rows = conn.execute(
        f"SELECT {_NEWS_COLS} FROM news_items "
        "WHERE (symbol = ? OR (symbol IS NULL AND item_type = 'macro')) "
        "  AND published_at >= now() - (? * INTERVAL 1 DAY) "
        "ORDER BY published_at DESC LIMIT ?",
        [symbol.upper(), max_age_days, limit],
    ).fetchall()
    return [_news_row_to_dict(r) for r in rows]


def news_freshness(conn: duckdb.DuckDBPyConnection, *, now: datetime | None = None) -> dict:
    """Backs the 消息雷達 freshness strip: "how stale is this feed right now,
    and how often does it actually update" -- the user's complaint was they
    have to re-skim the whole list every time they come back, with no signal
    for whether anything even changed. Read-only, degrades to nulls if
    ingestion_runs has no news rows yet (fresh DB / migration not run)."""
    now = now or datetime.now(timezone.utc)
    last_updated = conn.execute("SELECT max(created_at) FROM news_items").fetchone()[0]

    cutoff = now - timedelta(hours=24)
    news_last_24h = conn.execute(
        "SELECT count(*) FROM news_items WHERE created_at >= ?", [cutoff]
    ).fetchone()[0]

    run_row = conn.execute(
        """
        SELECT source, rows_written, finished_at, status
        FROM ingestion_runs
        WHERE target_table = 'news_articles_raw'
        ORDER BY finished_at DESC NULLS LAST
        LIMIT 1
        """
    ).fetchone()
    last_run = None
    if run_row is not None:
        last_run = {
            "source": run_row[0], "rows_written": run_row[1],
            "finished_at": run_row[2], "status": run_row[3],
        }

    # Cadence signal: median gap between the last ~20 successful news
    # ingestion runs. Needs >=2 finished runs to mean anything.
    finish_times = [
        r[0] for r in conn.execute(
            """
            SELECT finished_at FROM ingestion_runs
            WHERE target_table = 'news_articles_raw' AND status = 'success'
              AND finished_at IS NOT NULL
            ORDER BY finished_at DESC LIMIT 20
            """
        ).fetchall()
    ]
    median_gap_minutes = None
    if len(finish_times) >= 2:
        gaps = sorted(
            (finish_times[i] - finish_times[i + 1]).total_seconds() / 60
            for i in range(len(finish_times) - 1)
        )
        mid = len(gaps) // 2
        median_gap_minutes = (
            gaps[mid] if len(gaps) % 2 == 1 else (gaps[mid - 1] + gaps[mid]) / 2
        )

    return {
        "last_updated": last_updated,
        "last_run": last_run,
        "news_last_24h": news_last_24h,
        "median_ingest_gap_minutes": median_gap_minutes,
    }


def events(conn: duckdb.DuckDBPyConnection, *, now: datetime | None = None) -> list[dict]:
    """Upcoming scheduled market events (earnings / FOMC / CPI / NFP), soonest
    first. Real, public, everyone-knows-it news -- the calm counterpart to the
    catalyst feed's "market may not have priced this in yet"."""
    now = now or datetime.now(timezone.utc)
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT event_id, symbol, event_type, scheduled_at, status,
                   row_number() OVER (PARTITION BY event_id ORDER BY ingested_at DESC) AS rn
            FROM event_calendar
        )
        SELECT event_id, symbol, event_type, scheduled_at, status FROM latest
        WHERE rn = 1 AND scheduled_at >= ? ORDER BY scheduled_at
        """,
        [now - timedelta(days=1)],
    ).fetchall()
    out = []
    for event_id, symbol, event_type, scheduled_at, status in rows:
        out.append({
            "event_id": event_id, "symbol": symbol, "event_type": event_type,
            "event_type_label": EVENT_TYPE_CN.get(event_type, event_type),
            "scheduled_at": scheduled_at, "status": status,
            "days_until": (scheduled_at - now).days,
        })
    return out


def market_summary(conn: duckdb.DuckDBPyConnection) -> dict:
    """The morning-briefing strip: one glance at 'what is the market doing'.
    Regime mix + directional breadth + VIX + biggest movers across the
    watchlist, all from the latest cached rows."""
    preds = _latest_prediction_rows(conn)
    label_map = regime_label_map(conn)
    as_of = max((r[2] for r in preds), default=None)
    dir_counts = {"up": 0, "down": 0, "range": 0}
    regime_counts: dict[str, int] = {}
    convictions = []
    for r in preds:
        direction = r[9]
        dir_counts[direction] = dir_counts.get(direction, 0) + 1
        label = regime_label(r[5], label_map)
        regime_counts[label] = regime_counts.get(label, 0) + 1
        convictions.append(max(r[6], r[7], r[8]))

    # dominant regime = the one the most symbols are in right now
    dominant_regime = max(regime_counts.items(), key=lambda kv: kv[1])[0] if regime_counts else None

    vix_row = conn.execute(
        """
        WITH latest AS (SELECT max(trade_date) AS d FROM vix_term_structure_daily)
        SELECT tenor_days, vix_value FROM vix_term_structure_daily
        WHERE trade_date = (SELECT d FROM latest)
        """
    ).fetchall()
    vix_by_tenor = {t: v for t, v in vix_row}
    vix_spot = vix_by_tenor.get(30)
    vix_slope = (
        vix_by_tenor.get(90) - vix_by_tenor.get(9)
        if vix_by_tenor.get(90) is not None and vix_by_tenor.get(9) is not None else None
    )

    movers = conn.execute(
        """
        WITH latest AS (
            SELECT symbol, trade_date, close,
                   row_number() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rn
            FROM ohlcv_daily WHERE close IS NOT NULL
        )
        SELECT symbol,
               max(CASE WHEN rn = 1 THEN close END) AS c0,
               max(CASE WHEN rn = 2 THEN close END) AS c1
        FROM latest WHERE rn <= 2 GROUP BY symbol
        """
    ).fetchall()
    moves = [
        {"symbol": s, "close": c0, "change_pct": (c0 - c1) / c1}
        for s, c0, c1 in movers if c0 is not None and c1 is not None and c1
    ]
    moves.sort(key=lambda m: m["change_pct"], reverse=True)

    return {
        "as_of_date": as_of,
        "n_symbols": len(preds),
        "direction_counts": dir_counts,
        "regime_counts": regime_counts,
        "dominant_regime": dominant_regime,
        "avg_conviction": sum(convictions) / len(convictions) if convictions else None,
        "vix": vix_spot,
        "vix_term_slope": vix_slope,
        "analyst_sentiment": market_analyst_sentiment(conn),
        "top_gainers": moves[:3],
        "top_losers": list(reversed(moves[-3:])) if len(moves) >= 3 else [],
    }


# --- Trader Arena: leaderboard + per-trader profile -------------------------

CONTEST_RULES = {
    "starting_capital": _ledger.STARTING_CAPITAL,
    "instrument": "短天期選擇權(買/賣 call & put,對應核心 1-9 個月風向)",
    "max_position_pct": _ledger.MAX_POSITION_PCT,
    "scoring": "以總報酬率排名;方向命中率、Brier、每日對帳為輔助指標。",
    "note": "全部為模擬倉位,系統永遠不下真實訂單(CLAUDE.md 硬邊界)。",
}


def _current_underlying(conn: duckdb.DuckDBPyConnection) -> dict[str, float]:
    return _latest_by_symbol(conn, "ohlcv_daily", "close", "trade_date", where="WHERE close IS NOT NULL")


def _rough_mark(trade: dict, spot: float | None) -> tuple[float | None, float | None]:
    """A transparent delta~=0.5 mark for an open option so the UI can show an
    approximate current value. NOT a real quote (CLAUDE.md/HANDOFF: no options
    quote store) -- labelled '粗估' in the UI. Returns (current_premium_est,
    unrealized_pnl_dollars)."""
    if spot is None:
        return None, None
    move = spot - trade["entry_underlying"]
    if trade["option_right"] == "put":
        move = -move
    entry = trade["entry_premium"]
    # delta~=0.5 mark, clamped to a sane short-dated option range: it can decay
    # toward ~0 or roughly triple over the holding window, but this is an
    # approximation for display only (no real quote store), so it must not
    # manufacture an implausible mark-to-market swing.
    est = min(3.0 * entry, max(0.05 * entry, entry + 0.5 * move))
    per_share = (est - entry) if trade["side"] == "long" else (entry - est)
    unreal = per_share * 100 * trade["contracts"]
    return round(est, 2), round(unreal, 2)


def _trader_trades(conn: duckdb.DuckDBPyConnection, trader_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT trade_id, symbol, option_right, side, strike, expiry_date, contracts,
               entry_at, entry_underlying, entry_premium, exit_at, exit_underlying,
               exit_premium, realized_pnl, status, thesis, exit_reason
        FROM trader_trades WHERE trader_id = ? ORDER BY entry_at DESC
        """,
        [trader_id],
    ).fetchall()
    cols = ["trade_id", "symbol", "option_right", "side", "strike", "expiry_date", "contracts",
            "entry_at", "entry_underlying", "entry_premium", "exit_at", "exit_underlying",
            "exit_premium", "realized_pnl", "status", "thesis", "exit_reason"]
    return [dict(zip(cols, r)) for r in rows]


def _portfolio_row(conn: duckdb.DuckDBPyConnection, trader_id: str) -> dict | None:
    row = conn.execute(
        "SELECT starting_capital, cash, max_position_pct, instrument_scope, inception_date "
        "FROM trader_portfolios WHERE trader_id = ?",
        [trader_id],
    ).fetchone()
    if row is None:
        return None
    return {
        "starting_capital": row[0], "cash": row[1], "max_position_pct": row[2],
        "instrument_scope": row[3], "inception_date": row[4],
    }


def _portfolio_stats(portfolio: dict, trades: list[dict], spot: dict[str, float]) -> dict:
    closed = [t for t in trades if t["status"] == "closed"]
    open_trades = [t for t in trades if t["status"] == "open"]
    realized = sum(t["realized_pnl"] or 0.0 for t in closed)
    wins = [t for t in closed if (t["realized_pnl"] or 0.0) > 0]
    unrealized = 0.0
    for t in open_trades:
        _, unreal = _rough_mark(t, spot.get(t["symbol"]))
        unrealized += unreal or 0.0
    start = portfolio["starting_capital"]
    # Equity from first principles: bankroll + booked P&L + open mark-to-market.
    # (Avoids the cash-vs-cost-basis double-count -- cash is shown separately.)
    equity = start + realized + unrealized
    return {
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "equity": round(equity, 2),
        "total_return_pct": (equity - start) / start if start else None,
        "realized_return_pct": realized / start if start else None,
        "n_closed": len(closed),
        "n_open": len(open_trades),
        "trade_win_rate": (len(wins) / len(closed)) if closed else None,
        "best_trade": max((t["realized_pnl"] or 0.0 for t in closed), default=None),
        "worst_trade": min((t["realized_pnl"] or 0.0 for t in closed), default=None),
    }


def leaderboard(conn: duckdb.DuckDBPyConnection, *, window: int = 20) -> list[dict]:
    """Contest standings: every trader's virtual-account return, ranked. This is
    the merged Arena's headline board -- the 'who is actually making money'
    view, with the league hit-rate/Brier folded in as secondary detail."""
    spot = _current_underlying(conn)
    league_by_id = {r["trader_id"]: r for r in _compute_league_table(conn, window=window)}
    out = []
    for trader in list_all_traders(conn):
        league = league_by_id.get(trader.trader_id, {})
        rolling = league.get("rolling", {})
        portfolio = _portfolio_row(conn, trader.trader_id)
        # A trader with no virtual-account portfolio row yet (trader_portfolios
        # is populated lazily) should still appear on the board as long as it
        # has graded calls -- the standings the user cares about (hit rate,
        # option P&L) come from graded predictions, not from booked trades. Only
        # omit a trader that has neither a portfolio nor any graded call.
        if portfolio is None and not (rolling.get("n_graded") or 0):
            continue
        if portfolio is None:
            portfolio = {
                "starting_capital": _ledger.STARTING_CAPITAL, "cash": _ledger.STARTING_CAPITAL,
                "max_position_pct": None, "instrument_scope": None, "inception_date": None,
            }
        trades = _trader_trades(conn, trader.trader_id)
        stats = _portfolio_stats(portfolio, trades, spot)
        out.append({
            "trader_id": trader.trader_id, "name": trader.name, "philosophy": trader.philosophy,
            "active": trader.active,
            "starting_capital": portfolio["starting_capital"],
            **stats,
            "hit_rate": rolling.get("hit_rate"),
            "brier": rolling.get("brier"),
            "n_directional": rolling.get("n_directional", 0),
            # Wave D (IMPROVEMENT_PLAN.md §S3): real option P&L alongside the
            # directional hit-rate above -- a trader can be right on direction
            # and still lose money as an option (theta/IV-crush), which
            # hit_rate alone can't show.
            "option_win_rate": rolling.get("option_win_rate"),
            "avg_option_pnl": rolling.get("avg_option_pnl"),
            "cum_option_pnl": rolling.get("cum_option_pnl", 0.0),
            "n_graded": rolling.get("n_graded", 0),
        })
    # Ranked on REALIZED (booked) return -- a contest is scored on closed
    # results; open positions are marked-to-market for display but their rough
    # mark shouldn't decide the standings.
    out.sort(
        key=lambda r: (
            r["realized_return_pct"] if r["realized_return_pct"] is not None else -1e9,
            r["cum_option_pnl"] if r["cum_option_pnl"] is not None else -1e9,
        ),
        reverse=True,
    )
    for i, r in enumerate(out):
        r["rank"] = i + 1
    return out


def league_training(conn: duckdb.DuckDBPyConnection, *, window: int = 20) -> list[dict]:
    """Per-trader training-performance view: the league scorecard (overall /
    rolling / by-regime, from `league_table`) plus a running win-rate-over-time
    series and the self-improvement proposal + method-version history -- i.e.
    everything needed to see whether a trader's model is actually getting better
    over time, not just where it ranks today. Read-only, settled rows only
    (win-rate series is ordered by trade_date, so no look-ahead)."""
    from stockmoney.data.trader_methods import list_proposals
    from stockmoney.data.trader_predictions import graded_predictions

    proposals_by_trader: dict[str, list[dict]] = {}
    for p in list_proposals(conn):
        proposals_by_trader.setdefault(p["trader_id"], []).append(p)

    versions_by_trader: dict[str, list[dict]] = {}
    for tid, mv, eff, status in conn.execute(
        "SELECT trader_id, method_version, effective_date, status "
        "FROM trader_method_versions ORDER BY effective_date, method_version"
    ).fetchall():
        versions_by_trader.setdefault(tid, []).append(
            {"method_version": mv, "effective_date": eff, "status": status}
        )

    out = []
    for row in _compute_league_table(conn, window=window):
        tid = row["trader_id"]
        graded = [
            p for p in graded_predictions(conn, trader_id=tid)
            if p.direction in ("up", "down") and p.outcome is not None
        ]
        graded.sort(key=lambda p: (p.trade_date, p.symbol))
        series, wins = [], 0
        for i, p in enumerate(graded, start=1):
            if p.outcome == "win":
                wins += 1
            series.append({"trade_date": p.trade_date, "n": i, "hit_rate": wins / i})
        out.append({
            **row,
            "win_rate_series": series,
            "proposals": proposals_by_trader.get(tid, []),
            "method_versions": versions_by_trader.get(tid, []),
        })
    return out


def trader_profile(conn: duckdb.DuckDBPyConnection, trader_id: str) -> dict | None:
    """One trader's full account: contest stats, current open positions (with a
    rough live mark), full trade history, and their recent league calls -- the
    'click into a trader and see a real account' view."""
    trader = next((t for t in list_all_traders(conn) if t.trader_id == trader_id), None)
    portfolio = _portfolio_row(conn, trader_id)
    if trader is None or portfolio is None:
        return None
    spot = _current_underlying(conn)
    trades = _trader_trades(conn, trader_id)
    stats = _portfolio_stats(portfolio, trades, spot)

    open_positions = []
    for t in trades:
        if t["status"] != "open":
            continue
        s = spot.get(t["symbol"])
        est, unreal = _rough_mark(t, s)
        open_positions.append({**t, "current_underlying": s, "current_premium_est": est, "unrealized_pnl": unreal})
    closed_trades = [t for t in trades if t["status"] == "closed"]

    method = conn.execute(
        "SELECT method_version, spec FROM trader_method_versions "
        "WHERE trader_id = ? AND status = 'active' ORDER BY effective_date DESC LIMIT 1",
        [trader_id],
    ).fetchone()

    recent_preds = conn.execute(
        """
        SELECT trade_date, symbol, direction, conviction, rationale, regime, status, outcome,
               label_end_date, option_pnl
        FROM trader_predictions WHERE trader_id = ? ORDER BY trade_date DESC, symbol LIMIT 15
        """,
        [trader_id],
    ).fetchall()
    label_map = regime_label_map(conn)

    return {
        "trader_id": trader.trader_id, "name": trader.name, "philosophy": trader.philosophy,
        "active": trader.active,
        "method_version": method[0] if method else None,
        "method_spec": method[1] if method else None,
        "portfolio": {**portfolio, **stats},
        "rules": CONTEST_RULES,
        "open_positions": open_positions,
        "closed_trades": closed_trades,
        "recent_predictions": [
            {
                "trade_date": r[0], "symbol": r[1], "direction": r[2], "conviction": r[3],
                "rationale": r[4], "regime": r[5], "regime_label": regime_label(r[5], label_map),
                "status": r[6], "outcome": r[7], "label_end_date": r[8], "option_pnl": r[9],
            }
            for r in recent_preds
        ],
    }
