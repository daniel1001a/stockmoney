"""Daily attribution / review engine (CLAUDE.md section 11).

A *read-only analysis side-branch*: it reads already-graded predictions and
already-realized prices, decomposes each resolved call's return into
macro/sector/idiosyncratic contributions, tags any events it can honestly
match, classifies the model's call into one of three verdicts, and -- only
for the "wrong, but a signal existed in hindsight" case -- proposes an
independently-verifiable *feature hypothesis* into `feature_candidates` for
human review.

Hard boundaries (CLAUDE.md sections 7 / 11 / 17):
- Output is ONLY ever a new feature *hypothesis*, never a revision to a past
  label. It never rewrites daily_predictions or feature_store.
- `attribution_log` / `feature_candidates` are NEVER read by any training
  path (feature_matrix / walk_forward / regime / direction / production) --
  same discretion/meta-layer isolation as daily_predictions. A candidate can
  only be `proposed`; a human, not this engine, may later `accept` it.
- Only *resolved* (graded) predictions are attributed: attribution_log rows
  are finished facts, like feature_store, never pending verdicts.

The factor decomposition is deliberately the same equal-weight, exclude-
leveraged-ETF, idiosyncratic-as-residual philosophy as
`features/dispersion.py` -- no rolling-beta regression in v1 (that needs its
own look-ahead care over how beta is estimated; left as a v2 upgrade). Only
same-window realized returns are used, so there is no forward-looking input.
"""
from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import duckdb

from stockmoney.data.daily_predictions import DailyPrediction, _row_to_prediction, _SELECT_COLUMNS
from stockmoney.data.db import MARKET_SYMBOL
from stockmoney.data.features.dispersion import SECTOR_MEMBERS
from stockmoney.data.features.gdelt_sentiment import AVGTONE_FEATURE
from stockmoney.data.features.gdelt_sentiment import FEATURE_VERSION as GDELT_FEATURE_VERSION
from stockmoney.data.positions import price_on_date

# Verdict classes (attribution_log.verdict_class) -- CLAUDE.md section 11.
RIGHT_REASON_RIGHT = "right_reason_right"
WRONG_NOISE = "wrong_noise"
WRONG_SIGNAL_EXISTED = "wrong_signal_existed"

# Single-name market cross-section (equal weight), excluding the leveraged
# semiconductor ETFs SOXL/SOXS whose 3x mechanics would distort a broad
# market proxy -- same exclusion dispersion.py makes.
MARKET_MEMBERS: list[str] = sorted({m for members in SECTOR_MEMBERS.values() for m in members})

# Calendar-day grace window when a horizon end lands on a market holiday --
# mirrors daily_prediction_cli.GRADE_LOOKUP_GRACE_DAYS so window returns line
# up with how the target's own actual_price was graded.
WINDOW_GRACE_DAYS = 5

# A directional miss is treated as "a real signal the model failed to catch"
# only when the idiosyncratic residual out-weighs the systematic parts the
# model already sees (macro + sector). v1 heuristic, tune as history grows.
IDIO_DOMINANCE_MARGIN = 1.0  # |idio| must exceed IDIO_DOMINANCE_MARGIN * (|macro| + |sector|)

# GDELT market-tone abnormality: window mean tone this many trailing std devs
# away from its trailing baseline gets a market event tag. v1 heuristic.
GDELT_ABNORMAL_Z = 2.0
GDELT_BASELINE_DAYS = 90


@dataclass
class Decomposition:
    """Additive by construction: macro + sector + idio == actual_return."""
    attr_macro: float
    attr_sector: float
    attr_idiosyncratic: float
    market_ret: float
    sector_ret: float


