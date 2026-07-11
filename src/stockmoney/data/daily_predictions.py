"""DB access layer for `daily_predictions` -- the daily prediction ledger
(CLAUDE.md section 0/11 spirit: a human-reviewable, evidence-based track
record, never a silent retraining feedback loop). A prediction is recorded
once from `stockmoney.models.production.predict_latest`'s output, then graded
once its `label_end_date` has passed.

Win/loss uses the exact same volatility-scaled band formula
`stockmoney.models.feature_matrix.build_feature_matrix` uses for its
DOWN/RANGE/UP labels, computed from the `realized_vol_20d` value stored in
`feature_values` at prediction time (not re-queried) -- so grading can never
drift from what was actually predicted on, and win-rate here stays directly
comparable to the backtest's own reported accuracy.
"""
from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import duckdb

from stockmoney.data.positions import latest_underlying_price
from stockmoney.models import production
from stockmoney.models.feature_matrix import DOWN, RANGE, TRADING_DAYS, UP

_DIRECTION_BY_CLASS = {DOWN: "down", RANGE: "range", UP: "up"}


@dataclass
class DailyPrediction:
    prediction_id: str
    trade_date: date
    symbol: str
    sector: str
    horizon: int
    label_end_date: date
    regime: int
    proba: tuple[float, float, float]  # (down, range, up)
    predicted_direction: str
    entry_price: float
    band_k: float
    target_price_up: float
    target_price_down: float
    feature_values: dict[str, float]
    model_version: str
    status: str
    actual_price: float | None = None
    actual_return: float | None = None
    actual_label: str | None = None
    outcome: str | None = None


def _band(feature_values: dict[str, float], *, band_k: float, horizon: int) -> float:
    daily_vol = feature_values["realized_vol_20d"] / math.sqrt(TRADING_DAYS)
    return band_k * daily_vol * math.sqrt(horizon)


def record_prediction(
    conn: duckdb.DuckDBPyConnection,
    *,
    trade_date: date,
    symbol: str,
    sector: str,
    horizon: int,
    label_end_date: date,
    regime: int,
    proba: tuple[float, float, float],
    entry_price: float,
    feature_values: dict[str, float],
    model_version: str,
    band_k: float = 0.5,
) -> str:
    """Idempotent per (symbol, trade_date): re-running record-watchlist for a
    day that already has a prediction (e.g. run twice by hand) returns the
    existing prediction_id instead of inserting a duplicate, which would
    otherwise double-count in win_rate_history once both got graded."""
    existing = conn.execute(
        "SELECT prediction_id FROM daily_predictions WHERE symbol = ? AND trade_date = ?",
        [symbol.upper(), trade_date],
    ).fetchone()
    if existing is not None:
        return existing[0]

    direction_class = int(max(range(3), key=lambda i: proba[i]))
    predicted_direction = _DIRECTION_BY_CLASS[direction_class]
    band = _band(feature_values, band_k=band_k, horizon=horizon)

    prediction_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO daily_predictions (
            prediction_id, trade_date, symbol, sector, horizon, label_end_date,
            regime, proba_down, proba_range, proba_up, predicted_direction,
            entry_price, band_k, target_price_up, target_price_down,
            feature_values, model_version, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        [
            prediction_id, trade_date, symbol.upper(), sector, horizon, label_end_date,
            regime, proba[DOWN], proba[RANGE], proba[UP], predicted_direction,
            entry_price, band_k, entry_price * (1 + band), entry_price * (1 - band),
            json.dumps(feature_values), model_version, datetime.now(timezone.utc),
        ],
    )
    return prediction_id


def _add_trading_days(d: date, n: int) -> date:
    """Weekend-skipping approximation of "n trading days after d" -- used
    only to set a target label_end_date for a live prediction, where the
    real future trading calendar can't be read from ohlcv_daily yet (that's
    only possible in hindsight, which is exactly what build_feature_matrix
    does for resolved rows). Market holidays aren't accounted for; `grade`'s
    lookup has a small grace window to tolerate the rare mismatch."""
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def record_live_prediction(
    conn: duckdb.DuckDBPyConnection, *, symbol: str, sector: str, horizon: int = production.DEFAULT_HORIZON
) -> tuple[str | None, str | None]:
    """Fetch production's live prediction for `symbol` and commit it to the
    ledger via `record_prediction` (idempotent per (symbol, trade_date)).

    Shared by scripts/daily_prediction_cli.py's record-watchlist and
    scripts/build_dashboard_snapshot.py so both stay on one definition of
    "record today's live call for a symbol" rather than drifting apart.

    Returns (prediction_id, skip_reason) -- exactly one is None. A skip
    (production has no unresolved row yet, or there's no OHLCV price to
    anchor entry_price to) is an expected, non-error outcome for callers to
    report, not raise on.
    """
    pred = production.predict_latest(conn, target_symbol=symbol, sector=sector, horizon=horizon)
    if pred is None:
        return None, "no unresolved feature row, or missing sector feature/insufficient history"

    latest = latest_underlying_price(conn, symbol)
    if latest is None:
        return None, "no ohlcv_daily price available"

    label_end_date = _add_trading_days(pred.as_of_date, horizon)
    prediction_id = record_prediction(
        conn,
        trade_date=pred.as_of_date, symbol=symbol, sector=sector, horizon=horizon,
        label_end_date=label_end_date, regime=pred.regime, proba=tuple(pred.proba),
        entry_price=latest[1], feature_values=pred.feature_values,
        model_version=pred.model_version,
    )
    return prediction_id, None


