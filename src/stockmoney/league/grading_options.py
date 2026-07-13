"""Grade a trader_predictions row's REAL option P&L (IMPROVEMENT_PLAN.md §S3),
the league analogue of backtest_options_pnl.py's build_option_outcomes but for
one already-graded live prediction instead of a batch of walk-forward OOS
rows.

Reprices the EXACT contract chosen at entry (option_structure, stored verbatim
at prediction time by league/option_bridge.py -- never re-derived here, so
grading can't pick a "better" contract with hindsight) to the label_end_date
exit spot, holding IV constant at the entry level (options_pnl.py's documented
convention: isolates the directional + theta effect, needs no future IV).

Called from orchestration.grade_matured() right after the existing directional
grade (tp.grade_trader_prediction), reusing the SAME actual_price that call
already looked up -- no duplicate price queries.
"""
from __future__ import annotations

import duckdb

from stockmoney.data import trader_predictions as tp
from stockmoney.models.options_pnl import DEFAULT_RATE, DEFAULT_SPREAD_PCT, OptionEntry, OptionExit, option_return


def grade_option_pnl(
    conn: duckdb.DuckDBPyConnection, prediction_id: str, *, exit_spot: float, days_held: int,
    r: float = DEFAULT_RATE, spread_pct: float = DEFAULT_SPREAD_PCT,
) -> float | None:
    """Prices the prediction's stored option_structure to exit and writes
    option_pnl. No-op (returns None, writes nothing) if the prediction has no
    option_structure -- a 'range' call, or no usable entry IV existed that
    day (option_bridge.build_option_structure's own None cases)."""
    prediction = tp.get_trader_prediction(conn, prediction_id)
    if prediction is None:
        raise ValueError(f"unknown prediction_id: {prediction_id!r}")
    if prediction.option_structure is None:
        return None

    s = prediction.option_structure
    entry = OptionEntry(
        spot=s["spot"], strike=s["strike"], is_call=s["is_call"], iv=s["iv"],
        t_years=s["t_years"], side=s["side"],
    )
    exit_ = OptionExit(spot=exit_spot, iv=None, days_held=max(days_held, 1))
    pnl = option_return(entry, exit_, r=r, spread_pct=spread_pct)

    tp.set_option_pnl(conn, prediction_id, option_pnl=pnl)
    return pnl