def _window_return(
    conn: duckdb.DuckDBPyConnection, symbol: str, start: date, end: date
) -> float | None:
    """Simple return of `symbol` over [start, end], deduped the same way
    grading looks prices up. Tolerates an `end` on a market holiday with the
    same forward grace window grading uses."""
    start_px = price_on_date(conn, symbol, start)
    if start_px is None or start_px <= 0:
        return None
    end_px = price_on_date(conn, symbol, end)
    if end_px is None:
        for offset in range(1, WINDOW_GRACE_DAYS + 1):
            end_px = price_on_date(conn, symbol, end + timedelta(days=offset))
            if end_px is not None:
                break
    if end_px is None:
        return None
    return end_px / start_px - 1.0


def _mean_member_return(
    conn: duckdb.DuckDBPyConnection, members: list[str], start: date, end: date
) -> float | None:
    rets = [r for m in members if (r := _window_return(conn, m, start, end)) is not None]
    return statistics.fmean(rets) if rets else None


def decompose_return(
    conn: duckdb.DuckDBPyConnection, prediction: DailyPrediction
) -> Decomposition | None:
    """Split the target's realized return into macro/sector/idiosyncratic.

    macro   = equal-weight market cross-section return (the "everything moved"
              part the model's macro features proxy)
    sector  = sector cross-section return in excess of market (orthogonalized
              so it isn't double-counted against macro)
    idio    = residual = actual_return - sector_ret (what's left after the
              stock's own sector average -- the same idiosyncratic notion
              dispersion.py uses)

    Uses the prediction's *stored* actual_return, so the three parts always
    sum to exactly the graded outcome. Returns None if there aren't enough
    member prices over the window to form the cross-sections (expected only on
    pathological/empty data, not in normal operation).
    """
    if prediction.actual_return is None:
        return None
    market_ret = _mean_member_return(conn, MARKET_MEMBERS, prediction.trade_date, prediction.label_end_date)
    sector_members = SECTOR_MEMBERS.get(prediction.sector, [])
    sector_ret = _mean_member_return(conn, sector_members, prediction.trade_date, prediction.label_end_date)
    if market_ret is None or sector_ret is None:
        return None

    attr_macro = market_ret
    attr_sector = sector_ret - market_ret
    attr_idiosyncratic = prediction.actual_return - sector_ret  # == actual - macro - sector
    return Decomposition(
        attr_macro=attr_macro,
        attr_sector=attr_sector,
        attr_idiosyncratic=attr_idiosyncratic,
        market_ret=market_ret,
        sector_ret=sector_ret,
    )


def _gdelt_market_tag(
    conn: duckdb.DuckDBPyConnection, start: date, end: date
) -> str | None:
    """Tag the window if market-wide GDELT tone deviated abnormally from its
    trailing baseline. This is the only event source with real data today;
    per-symbol/earnings tagging stays empty until those tables fill."""
    window = conn.execute(
        """
        SELECT feature_value FROM feature_store
        WHERE feature_name = ? AND feature_version = ? AND symbol = ?
          AND feature_date BETWEEN ? AND ? AND feature_value IS NOT NULL
        """,
        [AVGTONE_FEATURE, GDELT_FEATURE_VERSION, MARKET_SYMBOL, start, end],
    ).fetchall()
    baseline = conn.execute(
        """
        SELECT feature_value FROM feature_store
        WHERE feature_name = ? AND feature_version = ? AND symbol = ?
          AND feature_date BETWEEN ? AND ? AND feature_value IS NOT NULL
        """,
        [AVGTONE_FEATURE, GDELT_FEATURE_VERSION, MARKET_SYMBOL,
         start - timedelta(days=GDELT_BASELINE_DAYS), start - timedelta(days=1)],
    ).fetchall()
    if not window or len(baseline) < 2:
        return None
    window_mean = statistics.fmean(v[0] for v in window)
    base_vals = [v[0] for v in baseline]
    base_mean = statistics.fmean(base_vals)
    base_std = statistics.stdev(base_vals)
    if base_std <= 0:
        return None
    if abs(window_mean - base_mean) >= GDELT_ABNORMAL_Z * base_std:
        return "gdelt_tone_abnormal"
    return None


