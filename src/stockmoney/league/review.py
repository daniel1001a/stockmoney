"""Per-trader review (generalized A4) + cross-trader divergence (CLAUDE.md
section 8). Read-only side-branch: reads graded trader_predictions, writes
trader_review_log / trader_divergence_log / (proposals), never touches a
training path.

It deliberately REUSES attribution.py's factor decomposition, event matching,
verdict classification, and (for the Chartist) feature-candidate proposal --
a TraderPrediction exposes exactly the attributes those functions read
(.actual_return/.trade_date/.label_end_date/.sector/.symbol/.outcome/
.actual_label/.predicted_direction/.proba), so there is one attribution
definition, not a forked copy.

Evolution discipline (rearchitecture doc section 2): when a trader shows a
systematic failure pattern, the review emits a `proposed` method update for a
human to weigh -- it never auto-changes the trader's method.
"""
from __future__ import annotations

import duckdb

from stockmoney.data import trader_methods
from stockmoney.data import trader_predictions as tp
from stockmoney.data import trader_review
from stockmoney.data.attribution import (
    WRONG_SIGNAL_EXISTED,
    classify_verdict,
    decompose_return,
    match_events,
    propose_feature_candidate,
)

__all__ = ["run_review"]

# A trader with at least this many wrong_signal_existed verdicts under its
# current method version gets a (single, idempotent) method-update proposal.
# v1 heuristic -- conservative so we don't spam proposals off thin history.
METHOD_PROPOSAL_MIN_CASES = 3


def run_review(conn: duckdb.DuckDBPyConnection) -> dict:
    """Attribute every graded trader prediction not yet reviewed, then rebuild
    cross-trader divergence. Idempotent: re-running only reviews new graded
    rows and refreshes divergence in place. Returns a summary."""
    summary = {"reviewed": 0, "skipped": 0, "candidates_proposed": 0, "method_proposals": 0}

    for p in tp.graded_predictions(conn):
        if trader_review.has_review(
            conn, review_date=p.trade_date, symbol=p.symbol,
            trader_id=p.trader_id, method_version=p.method_version,
        ):
            continue
        decomp = decompose_return(conn, p)
        if decomp is None:
            summary["skipped"] += 1
            continue
        event_tags = match_events(conn, p)
        verdict = classify_verdict(p, decomp, event_tags)
        trader_review.record_review(
            conn,
            review_date=p.trade_date, symbol=p.symbol, trader_id=p.trader_id,
            method_version=p.method_version, predicted_direction=p.direction,
            predicted_confidence=p.conviction, actual_return=p.actual_return,
            attr_macro=decomp.attr_macro, attr_sector=decomp.attr_sector,
            attr_idiosyncratic=decomp.attr_idiosyncratic, event_tags=event_tags,
            verdict_class=verdict,
        )
        summary["reviewed"] += 1

        # A model-feature gap only makes sense for the Chartist (its engine is
        # the 6-feature model); reuse attribution's existing candidate proposal.
        if verdict == WRONG_SIGNAL_EXISTED and p.trader_id == "chartist":
            if propose_feature_candidate(conn, p, decomp):
                summary["candidates_proposed"] += 1

    summary["method_proposals"] = _maybe_propose_method_updates(conn)
    _rebuild_divergence(conn)
    return summary


def _maybe_propose_method_updates(conn: duckdb.DuckDBPyConnection) -> int:
    """For each trader whose current method has accumulated enough
    wrong_signal_existed verdicts, emit one human-reviewed method-update
    proposal. Idempotent (propose_method_update dedups per day)."""
    rows = conn.execute(
        """
        SELECT trader_id, method_version, count(*) AS n, max(review_date) AS last_date
        FROM trader_review_log
        WHERE verdict_class = ?
        GROUP BY trader_id, method_version
        HAVING count(*) >= ?
        """,
        [WRONG_SIGNAL_EXISTED, METHOD_PROPOSAL_MIN_CASES],
    ).fetchall()
    proposed = 0
    for trader_id, method_version, n, last_date in rows:
        current = trader_methods.current_method_version(conn, trader_id)
        if current != method_version:
            continue  # pattern is against a superseded method; the human already moved on
        rationale = (
            f"{n} 'wrong_signal_existed' verdicts under {method_version}: the trader "
            f"repeatedly missed idiosyncratic moves a real signal existed for. Review "
            f"whether the method should incorporate a new signal source or reweight."
        )
        pid = trader_methods.propose_method_update(
            conn, trader_id=trader_id, from_version=method_version,
            rationale=rationale, source_review_date=last_date,
        )
        if pid:
            proposed += 1
    return proposed


def _rebuild_divergence(conn: duckdb.DuckDBPyConnection) -> None:
    """For every (day, symbol) at least two traders called, record each
    trader's direction + whether another trader disagreed + (once graded) who
    was right. This is the section-8 signal: disagreement is information."""
    contested = conn.execute(
        """
        SELECT trade_date, symbol FROM trader_predictions
        GROUP BY trade_date, symbol
        HAVING count(DISTINCT trader_id) >= 2
        """
    ).fetchall()
    for trade_date, symbol in contested:
        preds = tp.predictions_on_date(conn, trade_date, symbol=symbol)
        for p in preds:
            disagreed = any(q.direction != p.direction for q in preds if q.trader_id != p.trader_id)
            was_right = (p.outcome == "win") if (p.status == "graded" and p.outcome) else None
            trader_review.upsert_divergence(
                conn, trade_date=trade_date, symbol=symbol, trader_id=p.trader_id,
                direction=p.direction, conviction=p.conviction,
                disagreed=disagreed, was_right=was_right,
            )