_SELECT_COLUMNS = """
    prediction_id, trade_date, symbol, sector, horizon, label_end_date, regime,
    proba_down, proba_range, proba_up, predicted_direction, entry_price, band_k,
    target_price_up, target_price_down, feature_values, model_version, status,
    actual_price, actual_return, actual_label, outcome
"""


def _row_to_prediction(row: tuple) -> DailyPrediction:
    (
        prediction_id, trade_date, symbol, sector, horizon, label_end_date, regime,
        proba_down, proba_range, proba_up, predicted_direction, entry_price, band_k,
        target_price_up, target_price_down, feature_values_json, model_version, status,
        actual_price, actual_return, actual_label, outcome,
    ) = row
    return DailyPrediction(
        prediction_id=prediction_id, trade_date=trade_date, symbol=symbol, sector=sector,
        horizon=horizon, label_end_date=label_end_date, regime=regime,
        proba=(proba_down, proba_range, proba_up), predicted_direction=predicted_direction,
        entry_price=entry_price, band_k=band_k, target_price_up=target_price_up,
        target_price_down=target_price_down, feature_values=json.loads(feature_values_json),
        model_version=model_version, status=status, actual_price=actual_price,
        actual_return=actual_return, actual_label=actual_label, outcome=outcome,
    )


def get_prediction(conn: duckdb.DuckDBPyConnection, prediction_id: str) -> DailyPrediction | None:
    row = conn.execute(
        f"SELECT {_SELECT_COLUMNS} FROM daily_predictions WHERE prediction_id = ?",
        [prediction_id],
    ).fetchone()
    return _row_to_prediction(row) if row else None


def list_pending_predictions(
    conn: duckdb.DuckDBPyConnection, *, as_of: date | None = None
) -> list[DailyPrediction]:
    """Predictions whose horizon has matured (label_end_date <= as_of) but
    haven't been graded yet -- what `grade` should act on."""
    rows = conn.execute(
        f"""
        SELECT {_SELECT_COLUMNS} FROM daily_predictions
        WHERE status = 'pending' AND label_end_date <= ?
        ORDER BY label_end_date
        """,
        [as_of or date.today()],
    ).fetchall()
    return [_row_to_prediction(r) for r in rows]


def grade_prediction(
    conn: duckdb.DuckDBPyConnection, prediction_id: str, *, actual_price: float
) -> None:
    prediction = get_prediction(conn, prediction_id)
    if prediction is None:
        raise ValueError(f"unknown prediction_id: {prediction_id!r}")
    if prediction.status == "graded":
        raise ValueError(f"prediction {prediction_id!r} is already graded")

    actual_return = actual_price / prediction.entry_price - 1.0
    band = _band(prediction.feature_values, band_k=prediction.band_k, horizon=prediction.horizon)
    if actual_return > band:
        actual_label = "up"
    elif actual_return < -band:
        actual_label = "down"
    else:
        actual_label = "range"

    if prediction.predicted_direction == "range":
        outcome = "win" if actual_label == "range" else "loss"
    else:
        outcome = "win" if actual_label == prediction.predicted_direction else "loss"

    conn.execute(
        """
        UPDATE daily_predictions
        SET status = 'graded', actual_price = ?, actual_return = ?,
            actual_label = ?, outcome = ?, graded_at = ?
        WHERE prediction_id = ?
        """,
        [actual_price, actual_return, actual_label, outcome, datetime.now(timezone.utc), prediction_id],
    )


def win_rate_history(conn: duckdb.DuckDBPyConnection, *, symbol: str | None = None) -> list[tuple[date, str]]:
    """(label_end_date, outcome) pairs for graded, directional (non-range)
    predictions, ordered by label_end_date -- the dashboard's rolling
    win-rate chart input."""
    query = """
        SELECT label_end_date, outcome FROM daily_predictions
        WHERE status = 'graded' AND predicted_direction != 'range'
    """
    params: list = []
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol.upper())
    query += " ORDER BY label_end_date"
    return conn.execute(query, params).fetchall()
