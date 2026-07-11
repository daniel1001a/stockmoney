"""Module A direction-model comparison: LightGBM (CLAUDE.md section 14's
originally-planned v1 algorithm) vs the logistic-regression baseline, both
walk-forward over the identical GMM-regime OOS trade_date sequence (GMM
picked per project convention -- backtest_semiconductor.py found no
statistically significant OOS difference vs HMM, and HMM has a known
degenerate-regime failure mode).

This reports; it does not pick a winner into production. Same discipline as
backtest_semiconductor.py: per-regime metrics, never a single merged number,
and a bootstrap CI on the paired Brier difference rather than eyeballing
which number is smaller.

Usage:
    uv run python -m stockmoney.models.backtest_direction_models
"""
from __future__ import annotations

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection
from stockmoney.models.direction import LightGBMDirectionModel, LogisticDirectionModel
from stockmoney.models.feature_matrix import build_feature_matrix, to_dataset
from stockmoney.models.metrics import bootstrap_paired_diff_ci, brier_per_sample, report_by_regime
from stockmoney.models.regime import KMeansGMMTrack
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

    logistic_result = run_walk_forward(
        dataset,
        lambda: KMeansGMMTrack(method="gmm", n_regimes=3, seed=0),
        lambda: LogisticDirectionModel(seed=0, calibrate="sigmoid"),
        n_folds=N_FOLDS,
    )
    lgbm_result = run_walk_forward(
        dataset,
        lambda: KMeansGMMTrack(method="gmm", n_regimes=3, seed=0),
        lambda: LightGBMDirectionModel(seed=0),
        n_folds=N_FOLDS,
    )

    logistic_report = report_by_regime(logistic_result, horizon=HORIZON, cost_bps=COST_BPS)
    lgbm_report = report_by_regime(lgbm_result, horizon=HORIZON, cost_bps=COST_BPS)
    _print_report("Logistic regression (baseline)", logistic_report)
    _print_report("LightGBM", lgbm_report)

    assert logistic_result.trade_dates == lgbm_result.trade_dates
    brier_logistic = brier_per_sample(logistic_result.y_true, logistic_result.proba)
    brier_lgbm = brier_per_sample(lgbm_result.y_true, lgbm_result.proba)
    mean_diff, lo, hi = bootstrap_paired_diff_ci(brier_lgbm, brier_logistic)
    print(f"\n=== LightGBM vs Logistic: Brier(LightGBM) - Brier(Logistic) ===")
    print(f"mean diff: {mean_diff:+.4f}  95% CI: [{lo:+.4f}, {hi:+.4f}]")
    if hi < 0:
        print("-> LightGBM has significantly lower Brier (better calibrated) out-of-sample.")
    elif lo > 0:
        print("-> Logistic regression has significantly lower Brier (better calibrated) out-of-sample.")
    else:
        print("-> No statistically significant difference; the logistic baseline is a reasonable v1 choice.")

    conn.close()


if __name__ == "__main__":
    main()
