"""League table: each trader's scorecard, rolling + per-regime. This is the
output the foreman reads to decide whose call to surface and how big to size
it (the dynamic-league version of a meta-allocator).

Metrics, computed only over *graded* predictions so nothing forward-looking
leaks in:
- hit_rate: wins / directional (up|down) calls -- same "directional accuracy"
  the backtest and daily_predictions.win_rate_history report, so it's directly
  comparable to module A/B's own numbers.
- brier: mean((conviction - correct)^2) treating conviction as P(this call is
  right). Works identically for every trader regardless of engine, which is
  the whole point of the unified shape.
- avg_pnl / cum_pnl: signed realized return of directional calls (long on 'up',
  short on 'down'; 'range' has no directional exposure). Optional cost_bps.
- high_conviction_precision: hit rate among calls with conviction >= threshold
  -- CLAUDE.md section 12's "high-confidence-interval precision".
- option_win_rate / avg_option_pnl / cum_option_pnl (Wave D, IMPROVEMENT_PLAN.md
  §S3): the REAL option P&L of the concrete instrument option_bridge.py chose
  at entry (league/grading_options.py), scored alongside the underlying-return
  pnl above so a trader who is directionally right but loses to theta/IV-crush
  is visibly different from one who is right AND makes money as an option --
  exactly the gap a pure directional hit-rate can't see. Computed only over
  rows with a non-None option_pnl (a 'range' call, or a day with no usable
  entry IV, has none) -- honest None/0 when that bucket is empty, same as
  every other stat here.
"""
from __future__ import annotations

from datetime import date

import duckdb

from stockmoney.data import trader_predictions as tp
from stockmoney.data.traders import list_all_traders
from stockmoney.data.trader_predictions import TraderPrediction

DEFAULT_HIGH_CONVICTION = 0.6
DEFAULT_ROLLING_WINDOW = 20


def _is_directional(p: TraderPrediction) -> bool:
    return p.direction in ("up", "down")


def _signed_return(p: TraderPrediction, *, cost_bps: float) -> float:
    r = p.actual_return or 0.0
    signed = r if p.direction == "up" else -r
    return signed - cost_bps / 1e4


def compute_stats(
    preds: list[TraderPrediction], *, high_conviction: float = DEFAULT_HIGH_CONVICTION, cost_bps: float = 0.0
) -> dict:
    """Scorecard over a list of already-graded predictions. Returns zeros/None
    honestly when a bucket is empty rather than dividing by zero."""
    graded = [p for p in preds if p.status == "graded" and p.outcome is not None]
    directional = [p for p in graded if _is_directional(p)]

    n_graded = len(graded)
    n_dir = len(directional)
    wins_dir = sum(1 for p in directional if p.outcome == "win")

    brier = (
        sum((p.conviction - (1.0 if p.outcome == "win" else 0.0)) ** 2 for p in graded) / n_graded
        if n_graded else None
    )
    pnls = [_signed_return(p, cost_bps=cost_bps) for p in directional]
    high_conv = [p for p in graded if p.conviction >= high_conviction]
    high_conv_wins = sum(1 for p in high_conv if p.outcome == "win")

    option_pnls = [p.option_pnl for p in graded if p.option_pnl is not None]
    option_wins = sum(1 for pnl in option_pnls if pnl > 0)
    # "Right on direction but the option lost" -- theta/IV-crush, the exact
    # gap this wave closes (IMPROVEMENT_PLAN.md §S3's acceptance case).
    directional_wins_option_losses = sum(
        1 for p in directional if p.outcome == "win" and p.option_pnl is not None and p.option_pnl <= 0
    )

    return {
        "n_graded": n_graded,
        "n_directional": n_dir,
        "hit_rate": (wins_dir / n_dir) if n_dir else None,
        "brier": brier,
        "avg_pnl": (sum(pnls) / len(pnls)) if pnls else None,
        "cum_pnl": sum(pnls) if pnls else 0.0,
        "high_conviction_threshold": high_conviction,
        "high_conviction_n": len(high_conv),
        "high_conviction_precision": (high_conv_wins / len(high_conv)) if high_conv else None,
        "n_option_graded": len(option_pnls),
        "option_win_rate": (option_wins / len(option_pnls)) if option_pnls else None,
        "avg_option_pnl": (sum(option_pnls) / len(option_pnls)) if option_pnls else None,
        "cum_option_pnl": sum(option_pnls) if option_pnls else 0.0,
        "directional_win_option_loss_n": directional_wins_option_losses,
    }


