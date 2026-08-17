"""DB access layer for `trader_predictions` -- the unified Trader League
ledger (~/.claude/plans/trader-council-rearchitecture.md section 2). The
multi-trader generalization of `daily_predictions`: every trader, whatever its
engine, commits one row of the same shape (direction + conviction + rationale +
horizon + invalidation) per (symbol, trade_date), then that row is graded once
its horizon matures.

GRADING IS A MARKET FACT (the key to a fair league): win/loss uses the exact
same volatility-band formula `daily_predictions`/`feature_matrix` use, applied
to `grade_vol` (the realized_vol_20d stored on the row -- a market feature
every trader can see). So a Chartist and an Analyst who both said "up" on SOXL
on the same day are graded on one identical ruler, and their hit-rate / Brier /
PnL are directly comparable. We deliberately import daily_predictions.band so
there is exactly one band definition in the codebase, not a second copy that
could drift.

Isolation (CLAUDE.md section 7): this module and its table are a human-facing
analytics side-branch; nothing here is ever read by a training path.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import duckdb

from stockmoney.data.daily_predictions import band as _volatility_band

# 'up'/'down' are directional; 'range' is a non-directional (stay-in-band) call.
DIRECTIONS = ("up", "down", "range")


@dataclass
class TraderPrediction:
    prediction_id: str
    trader_id: str
    method_version: str
    trade_date: date
    symbol: str
    sector: str
    horizon: int
    label_end_date: date
    direction: str          # 'up' | 'down' | 'range'
    conviction: float       # 0..1, also read as P(chosen direction correct) for Brier
    rationale: str
    invalidation: str
    entry_price: float
    band_k: float
    grade_vol: float        # realized_vol_20d -> the market fact all traders grade on
    engine_payload: dict = field(default_factory=dict)
    regime: int | None = None
    available_at: datetime | None = None
    status: str = "pending"
    actual_price: float | None = None
    actual_return: float | None = None
    actual_label: str | None = None
    outcome: str | None = None
    # Wave D (IMPROVEMENT_PLAN.md §S3): the concrete option instrument chosen
    # at prediction time (league/option_bridge.py), and its realized P&L once
    # graded (league/grading_options.py). Both None for 'range' calls or when
    # no usable entry IV existed that day -- see option_bridge.py.
    option_structure: dict | None = None
    option_pnl: float | None = None
    # Trade Note (交易筆記, issue #7 P1): structured replacement for the two
    # free-text fields above. None for calls recorded before this landed --
    # old rationale/invalidation stay meaningful on their own, nothing is
    # backfilled. evidence_chain is a list of up to 3 {layer, kind
    # ('observation'|'inference'), credibility, note} dicts.
    thesis: str | None = None
    evidence_chain: list[dict] | None = None
    rejected_alternatives: str | None = None
    confidence_rationale: str | None = None
    # Decision Point (決策時點, issue #7 P1): None = legacy/immediate (recorded
    # and booked in one pass, the only mode that existed before P1).
    # 'pre_market'/'event' calls await a post_open confirm/withdraw pass
    # before they are ever booked; 'post_close' never creates a row.
    decision_point: str | None = None
    confirmed_at: datetime | None = None
    withdrawn: bool = False
    withdrawn_at: datetime | None = None
    withdrawn_reason: str | None = None

    # Attributes the review layer (reused attribution helpers) reads. The
    # attribution decompose/verdict functions only touch these + the fields
    # above, so a TraderPrediction is a drop-in for a DailyPrediction there.
    @property
    def predicted_direction(self) -> str:
        return self.direction

    @property
    def proba(self) -> tuple[float, float, float]:
        """(down, range, up) with the chosen direction carrying `conviction`
        and the remainder split across the other two -- lets attribution's
        _proba_confidence read a coherent confidence for any trader."""
        rest = (1.0 - self.conviction) / 2.0
        return (
            self.conviction if self.direction == "down" else rest,
            self.conviction if self.direction == "range" else rest,
            self.conviction if self.direction == "up" else rest,
        )


def record_trader_prediction(
    conn: duckdb.DuckDBPyConnection,
    *,
    trader_id: str,
    method_version: str,
    trade_date: date,
    symbol: str,
    sector: str,
    horizon: int,
    label_end_date: date,
    direction: str,
    conviction: float,
    rationale: str,
    invalidation: str,
    entry_price: float,
    grade_vol: float,
    engine_payload: dict,
    regime: int | None = None,
    band_k: float = 0.5,
    available_at: datetime | None = None,
    option_structure: dict | None = None,
    thesis: str | None = None,
    evidence_chain: list[dict] | None = None,
    rejected_alternatives: str | None = None,
    confidence_rationale: str | None = None,
    decision_point: str | None = None,
) -> str:
    """Idempotent per (trader_id, symbol, trade_date): re-running `predict`
    for a day already recorded returns the existing prediction_id rather than
    double-counting once both get graded.

    `decision_point` is None for the legacy/immediate path (the only mode
    that existed before issue #7 P1) -- orchestration.run_predictions books
    the trade in the same pass. 'pre_market'/'event' rows are booked later,
    only once orchestration.confirm_and_book confirms them at the post_open
    decision point."""
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}, got {direction!r}")
    existing = conn.execute(
        "SELECT prediction_id FROM trader_predictions WHERE trader_id = ? AND symbol = ? AND trade_date = ?",
        [trader_id, symbol.upper(), trade_date],
    ).fetchone()
    if existing is not None:
        return existing[0]

    prediction_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO trader_predictions (
            prediction_id, trader_id, method_version, trade_date, symbol, sector,
            horizon, label_end_date, direction, conviction, rationale, invalidation,
            regime, entry_price, band_k, grade_vol, engine_payload, available_at,
            option_structure, thesis, evidence_chain, rejected_alternatives,
            confidence_rationale, decision_point, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        [
            prediction_id, trader_id, method_version, trade_date, symbol.upper(), sector,
            horizon, label_end_date, direction, conviction, rationale, invalidation,
            regime, entry_price, band_k, grade_vol, json.dumps(engine_payload),
            available_at or datetime.now(timezone.utc),
            json.dumps(option_structure) if option_structure is not None else None,
            thesis, json.dumps(evidence_chain) if evidence_chain is not None else None,
            rejected_alternatives, confidence_rationale, decision_point,
            datetime.now(timezone.utc),
        ],
    )
    return prediction_id


def confirm_call(conn: duckdb.DuckDBPyConnection, prediction_id: str) -> None:
    """Post_open decision point: a 'pre_market'/'event' call still stands.
    Idempotent (re-confirming an already-confirmed call is a no-op)."""
    conn.execute(
        "UPDATE trader_predictions SET confirmed_at = ? WHERE prediction_id = ? AND confirmed_at IS NULL",
        [datetime.now(timezone.utc), prediction_id],
    )


def withdraw_call(conn: duckdb.DuckDBPyConnection, prediction_id: str, *, reason: str) -> None:
    """Post_open decision point: a 'pre_market'/'event' call's thesis was
    already invalidated before it could be filled -- never booked, never
    graded (CONTEXT.md: avoids 追高殺低). Idempotent."""
    conn.execute(
        """
        UPDATE trader_predictions
        SET withdrawn = true, withdrawn_at = ?, withdrawn_reason = ?
        WHERE prediction_id = ? AND withdrawn = false
        """,
        [datetime.now(timezone.utc), reason, prediction_id],
    )


def predictions_pending_confirmation(
    conn: duckdb.DuckDBPyConnection, trade_date: date
) -> list[TraderPrediction]:
    """'pre_market'/'event' calls made on trade_date that the post_open
    decision point hasn't yet confirmed or withdrawn -- what
    orchestration.confirm_and_book acts on."""
    rows = conn.execute(
        f"""
        SELECT {_SELECT_COLUMNS} FROM trader_predictions
        WHERE trade_date = ? AND decision_point IN ('pre_market', 'event')
          AND confirmed_at IS NULL AND (withdrawn IS NULL OR withdrawn = false)
        ORDER BY symbol, trader_id
        """,
        [trade_date],
    ).fetchall()
    return [_row_to_prediction(r) for r in rows]


def todays_call_counts(conn: duckdb.DuckDBPyConnection, trade_date: date) -> tuple[int, int, int]:
    """(recorded, confirmed, withdrawn) among trade_date's decision-point-
    gated calls -- lets orchestration.run_decision_point assemble the
    post_close digest without threading state across the separate
    run_predictions/confirm_and_book calls earlier in the day."""
    row = conn.execute(
        """
        SELECT count(*),
               count(*) FILTER (WHERE confirmed_at IS NOT NULL),
               count(*) FILTER (WHERE withdrawn = true)
        FROM trader_predictions
        WHERE trade_date = ? AND decision_point IN ('pre_market', 'event')
        """,
        [trade_date],
    ).fetchone()
    return row[0], row[1], row[2]


_SELECT_COLUMNS = """
    prediction_id, trader_id, method_version, trade_date, symbol, sector, horizon,
    label_end_date, direction, conviction, rationale, invalidation, regime,
    entry_price, band_k, grade_vol, engine_payload, available_at, status,
    actual_price, actual_return, actual_label, outcome, option_structure, option_pnl,
    thesis, evidence_chain, rejected_alternatives, confidence_rationale,
    decision_point, confirmed_at, withdrawn, withdrawn_at, withdrawn_reason
"""


def _row_to_prediction(row: tuple) -> TraderPrediction:
    (
        prediction_id, trader_id, method_version, trade_date, symbol, sector, horizon,
        label_end_date, direction, conviction, rationale, invalidation, regime,
        entry_price, band_k, grade_vol, engine_payload_json, available_at, status,
        actual_price, actual_return, actual_label, outcome,
        option_structure_json, option_pnl,
        thesis, evidence_chain_json, rejected_alternatives, confidence_rationale,
        decision_point, confirmed_at, withdrawn, withdrawn_at, withdrawn_reason,
    ) = row
    return TraderPrediction(
        prediction_id=prediction_id, trader_id=trader_id, method_version=method_version,
        trade_date=trade_date, symbol=symbol, sector=sector, horizon=horizon,
        label_end_date=label_end_date, direction=direction, conviction=conviction,
        rationale=rationale, invalidation=invalidation, regime=regime,
        entry_price=entry_price, band_k=band_k, grade_vol=grade_vol,
        engine_payload=json.loads(engine_payload_json) if engine_payload_json else {},
        available_at=available_at, status=status, actual_price=actual_price,
        actual_return=actual_return, actual_label=actual_label, outcome=outcome,
        option_structure=json.loads(option_structure_json) if option_structure_json else None,
        option_pnl=option_pnl,
        thesis=thesis,
        evidence_chain=json.loads(evidence_chain_json) if evidence_chain_json else None,
        rejected_alternatives=rejected_alternatives, confidence_rationale=confidence_rationale,
        decision_point=decision_point, confirmed_at=confirmed_at,
        withdrawn=bool(withdrawn), withdrawn_at=withdrawn_at, withdrawn_reason=withdrawn_reason,
    )


def get_trader_prediction(conn: duckdb.DuckDBPyConnection, prediction_id: str) -> TraderPrediction | None:
    row = conn.execute(
        f"SELECT {_SELECT_COLUMNS} FROM trader_predictions WHERE prediction_id = ?",
        [prediction_id],
    ).fetchone()
    return _row_to_prediction(row) if row else None


def list_pending_trader_predictions(
    conn: duckdb.DuckDBPyConnection, *, as_of: date | None = None
) -> list[TraderPrediction]:
    """Matured (label_end_date <= as_of) but ungraded predictions -- what
    `grade` acts on. Excludes withdrawn calls (issue #7 P1): a call the
    post_open decision point withdrew never became a real opinion the trader
    stood behind, so it must never be graded -- see migration 040."""
    rows = conn.execute(
        f"""
        SELECT {_SELECT_COLUMNS} FROM trader_predictions
        WHERE status = 'pending' AND label_end_date <= ?
          AND (withdrawn IS NULL OR withdrawn = false)
        ORDER BY label_end_date, trader_id, symbol
        """,
        [as_of or date.today()],
    ).fetchall()
    return [_row_to_prediction(r) for r in rows]


def band_for(prediction: TraderPrediction) -> float:
    """The one band definition, reused from daily_predictions.band with this
    row's stored grade_vol -- identical ruler for every trader. Public (not
    underscore-prefixed): orchestration.confirm_and_book also needs this same
    ruler to check whether a live quote has already moved past it."""
    return _volatility_band(
        {"realized_vol_20d": prediction.grade_vol}, band_k=prediction.band_k, horizon=prediction.horizon
    )


def grade_trader_prediction(
    conn: duckdb.DuckDBPyConnection, prediction_id: str, *, actual_price: float
) -> None:
    prediction = get_trader_prediction(conn, prediction_id)
    if prediction is None:
        raise ValueError(f"unknown prediction_id: {prediction_id!r}")
    if prediction.status == "graded":
        raise ValueError(f"prediction {prediction_id!r} is already graded")

    actual_return = actual_price / prediction.entry_price - 1.0
    band = band_for(prediction)
    if actual_return > band:
        actual_label = "up"
    elif actual_return < -band:
        actual_label = "down"
    else:
        actual_label = "range"

    outcome = "win" if actual_label == prediction.direction else "loss"

    conn.execute(
        """
        UPDATE trader_predictions
        SET status = 'graded', actual_price = ?, actual_return = ?,
            actual_label = ?, outcome = ?, graded_at = ?
        WHERE prediction_id = ?
        """,
        [actual_price, actual_return, actual_label, outcome, datetime.now(timezone.utc), prediction_id],
    )


def set_option_pnl(conn: duckdb.DuckDBPyConnection, prediction_id: str, *, option_pnl: float) -> None:
    """Write the option_pnl computed by league/grading_options.py. Separate
    from grade_trader_prediction (which sets the directional outcome) because
    the two are graded from different inputs (a volatility-band label vs an
    actual repriced option) and grading_options.py needs to run AFTER the
    directional grade has stamped label_end_date's actual_price, not instead
    of it."""
    conn.execute(
        "UPDATE trader_predictions SET option_pnl = ? WHERE prediction_id = ?",
        [option_pnl, prediction_id],
    )


def graded_predictions(
    conn: duckdb.DuckDBPyConnection, *, trader_id: str | None = None
) -> list[TraderPrediction]:
    query = f"SELECT {_SELECT_COLUMNS} FROM trader_predictions WHERE status = 'graded'"
    params: list = []
    if trader_id:
        query += " AND trader_id = ?"
        params.append(trader_id)
    query += " ORDER BY trade_date, symbol, trader_id"
    return [_row_to_prediction(r) for r in conn.execute(query, params).fetchall()]


def predictions_on_date(
    conn: duckdb.DuckDBPyConnection, trade_date: date, *, symbol: str | None = None
) -> list[TraderPrediction]:
    query = f"SELECT {_SELECT_COLUMNS} FROM trader_predictions WHERE trade_date = ?"
    params: list = [trade_date]
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol.upper())
    query += " ORDER BY symbol, trader_id"
    return [_row_to_prediction(r) for r in conn.execute(query, params).fetchall()]


def list_predictions_for_trader(
    conn: duckdb.DuckDBPyConnection, trader_id: str
) -> list[TraderPrediction]:
    """Every prediction (any status) for one trader, oldest first -- the raw
    material league/ledger.py's backfill replays chronologically to rebuild a
    trader's paper-trading account from scratch."""
    query = f"SELECT {_SELECT_COLUMNS} FROM trader_predictions WHERE trader_id = ? ORDER BY trade_date, symbol"
    return [_row_to_prediction(r) for r in conn.execute(query, [trader_id]).fetchall()]
