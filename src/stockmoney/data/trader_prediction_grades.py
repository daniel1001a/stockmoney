"""Multi-horizon grading (issue #7 P1, CONTEXT.md's Grading Horizon): the
same Call, written once by `trader_predictions.record_trader_prediction`, is
graded independently at 1/5/21 trading days out. This is a NEW layer
alongside trader_predictions' own label_end_date/status/actual_* -- that
original single-horizon grade keeps meaning exactly what it always meant
(the call's declared horizon); this table never overwrites or replaces it.

Same discipline as trader_predictions.py: GRADING IS A MARKET FACT, using the
identical volatility-band formula (`daily_predictions.band`), just evaluated
at each horizon's own trading-day count rather than the call's originally
declared horizon -- so a call graded "wrong at 1 day, right at 21 days" is
recorded as exactly that, not squashed into one verdict.

Isolation (CLAUDE.md section 7): never read by any training path.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone

import duckdb

from stockmoney.data.daily_predictions import add_trading_days, band as volatility_band
from stockmoney.data.trader_predictions import TraderPrediction

GRADING_HORIZONS = (1, 5, 21)


@dataclass
class PredictionGrade:
    prediction_id: str
    horizon_days: int
    label_end_date: date
    status: str = "pending"
    actual_price: float | None = None
    actual_return: float | None = None
    actual_label: str | None = None
    outcome: str | None = None
    graded_at: datetime | None = None


def record_pending_grades(
    conn: duckdb.DuckDBPyConnection,
    *,
    prediction_id: str,
    trade_date: date,
    horizons: tuple[int, ...] = GRADING_HORIZONS,
) -> None:
    """Write one pending grade row per horizon for a just-recorded call.
    Idempotent: re-running `predict` for an already-recorded call (the same
    idempotency `record_trader_prediction` already provides) must not
    duplicate rows."""
    now = datetime.now(timezone.utc)
    for horizon_days in horizons:
        conn.execute(
            """
            INSERT INTO trader_prediction_grades
                (prediction_id, horizon_days, label_end_date, status, created_at)
            VALUES (?, ?, ?, 'pending', ?)
            ON CONFLICT (prediction_id, horizon_days) DO NOTHING
            """,
            [prediction_id, horizon_days, add_trading_days(trade_date, horizon_days), now],
        )


def list_pending_grades(
    conn: duckdb.DuckDBPyConnection, *, as_of: date | None = None
) -> list[PredictionGrade]:
    """Matured (label_end_date <= as_of) but ungraded horizon rows -- what
    orchestration.grade_matured acts on, mirroring
    trader_predictions.list_pending_trader_predictions. Excludes horizon rows
    belonging to a withdrawn call (issue #7 P1) -- same discipline as that
    function: a withdrawn call never became a real opinion, so it must never
    be graded at any horizon."""
    rows = conn.execute(
        """
        SELECT g.prediction_id, g.horizon_days, g.label_end_date, g.status,
               g.actual_price, g.actual_return, g.actual_label, g.outcome, g.graded_at
        FROM trader_prediction_grades g
        JOIN trader_predictions p ON p.prediction_id = g.prediction_id
        WHERE g.status = 'pending' AND g.label_end_date <= ?
          AND (p.withdrawn IS NULL OR p.withdrawn = false)
        ORDER BY g.label_end_date, g.prediction_id, g.horizon_days
        """,
        [as_of or date.today()],
    ).fetchall()
    return [PredictionGrade(*r) for r in rows]


def grade_horizon(
    conn: duckdb.DuckDBPyConnection, *, prediction_id: str, horizon_days: int, actual_price: float
) -> None:
    """Grade one (prediction_id, horizon_days) pair against the underlying
    call's direction, using the SAME band formula trader_predictions.py uses
    for its own single-horizon grade -- one ruler, just evaluated at this
    horizon's own trading-day count."""
    row = conn.execute(
        "SELECT status FROM trader_prediction_grades WHERE prediction_id = ? AND horizon_days = ?",
        [prediction_id, horizon_days],
    ).fetchone()
    if row is None:
        raise ValueError(f"no pending grade for prediction_id={prediction_id!r} horizon_days={horizon_days}")
    if row[0] == "graded":
        raise ValueError(f"prediction_id={prediction_id!r} horizon_days={horizon_days} is already graded")

    call_row = conn.execute(
        "SELECT direction, entry_price, band_k, grade_vol FROM trader_predictions WHERE prediction_id = ?",
        [prediction_id],
    ).fetchone()
    if call_row is None:
        raise ValueError(f"unknown prediction_id: {prediction_id!r}")
    direction, entry_price, band_k, grade_vol = call_row

    actual_return = actual_price / entry_price - 1.0
    the_band = volatility_band({"realized_vol_20d": grade_vol}, band_k=band_k, horizon=horizon_days)
    if actual_return > the_band:
        actual_label = "up"
    elif actual_return < -the_band:
        actual_label = "down"
    else:
        actual_label = "range"
    outcome = "win" if actual_label == direction else "loss"

    conn.execute(
        """
        UPDATE trader_prediction_grades
        SET status = 'graded', actual_price = ?, actual_return = ?,
            actual_label = ?, outcome = ?, graded_at = ?
        WHERE prediction_id = ? AND horizon_days = ?
        """,
        [actual_price, actual_return, actual_label, outcome, datetime.now(timezone.utc),
         prediction_id, horizon_days],
    )


def graded_for_horizon(
    conn: duckdb.DuckDBPyConnection, *, horizon_days: int, trader_id: str | None = None
) -> list[TraderPrediction]:
    """Every graded row at one horizon, as TraderPrediction-shaped records
    (direction/conviction/regime/symbol/trader_id joined from
    trader_predictions; label_end_date/status/actual_*/outcome from this
    table) so league_table.compute_stats can score a horizon with zero new
    code. option_pnl is always None here -- the booked option instrument is
    priced to the call's ORIGINAL declared horizon only (league/ledger.py),
    not repriced at each of the three grading horizons."""
    query = """
        SELECT
            p.prediction_id, p.trader_id, p.method_version, p.trade_date, p.symbol, p.sector,
            g.horizon_days AS horizon, g.label_end_date, p.direction, p.conviction,
            p.rationale, p.invalidation, p.regime, p.entry_price, p.band_k, p.grade_vol,
            p.engine_payload, p.available_at, g.status,
            g.actual_price, g.actual_return, g.actual_label, g.outcome
        FROM trader_prediction_grades g
        JOIN trader_predictions p ON p.prediction_id = g.prediction_id
        WHERE g.horizon_days = ? AND g.status = 'graded'
    """
    params: list = [horizon_days]
    if trader_id:
        query += " AND p.trader_id = ?"
        params.append(trader_id)
    query += " ORDER BY p.trade_date, p.symbol, p.trader_id"

    out: list[TraderPrediction] = []
    for row in conn.execute(query, params).fetchall():
        (
            prediction_id, trader_id_, method_version, trade_date, symbol, sector,
            horizon, label_end_date, direction, conviction,
            rationale, invalidation, regime, entry_price, band_k, grade_vol,
            engine_payload_json, available_at, status,
            actual_price, actual_return, actual_label, outcome,
        ) = row
        out.append(TraderPrediction(
            prediction_id=prediction_id, trader_id=trader_id_, method_version=method_version,
            trade_date=trade_date, symbol=symbol, sector=sector, horizon=horizon,
            label_end_date=label_end_date, direction=direction, conviction=conviction,
            rationale=rationale, invalidation=invalidation, regime=regime,
            entry_price=entry_price, band_k=band_k, grade_vol=grade_vol,
            engine_payload=json.loads(engine_payload_json) if engine_payload_json else {},
            available_at=available_at, status=status, actual_price=actual_price,
            actual_return=actual_return, actual_label=actual_label, outcome=outcome,
        ))
    return out
