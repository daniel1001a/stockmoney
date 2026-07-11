"""Nightly cache builder for the FastAPI backend (see
~/.claude/plans/frontend-catalyst-rebuild.md, Track B / B1). Fitting module
A/B live (regime clustering + walk-forward backtest) is slow enough that
doing it inside an HTTP request handler would make every dashboard load
block for seconds -- this script does it once per night for every active
watchlist symbol and caches the results, so the API only ever does a DuckDB
read.

Two things get written per symbol:
1. Today's live call, via `daily_predictions.record_live_prediction` (same
   path `scripts/daily_prediction_cli.py record-watchlist` uses -- this
   script is what actually keeps that ledger fresh every day now; the CLI
   remains available for by-hand runs/backfills).
2. A walk-forward backtest summary + current EV-of-continuing estimate,
   cached into `symbol_backtest_snapshot` (today's row is replaced if this
   script runs more than once on the same day).

GMM-only (not GMM+HMM): mirrors stockmoney.models.production's own choice of
GMM as the unattended-production default. Running both tracks for all 11
watchlist symbols every night would roughly double runtime for a comparison
nothing here reads.

Usage:
    uv run python scripts/build_dashboard_snapshot.py
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from stockmoney.data.daily_predictions import record_live_prediction
from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations
from stockmoney.data.watchlist import sector_for_symbol
from stockmoney.models import production
from stockmoney.models.direction import LogisticDirectionModel
from stockmoney.models.ev_gate import compute_ev, derive_trade_outcomes, expanding_stats_asof, run_ev_gate
from stockmoney.models.feature_matrix import build_feature_matrix, to_dataset
from stockmoney.models.metrics import report_by_regime
from stockmoney.models.regime import KMeansGMMTrack
from stockmoney.models.walk_forward import run_walk_forward

HORIZON = production.DEFAULT_HORIZON
COST_BPS = production.DEFAULT_COST_BPS


def _active_watchlist_symbols(conn) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT symbol FROM watchlist_members WHERE removed_date IS NULL ORDER BY symbol"
        ).fetchall()
    ]


def _build_backtest_snapshot(conn, *, symbol: str, sector: str, as_of: date) -> dict | None:
    """None if there isn't enough resolved history to backtest yet (same
    condition production.fit_production_model treats as unsupported)."""
    matrix = build_feature_matrix(conn, target_symbol=symbol, sector=sector, horizon=HORIZON)
    if matrix.height == 0:
        return None
    dataset = to_dataset(matrix)

    wf_result = run_walk_forward(
        dataset,
        lambda: KMeansGMMTrack(method="gmm", n_regimes=3, seed=0),
        lambda: LogisticDirectionModel(seed=0, calibrate="sigmoid"),
    )
    report = report_by_regime(wf_result, horizon=HORIZON, cost_bps=COST_BPS)

    outcomes = derive_trade_outcomes(wf_result)
    gate_results = run_ev_gate(outcomes, cost_bps=COST_BPS)
    gated = [(o, r) for o, r in zip(outcomes, gate_results) if r.threshold is not None]
    passed = [o.pnl_gross for o, r in gated if r.tradeable]
    blocked = [o.pnl_gross for o, r in gated if not r.tradeable]

    # EV-of-continuing "as of today": reuse the same `outcomes` already
    # derived above rather than calling production.current_ev_of_continuing
    # (which would re-run this exact walk-forward a second time).
    stats = expanding_stats_asof(outcomes, as_of)
    ev_now = compute_ev(stats, cost_bps=COST_BPS) if stats is not None else None

    return {
        "overall_n": report["overall"].n,
        "overall_accuracy": report["overall"].accuracy,
        "overall_brier": report["overall"].brier,
        "overall_sharpe": report["overall"].sharpe,
        "ev_passed_n": len(passed),
        "ev_passed_win_rate": (sum(1 for p in passed if p > 0) / len(passed)) if passed else None,
        "ev_blocked_n": len(blocked),
        "ev_blocked_win_rate": (sum(1 for p in blocked if p > 0) / len(blocked)) if blocked else None,
        "ev_of_continuing_now": ev_now,
    }


def _write_backtest_snapshot(conn, *, as_of: date, symbol: str, sector: str, stats: dict) -> None:
    conn.execute(
        "DELETE FROM symbol_backtest_snapshot WHERE as_of_date = ? AND symbol = ?", [as_of, symbol]
    )
    conn.execute(
        """
        INSERT INTO symbol_backtest_snapshot (
            as_of_date, symbol, sector, overall_n, overall_accuracy, overall_brier,
            overall_sharpe, ev_passed_n, ev_passed_win_rate, ev_blocked_n,
            ev_blocked_win_rate, ev_of_continuing_now, computed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            as_of, symbol, sector, stats["overall_n"], stats["overall_accuracy"], stats["overall_brier"],
            stats["overall_sharpe"], stats["ev_passed_n"], stats["ev_passed_win_rate"],
            stats["ev_blocked_n"], stats["ev_blocked_win_rate"], stats["ev_of_continuing_now"],
            datetime.now(timezone.utc),
        ],
    )


def run_snapshot_build(conn, *, horizon: int = HORIZON) -> dict[str, str]:
    as_of = date.today()
    summary: dict[str, str] = {}
    for symbol in _active_watchlist_symbols(conn):
        sector = sector_for_symbol(conn, symbol)
        if sector is None:
            summary[symbol] = "skipped: not an active watchlist member / no sector"
            continue

        _prediction_id, skip_reason = record_live_prediction(
            conn, symbol=symbol, sector=sector, horizon=horizon
        )
        if skip_reason is not None:
            summary[symbol] = f"prediction skipped: {skip_reason}"
            continue

        stats = _build_backtest_snapshot(conn, symbol=symbol, sector=sector, as_of=as_of)
        if stats is None:
            summary[symbol] = "prediction recorded; backtest snapshot skipped: empty feature matrix"
            continue

        _write_backtest_snapshot(conn, as_of=as_of, symbol=symbol, sector=sector, stats=stats)
        summary[symbol] = "ok"
    return summary


def main(db_path: str = DEFAULT_DB_PATH) -> None:
    conn = get_connection(db_path)
    run_migrations(conn)
    try:
        summary = run_snapshot_build(conn)
    finally:
        conn.close()
    for symbol, status in summary.items():
        print(f"  {symbol}: {status}")


if __name__ == "__main__":
    main()
