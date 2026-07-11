"""EV trading gate (CLAUDE.md section 6): only signal a trade as tradeable when
today's expected value sits in the top quartile of its own recent history.

    EV = P(win) x avg_win_size - P(loss) x avg_loss_size - transaction_cost

Built directly on module A's `WalkForwardResult` — a "trade" is any OOS day
where the direction model's argmax prediction is not RANGE (position != 0),
using the same {-1,0,+1} convention already established in
`metrics.strategy_net_returns`.

Point-in-time discipline (the look-ahead risk in this module): a trade opened
on `trade_date` only "resolves" — its win/loss becomes knowable — once its
`label_end_date` (trade_date + horizon trading days) has passed. Estimating
P(win)/avg_win/avg_loss "as of" day d must therefore only use trades with
label_end_date < d. This is the direct analogue of module A's purge logic
(`walk_forward.make_folds`), applied here to a rolling statistics estimate
instead of a train/test split.

Two separate windows, deliberately not the same window:
- An **expanding** window (all resolved trades from the start up to d) for
  estimating P(win)/avg_win/avg_loss — stable, low-variance statistics.
- A **rolling 90-trading-day** window over the resulting daily EV *values*
  (not the underlying trades) for the percentile gate in `run_ev_gate`.
Reusing one rolling window for both would make EV[d] and EV[d-1] share nearly
all their underlying trades, so "is today's EV in the top quartile of the last
90" would be comparing a value against a distribution built mostly from
itself. CLAUDE.md doesn't fully specify this nested-window structure (window
length/threshold are explicitly listed as v1 starting points to calibrate via
backtest in section 16); this file's docstrings/comments are the record of the
interpretation chosen.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np

from stockmoney.models.walk_forward import WalkForwardResult

MIN_RESOLVED_TRADES = 10
DEFAULT_WINDOW = 90            # CLAUDE.md section 6 v1 starting point
DEFAULT_QUANTILE = 0.75        # "top 25%" -> >= 75th percentile
DEFAULT_COST_BPS = 5.0


@dataclass
class TradeOutcome:
    trade_date: date
    label_end_date: date
    position: int          # -1 (short/down) or +1 (long/up); position==0 is never a trade
    pnl_gross: float        # position * fwd_return, BEFORE transaction cost


@dataclass
class TrailingStats:
    n_win: int
    n_loss: int
    p_win: float
    avg_win: float          # mean gross pnl of winning trades (positive)
    avg_loss: float         # mean |gross pnl| of losing trades (positive)


def derive_trade_outcomes(result: WalkForwardResult) -> list[TradeOutcome]:
    outcomes = []
    for trade_date, label_end_date, proba, fwd_return in zip(
        result.trade_dates, result.label_end_dates, result.proba, result.fwd_return
    ):
        position = int(proba.argmax()) - 1
        if position == 0:
            continue
        outcomes.append(
            TradeOutcome(
                trade_date=trade_date,
                label_end_date=label_end_date,
                position=position,
                pnl_gross=position * float(fwd_return),
            )
        )
    return sorted(outcomes, key=lambda o: o.trade_date)


def expanding_stats_asof(
    outcomes: list[TradeOutcome], asof_date: date, *, min_trades: int = MIN_RESOLVED_TRADES
) -> TrailingStats | None:
    """Stats from every trade resolved strictly before `asof_date`. Returns
    None if fewer than `min_trades` resolved trades exist yet (insufficient
    sample -> caller should treat as "not tradeable", never "assume an edge")."""
    resolved = [o for o in outcomes if o.label_end_date < asof_date]
    if len(resolved) < min_trades:
        return None

    wins = [o.pnl_gross for o in resolved if o.pnl_gross > 0]
    losses = [-o.pnl_gross for o in resolved if o.pnl_gross <= 0]
    n_win, n_loss = len(wins), len(losses)
    if n_win == 0 or n_loss == 0:
        return None  # can't estimate both sides of the distribution yet

    return TrailingStats(
        n_win=n_win,
        n_loss=n_loss,
        p_win=n_win / (n_win + n_loss),
        avg_win=sum(wins) / n_win,
        avg_loss=sum(losses) / n_loss,
    )


def compute_ev(stats: TrailingStats, *, cost_bps: float) -> float:
    p_loss = 1.0 - stats.p_win
    cost = cost_bps / 1e4
    return stats.p_win * stats.avg_win - p_loss * stats.avg_loss - cost


@dataclass
class EVGateResult:
    trade_date: date
    ev: float | None            # None if too few resolved trades to estimate stats yet
    threshold: float | None     # None if the 90-EV reference window isn't full yet
    tradeable: bool


def run_ev_gate(
    outcomes: list[TradeOutcome],
    *,
    window: int = DEFAULT_WINDOW,
    quantile: float = DEFAULT_QUANTILE,
    cost_bps: float = DEFAULT_COST_BPS,
    min_trades: int = MIN_RESOLVED_TRADES,
) -> list[EVGateResult]:
    """Walk the trades in date order, computing EV[d] from expanding
    point-in-time stats, then gating on whether EV[d] sits at/above the
    `quantile` percentile of the most recent `window` *previously computed*
    EV values (never including EV[d] itself). Requires a full window of prior
    EV history before gating turns on — early trades default to
    `tradeable=False` rather than being let through for free.
    """
    ordered = sorted(outcomes, key=lambda o: o.trade_date)
    ev_history: list[float] = []
    results: list[EVGateResult] = []

    for o in ordered:
        stats = expanding_stats_asof(ordered, o.trade_date, min_trades=min_trades)
        ev = compute_ev(stats, cost_bps=cost_bps) if stats is not None else None

        threshold: float | None = None
        tradeable = False
        if ev is not None and len(ev_history) >= window:
            recent = ev_history[-window:]
            threshold = float(np.percentile(recent, quantile * 100))
            tradeable = ev >= threshold

        results.append(
            EVGateResult(trade_date=o.trade_date, ev=ev, threshold=threshold, tradeable=tradeable)
        )
        if ev is not None:
            ev_history.append(ev)

    return results