def equity_curve(preds: list[TraderPrediction], *, cost_bps: float = 0.0) -> list[dict]:
    """Time-ordered cumulative P&L series for the Arena's 資金曲線
    (equity-curve) chart -- "who is winning, and by how much, over time".

    Ordered by `trade_date` (the day the call was made, not `label_end_date`
    when it matured/graded), so the curve reads left-to-right the way a
    trader would read their own daily blotter. Only settled rows
    (`status == 'graded'`) are included -- no look-ahead, matching
    `compute_stats`.

    Each point carries both P&L notions `compute_stats` already distinguishes:
    - directional pnl: `_signed_return` (same signed-return definition as
      `cum_pnl` there), 0-contribution for non-directional 'range' calls.
    - option pnl: the stored `option_pnl` (league/grading_options.py),
      0-contribution when None (range call, or no usable entry IV that day).
    The two running totals are accumulated over different denominators than
    `compute_stats` reports (every graded row here vs n_directional /
    n_option_graded there) -- that's intentional, an equity curve needs one
    continuous line, not a per-bucket average.
    """
    graded = [p for p in preds if p.status == "graded" and p.outcome is not None]
    graded.sort(key=lambda p: (p.trade_date, p.symbol))
    out: list[dict] = []
    cum_pnl = 0.0
    cum_option_pnl = 0.0
    for p in graded:
        pnl = _signed_return(p, cost_bps=cost_bps) if _is_directional(p) else 0.0
        option_pnl = p.option_pnl if p.option_pnl is not None else 0.0
        cum_pnl += pnl
        cum_option_pnl += option_pnl
        out.append({
            "trade_date": p.trade_date,
            "symbol": p.symbol,
            "direction": p.direction,
            "pnl": pnl,
            "cum_pnl": cum_pnl,
            "option_pnl": option_pnl,
            "cum_option_pnl": cum_option_pnl,
        })
    return out


def league_equity_curves(conn: duckdb.DuckDBPyConnection, *, cost_bps: float = 0.0) -> list[dict]:
    """Per-trader equity curves for every trader that has ever traded (active
    or retired), same roster as `league_table`. Honest empty `points: []`
    when a trader has no graded predictions yet rather than omitting them."""
    rows: list[dict] = []
    for trader in list_all_traders(conn):
        graded = tp.graded_predictions(conn, trader_id=trader.trader_id)
        rows.append({
            "trader_id": trader.trader_id,
            "name": trader.name,
            "philosophy": trader.philosophy,
            "active": trader.active,
            "points": equity_curve(graded, cost_bps=cost_bps),
        })
    return rows


def _by_regime(
    preds: list[TraderPrediction], *, high_conviction: float, cost_bps: float
) -> dict[str, dict]:
    buckets: dict[int, list[TraderPrediction]] = {}
    for p in preds:
        if p.regime is None:
            continue
        buckets.setdefault(p.regime, []).append(p)
    return {
        str(regime): compute_stats(rows, high_conviction=high_conviction, cost_bps=cost_bps)
        for regime, rows in sorted(buckets.items())
    }


def league_table(
    conn: duckdb.DuckDBPyConnection,
    *,
    window: int = DEFAULT_ROLLING_WINDOW,
    high_conviction: float = DEFAULT_HIGH_CONVICTION,
    cost_bps: float = 0.0,
    as_of: date | None = None,
) -> list[dict]:
    """Per-trader scorecard: overall + rolling(last `window` graded by
    label_end_date) + per-regime. Includes every trader that has ever traded,
    active or retired (retired traders' history stays visible)."""
    rows: list[dict] = []
    for trader in list_all_traders(conn):
        graded = tp.graded_predictions(conn, trader_id=trader.trader_id)
        graded.sort(key=lambda p: (p.label_end_date, p.symbol))
        rolling = graded[-window:] if window else graded
        rows.append({
            "trader_id": trader.trader_id,
            "name": trader.name,
            "philosophy": trader.philosophy,
            "active": trader.active,
            "overall": compute_stats(graded, high_conviction=high_conviction, cost_bps=cost_bps),
            "rolling": {"window": window, **compute_stats(rolling, high_conviction=high_conviction, cost_bps=cost_bps)},
            "by_regime": _by_regime(graded, high_conviction=high_conviction, cost_bps=cost_bps),
        })
    # Rank the table by rolling hit_rate (None sorts last), most accurate first.
    rows.sort(key=lambda r: (r["rolling"]["hit_rate"] is None, -(r["rolling"]["hit_rate"] or 0.0)))
    return rows
