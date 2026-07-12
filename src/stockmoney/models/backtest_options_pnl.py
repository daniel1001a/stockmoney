"""Options-P&L backtest (Part 1D of ~/.claude/plans/buzzing-yawning-squid.md).

The options analogue of backtest_ev_gate.py. Where that module measures the EV
gate against the *underlying's* forward return, this one closes the loop to the
thing actually traded: each OOS directional signal is turned into a concrete
option (option_selection.select_option), that option is repriced to the exit
(options_pnl.option_return), and the resulting *option* return -- not the
underlying return -- is what gets reported and fed into the EV gate / Kelly.

This is the number that answers "does this signal make money as options", net of
theta, the entry->exit move, and the bid-ask spread. A positive underlying
Sharpe (metrics.py's toy strategy) can coexist with a losing options book; only
this backtest can tell them apart.

Two honesty caveats, both loud on purpose (CLAUDE.md section 2's "self-estimated
v1, validate before paying for a real feed" applies to the whole options layer):

1. Entry-IV proxy. iv_surface_daily only accumulates going forward (it has no
   deep history yet), so an 8-year backtest has no real historical IV to price
   entries with. `entry_iv = realized_vol_20d * IV_RV_RATIO` stands in -- IV
   typically trades a little above realized vol, so a ~1.1 ratio is a defensible
   v1 placeholder. It is a *proxy*, swapped for real iv_surface_daily history
   once enough has accumulated; `build_option_outcomes` takes entry_iv as an
   argument precisely so the proxy lives in one place (main) and the assembly
   logic never bakes it in.
2. Constant-IV exit (OptionExit.iv=None). The exit is repriced at the entry IV,
   isolating the directional + theta effect and needing no future IV. A separate
   vega scenario (realized exit IV) is future work once exit IVs exist in
   history; feeding a future IV in as if known at entry would be look-ahead.

Look-ahead discipline is otherwise inherited wholesale from run_walk_forward
(purge/embargo) and the point-in-time EV gate; this module adds no new training.

Usage:
    uv run python -m stockmoney.models.backtest_options_pnl
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date

import numpy as np

from stockmoney.models.ev_gate import (
    DEFAULT_COST_BPS,
    MIN_RESOLVED_TRADES,
    TradeOutcome,
    run_ev_gate,
)
from stockmoney.models.option_selection import SelectionParams, select_option
from stockmoney.models.options_pnl import (
    DEFAULT_RATE,
    DEFAULT_SPREAD_PCT,
    OptionExit,
    option_return,
)

HORIZON = 5
N_FOLDS = 5
EV_WINDOW = 90
IV_RV_RATIO = 1.1  # entry-IV proxy multiplier over realized vol (v1; see module docstring)


@dataclass(frozen=True)
class OptionTrade:
    """One realized option trade in the backtest: the EV gate only needs
    (trade_date, label_end_date, position, pnl_gross), but we also carry the
    regime so results can be reported per regime (CLAUDE.md section 12)."""
    trade_date: date
    label_end_date: date
    position: int          # +1 bought a call (up signal), -1 bought a put (down signal)
    pnl_gross: float        # net option return on premium, AFTER bid-ask spread
    regime: int


def build_option_outcomes(
    trade_dates: list[date],
    label_end_dates: list[date],
    proba: np.ndarray,          # (n, 3) P(down/range/up)
    fwd_return: np.ndarray,     # (n,) underlying forward return over the horizon
    entry_spot: np.ndarray,     # (n,) underlying close at entry
    entry_iv: np.ndarray,       # (n,) IV to price the entry with (proxy applied by caller)
    regime: np.ndarray,         # (n,) regime label per row
    *,
    selection: SelectionParams = SelectionParams(),
    spread_pct: float = DEFAULT_SPREAD_PCT,
    r: float = DEFAULT_RATE,
) -> list[OptionTrade]:
    """Pure assembly: map each row's direction to an option, reprice it to the
    exit spot at the entry IV (constant-IV), and record the net option return.
    RANGE rows (and rows with an unusable entry IV) produce no trade -- exactly
    the ev_gate position==0 convention."""
    trades: list[OptionTrade] = []
    for i, (td, led) in enumerate(zip(trade_dates, label_end_dates)):
        direction = int(proba[i].argmax())
        entry = select_option(direction, float(entry_spot[i]), float(entry_iv[i]), params=selection, r=r)
        if entry is None:
            continue
        days_held = max((led - td).days, 1)
        exit_spot = float(entry_spot[i]) * (1.0 + float(fwd_return[i]))
        ret = option_return(
            entry, OptionExit(spot=exit_spot, iv=None, days_held=days_held), r=r, spread_pct=spread_pct
        )
        trades.append(
            OptionTrade(
                trade_date=td,
                label_end_date=led,
                position=1 if entry.is_call else -1,
                pnl_gross=ret,
                regime=int(regime[i]),
            )
        )
    return trades


def to_ev_outcomes(trades: list[OptionTrade]) -> list[TradeOutcome]:
    """Adapt to the ev_gate's TradeOutcome so run_ev_gate / expanding_stats_asof
    work unchanged -- the whole point of matching pnl_gross's shape (1C)."""
    return [
        TradeOutcome(
            trade_date=t.trade_date,
            label_end_date=t.label_end_date,
            position=t.position,
            pnl_gross=t.pnl_gross,
        )
        for t in trades
    ]


