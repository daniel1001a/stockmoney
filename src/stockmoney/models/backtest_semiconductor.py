"""Module A vertical-slice entry point: walk-forward both regime tracks
(KMeans/GMM vs HMM) over 8 years of SOXL + semiconductor cross-sectional data,
report out-of-sample metrics per regime, and bootstrap-compare the tracks.

This reports; it does not pick a winner into production (CLAUDE.md section 4:
"用樣本外表現決定採用哪一套" is a human decision informed by this output).

Usage:
    uv run python -m stockmoney.models.backtest_semiconductor
"""
from __future__ import annotations

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection
from stockmoney.models.direction import LogisticDirectionModel
from stockmoney.models.feature_matrix import build_feature_matrix, to_dataset
from stockmoney.models.metrics import bootstrap_paired_diff_ci, brier_per_sample, report_by_regime
from stockmoney.models.regime import HMMTrack, KMeansGMMTrack
from stockmoney.models.walk_forward import run_walk_forward

HORIZON = 5
COST_BPS = 5.0
N_FOLDS = 5


def _print_report(name: str, report: dict) -> None:
    print(f"\n=== {name} ===")
    print(f"baseline (majority class) accuracy: {report['baseline_accuracy']:.3f}")
    o = report["overall"]
    print(f"overall: n={o.n} accuracy={o.accuracy:.3f} brier={o.brier:.3f} sharpe={o.sharpe:.2f}")
    for r in report["per_regime"]:
        print(f"  regime {r.regime}: n={r.n} accuracy={r.accuracy:.3f} "
              f"brier={r.brier:.3f} sharpe={r.sharpe:.2f}")


def main(db_path: str = DEFAULT_DB_PATH) -> None:
    conn = get_connection(db_path)

    matrix = build_feature_matrix(conn, target_symbol="SOXL", sector="semiconductor", horizon=HORIZON)
    print(f"feature matrix: {matrix.height} rows, "
          f"{matrix['trade_date'].min()} -> {matrix['trade_date'].max()}")
    dataset = to_dataset(matrix)

    kmeans_result = run_walk_forward(
        dataset,
        lambda: KMeansGMMTrack(method="gmm", n_regimes=3, seed=0),
        lambda: LogisticDirectionModel(seed=0, calibrate="sigmoid"),
        n_folds=N_FOLDS,
    )
    hmm_result = run_walk_forward(
        dataset,
        lambda: HMMTrack(n_regimes=3, seed=0),
        lambda: LogisticDirectionModel(seed=0, calibrate="sigmoid"),
        n_folds=N_FOLDS,
    )

    kmeans_report = report_by_regime(kmeans_result, horizon=HORIZON, cost_bps=COST_BPS)
    hmm_report = report_by_regime(hmm_result, horizon=HORIZON, cost_bps=COST_BPS)
    _print_report("Track A: GMM regime detection", kmeans_report)
    _print_report("Track B: HMM regime detection (filtered)", hmm_report)

    # Paired comparison: both tracks are evaluated over the identical OOS
    # trade_date sequence, so per-sample Brier scores line up 1:1.
    assert kmeans_result.trade_dates == hmm_result.trade_dates
    brier_a = brier_per_sample(kmeans_result.y_true, kmeans_result.proba)
    brier_b = brier_per_sample(hmm_result.y_true, hmm_result.proba)
    mean_diff, lo, hi = bootstrap_paired_diff_ci(brier_a, brier_b)
    print(f"\n=== GMM vs HMM: Brier(GMM) - Brier(HMM) ===")
    print(f"mean diff: {mean_diff:+.4f}  95% CI: [{lo:+.4f}, {hi:+.4f}]")
    if lo > 0:
        print("-> HMM has significantly lower Brier (better calibrated) out-of-sample.")
    elif hi < 0:
        print("-> GMM has significantly lower Brier (better calibrated) out-of-sample.")
    else:
        print("-> No statistically significant difference; either is a reasonable v1 choice.")

    conn.close()


if __name__ == "__main__":
    main()
