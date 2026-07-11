"""Module B vertical-slice entry point: run the EV gate + Kelly sizing over
module A's real 8-year SOXL/semiconductor walk-forward output, and check
whether the gate's "tradeable" flag actually separates better-realized trades
from worse ones out of sample — the only honest way to validate a filter like
this (CLAUDE.md section 12: report the split, don't just assert it works).

Usage:
    uv run python -m stockmoney.models.backtest_ev_gate
"""
from __future__ import annotations

import statistics

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection
from stockmoney.models.direction import LogisticDirectionModel
from stockmoney.models.ev_gate import derive_trade_outcomes, expanding_stats_asof, run_ev_gate
from stockmoney.models.feature_matrix import build_feature_matrix, to_dataset
from stockmoney.models.kelly import position_size
from stockmoney.models.regime import KMeansGMMTrack
from stockmoney.models.walk_forward import run_walk_forward

HORIZON = 5
COST_BPS = 5.0
N_FOLDS = 5
EV_WINDOW = 90
MIN_TRADES = 10


def _group_stats(pnls: list[float]) -> str:
    if not pnls:
        return "n=0"
    wins = [p for p in pnls if p > 0]
    win_rate = len(wins) / len(pnls)
    avg = statistics.fmean(pnls)
    avg_win = statistics.fmean(wins) if wins else 0.0
    losses = [p for p in pnls if p <= 0]
    avg_loss = statistics.fmean(losses) if losses else 0.0
    return (f"n={len(pnls)} win_rate={win_rate:.3f} avg_pnl={avg:+.4f} "
            f"avg_win={avg_win:.4f} avg_loss={avg_loss:.4f}")


def main(db_path: str = DEFAULT_DB_PATH) -> None:
    conn = get_connection(db_path)

    matrix = build_feature_matrix(conn, target_symbol="SOXL", sector="semiconductor", horizon=HORIZON)
    dataset = to_dataset(matrix)

    # Module A track choice here is provisional: the module-A backtest found
    # no statistically significant OOS difference between GMM and HMM, so
    # either is a reasonable v1 substrate for module B; GMM picked arbitrarily.
    wf_result = run_walk_forward(
        dataset,
        lambda: KMeansGMMTrack(method="gmm", n_regimes=3, seed=0),
        lambda: LogisticDirectionModel(seed=0),
        n_folds=N_FOLDS,
    )

    outcomes = derive_trade_outcomes(wf_result)
    print(f"directional (non-range) OOS trades: {len(outcomes)} / {len(wf_result.trade_dates)} days")

    gate_results = run_ev_gate(outcomes, window=EV_WINDOW, cost_bps=COST_BPS, min_trades=MIN_TRADES)

    # Only trades where the gate had a full 90-EV reference window are a fair
    # test of the gate itself (earlier ones are unconditionally not tradeable).
    gated = [(o, r) for o, r in zip(outcomes, gate_results) if r.threshold is not None]
    passed = [o for o, r in gated if r.tradeable]
    blocked = [o for o, r in gated if not r.tradeable]

    print(f"\ntrades with a full {EV_WINDOW}-EV reference window: {len(gated)}")
    print(f"  gate PASSED (tradeable): {_group_stats([o.pnl_gross for o in passed])}")
    print(f"  gate BLOCKED           : {_group_stats([o.pnl_gross for o in blocked])}")

    # Kelly position sizes, computed from the same point-in-time stats used
    # to gate each trade (informational only per CLAUDE.md section 0 - no
    # order is ever placed).
    sizes = []
    for o in outcomes:
        stats = expanding_stats_asof(outcomes, o.trade_date, min_trades=MIN_TRADES)
        if stats is None:
            continue
        sizes.append(position_size(stats.p_win, stats.avg_win, stats.avg_loss))

    nonzero = [s for s in sizes if s > 0]
    print(f"\nKelly position sizes (n={len(sizes)}, nonzero={len(nonzero)}):")
    if sizes:
        print(f"  mean={statistics.fmean(sizes):.4f} "
              f"median={statistics.median(sizes):.4f} max={max(sizes):.4f}")

    conn.close()


if __name__ == "__main__":
    main()