def _group_stats(pnls: list[float]) -> str:
    if not pnls:
        return "n=0"
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    return (
        f"n={len(pnls)} win_rate={len(wins) / len(pnls):.3f} "
        f"mean_ret={statistics.fmean(pnls):+.4f} "
        f"avg_win={statistics.fmean(wins) if wins else 0.0:+.4f} "
        f"avg_loss={statistics.fmean(losses) if losses else 0.0:+.4f}"
    )


def _report(trades: list[OptionTrade]) -> None:
    from stockmoney.models.metrics import bootstrap_mean_ci

    pnls = [t.pnl_gross for t in trades]
    print(f"\noption trades (non-range signals): {len(trades)}")
    print(f"  overall: {_group_stats(pnls)}")
    if pnls:
        mean, lo, hi = bootstrap_mean_ci(np.array(pnls))
        verdict = "PROFITABLE" if lo > 0 else ("LOSING" if hi < 0 else "not distinguishable from 0")
        print(f"  mean option return 95% CI: [{lo:+.4f}, {hi:+.4f}] -> {verdict}")
    for reg in sorted({t.regime for t in trades}):
        print(f"  regime {reg}: {_group_stats([t.pnl_gross for t in trades if t.regime == reg])}")


def main(db_path: str | None = None) -> None:
    from stockmoney.data.db import DEFAULT_DB_PATH, get_connection
    from stockmoney.models.direction import LogisticDirectionModel
    from stockmoney.models.feature_matrix import build_feature_matrix, to_dataset
    from stockmoney.models.regime import KMeansGMMTrack
    from stockmoney.models.walk_forward import run_walk_forward

    conn = get_connection(db_path or DEFAULT_DB_PATH)
    matrix = build_feature_matrix(conn, target_symbol="SOXL", sector="semiconductor", horizon=HORIZON)
    if matrix.height == 0:
        print("empty feature matrix -- build/backfill the DB first (see HANDOFF.md).")
        conn.close()
        return
    dataset = to_dataset(matrix)

    wf = run_walk_forward(
        dataset,
        lambda: KMeansGMMTrack(method="gmm", n_regimes=3, seed=0),
        lambda: LogisticDirectionModel(seed=0, calibrate="sigmoid"),
        n_folds=N_FOLDS,
    )

    # Per-date entry spot (close) and realized-vol -> entry-IV proxy, aligned to
    # the OOS trade_dates by lookup (walk-forward returns only the test rows).
    rv_by_date = {d: float(x[0]) for d, x in zip(dataset.trade_dates, dataset.X)}  # realized_vol_20d is column 0
    close_by_date = _closes_by_date(conn)
    entry_spot = np.array([close_by_date[d] for d in wf.trade_dates])
    entry_iv = np.array([rv_by_date[d] * IV_RV_RATIO for d in wf.trade_dates])

    trades = build_option_outcomes(
        wf.trade_dates, wf.label_end_dates, wf.proba, wf.fwd_return, entry_spot, entry_iv, wf.regime
    )
    _report(trades)

    # EV gate applied to the *option* return distribution (1C): does gating on
    # top-quartile EV separate better option trades from worse ones?
    ev_outcomes = to_ev_outcomes(trades)
    gate = run_ev_gate(ev_outcomes, window=EV_WINDOW, cost_bps=DEFAULT_COST_BPS, min_trades=MIN_RESOLVED_TRADES)
    gated = [(t, g) for t, g in zip(trades, gate) if g.threshold is not None]
    passed = [t.pnl_gross for t, g in gated if g.tradeable]
    blocked = [t.pnl_gross for t, g in gated if not g.tradeable]
    print(f"\nEV gate on option returns (trades with a full {EV_WINDOW}-EV window: {len(gated)}):")
    print(f"  gate PASSED : {_group_stats(passed)}")
    print(f"  gate BLOCKED: {_group_stats(blocked)}")

    conn.close()


def _closes_by_date(conn) -> dict:
    from stockmoney.models.feature_matrix import _load_closes

    return {d: c for d, c in _load_closes(conn, "SOXL")}


if __name__ == "__main__":
    main()