def _stock_news_tag(
    conn: duckdb.DuckDBPyConnection, symbol: str, start: date, end: date
) -> str | None:
    """Tag if any classified news/social item was attributed to this symbol
    within the window (via scan_classifications, which the classification pass
    populates with item->symbol links). Sparse today, wires up automatically
    as that table fills."""
    # symbols is a JSON-array text like '["META"]'; match the quoted ticker so
    # 'AMD' can't spuriously hit 'AMDX'. Parameterized -- injection-safe.
    row = conn.execute(
        """
        SELECT count(*) FROM scan_classifications
        WHERE symbols IS NOT NULL
          AND symbols LIKE ?
          AND CAST(processed_at AS DATE) BETWEEN ? AND ?
        """,
        [f'%"{symbol.upper()}"%', start, end],
    ).fetchone()
    return "news_flagged" if row and row[0] > 0 else None


def match_events(conn: duckdb.DuckDBPyConnection, prediction: DailyPrediction) -> list[str]:
    """Honest event tags for the prediction's window. Returns [] when nothing
    matches -- never fabricates an event. `event_calendar` (earnings) is empty
    today so no earnings tag is emitted yet."""
    tags: list[str] = []
    market_tag = _gdelt_market_tag(conn, prediction.trade_date, prediction.label_end_date)
    if market_tag:
        tags.append(market_tag)
    stock_tag = _stock_news_tag(conn, prediction.symbol, prediction.trade_date, prediction.label_end_date)
    if stock_tag:
        tags.append(stock_tag)
    return tags


def classify_verdict(
    prediction: DailyPrediction, decomp: Decomposition, event_tags: list[str]
) -> str:
    """Three-way verdict (CLAUDE.md section 11).

    - win -> right_reason_right (v1: 'reason' can't be fully verified until the
      event layer has data, so a correct directional/range call is the honest
      'model right' bucket)
    - loss whose actual move stayed inside the model's own band (actual_label
      == 'range') -> wrong_noise: there was no real move to catch, so any
      "signal" would have been sub-threshold noise
    - loss on a genuine directional move the model missed -> wrong_signal_existed
      iff the idiosyncratic residual dominates the systematic parts the model
      already sees, OR a stock-specific event tag matched; else wrong_noise
      (the move was explained by macro/sector features the model had).
    """
    if prediction.outcome == "win":
        return RIGHT_REASON_RIGHT
    if prediction.actual_label == "range":
        return WRONG_NOISE
    systematic = abs(decomp.attr_macro) + abs(decomp.attr_sector)
    idio_dominates = abs(decomp.attr_idiosyncratic) > IDIO_DOMINANCE_MARGIN * systematic
    stock_signal = "news_flagged" in event_tags
    if idio_dominates or stock_signal:
        return WRONG_SIGNAL_EXISTED
    return WRONG_NOISE


def _has_attribution(
    conn: duckdb.DuckDBPyConnection, *, attribution_date: date, symbol: str, model_version: str
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM attribution_log WHERE attribution_date = ? AND symbol = ? AND model_version = ?",
        [attribution_date, symbol, model_version],
    ).fetchone()
    return row is not None


def _proba_confidence(prediction: DailyPrediction) -> float:
    """Confidence attached to the predicted direction = that class's probability."""
    down, rng, up = prediction.proba
    return {"down": down, "range": rng, "up": up}[prediction.predicted_direction]


def propose_feature_candidate(
    conn: duckdb.DuckDBPyConnection, prediction: DailyPrediction, decomp: Decomposition
) -> str | None:
    """Emit a `proposed` feature hypothesis for a wrong_signal_existed case.
    Idempotent per (source_attribution_date, symbol): re-running attribution
    for the same resolved prediction won't stack duplicate candidates."""
    existing = conn.execute(
        """
        SELECT candidate_id FROM feature_candidates
        WHERE source_attribution_date = ? AND proposed_feature_name = ?
        """,
        [prediction.trade_date, f"idio_signal::{prediction.symbol}"],
    ).fetchone()
    if existing is not None:
        return existing[0]

    candidate_id = str(uuid.uuid4())
    hypothesis = (
        f"{prediction.symbol} returned {prediction.actual_return:+.2%} over "
        f"{prediction.trade_date}->{prediction.label_end_date}, dominated by an "
        f"idiosyncratic residual ({decomp.attr_idiosyncratic:+.2%}) that none of the "
        f"current 6 model features captured (macro {decomp.attr_macro:+.2%}, "
        f"sector {decomp.attr_sector:+.2%}). Investigate an independently-verifiable "
        f"per-symbol signal around this window (news flow, options-flow/skew, earnings) "
        f"available at prediction time."
    )
    conn.execute(
        """
        INSERT INTO feature_candidates (
            candidate_id, proposed_date, proposed_feature_name, hypothesis,
            source_attribution_date, status, created_at
        ) VALUES (?, ?, ?, ?, ?, 'proposed', ?)
        """,
        [candidate_id, date.today(), f"idio_signal::{prediction.symbol}", hypothesis,
         prediction.trade_date, datetime.now(timezone.utc)],
    )
    return candidate_id


def record_attribution(
    conn: duckdb.DuckDBPyConnection, prediction: DailyPrediction
) -> str | None:
    """Attribute one resolved prediction: decompose, tag, classify, write the
    attribution_log row (+ a feature candidate for wrong_signal_existed).

    Returns the verdict written, or None if skipped (not graded, or the window
    cross-sections couldn't be formed). Idempotent per (attribution_date,
    symbol, model_version) -- a prediction already attributed is skipped.
    """
    if prediction.status != "graded" or prediction.actual_return is None:
        return None
    if _has_attribution(
        conn, attribution_date=prediction.trade_date,
        symbol=prediction.symbol, model_version=prediction.model_version,
    ):
        return None

    decomp = decompose_return(conn, prediction)
    if decomp is None:
        return None
    event_tags = match_events(conn, prediction)
    verdict = classify_verdict(prediction, decomp, event_tags)

    conn.execute(
        """
        INSERT INTO attribution_log (
            attribution_date, symbol, predicted_direction, predicted_confidence,
            actual_return, attr_macro, attr_sector, attr_idiosyncratic,
            event_tags, verdict_class, model_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            prediction.trade_date, prediction.symbol, prediction.predicted_direction,
            _proba_confidence(prediction), prediction.actual_return,
            decomp.attr_macro, decomp.attr_sector, decomp.attr_idiosyncratic,
            ",".join(event_tags), verdict, prediction.model_version,
            datetime.now(timezone.utc),
        ],
    )

    if verdict == WRONG_SIGNAL_EXISTED:
        propose_feature_candidate(conn, prediction, decomp)
    return verdict


def _graded_predictions(conn: duckdb.DuckDBPyConnection) -> list[DailyPrediction]:
    rows = conn.execute(
        f"SELECT {_SELECT_COLUMNS} FROM daily_predictions WHERE status = 'graded' "
        f"ORDER BY trade_date, symbol"
    ).fetchall()
    return [_row_to_prediction(r) for r in rows]


def run_attribution(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Attribute every graded prediction that doesn't yet have an
    attribution_log row. Safe to re-run nightly (idempotent). Returns a
    summary count per verdict, plus candidates proposed."""
    summary = {RIGHT_REASON_RIGHT: 0, WRONG_NOISE: 0, WRONG_SIGNAL_EXISTED: 0, "skipped": 0}
    candidates_before = conn.execute("SELECT count(*) FROM feature_candidates").fetchone()[0]
    for prediction in _graded_predictions(conn):
        verdict = record_attribution(conn, prediction)
        if verdict is None:
            summary["skipped"] += 1
        else:
            summary[verdict] += 1
    candidates_after = conn.execute("SELECT count(*) FROM feature_candidates").fetchone()[0]
    summary["candidates_proposed"] = candidates_after - candidates_before
    return summary
