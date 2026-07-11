"""Feature ablation: does adding rsi_14/volume_zscore_20d to the module-A
feature set actually help, or is an eyeballed accuracy/Brier delta just noise?
CLAUDE.md section 12 is explicit that a new feature only "passes" via a
bootstrap significance test on its own OOS metric delta, never a raw single-
number comparison -- this script is that test, reusable for any future
feature candidate, not just this one.

Both the baseline (original 6 columns) and extended (all of
feature_matrix.FEATURE_COLUMNS) variants are built from the exact same
build_feature_matrix() call and OOS trade_date sequence -- only the column
slice fed to the direction model differs. The regime track sees the same
regime_X (CLAUDE.md section 4's fixed 3-column observation set) in both
arms regardless of which columns are being ablated for the direction model
-- Dataset.regime_X is copied unchanged by dataclasses.replace(..., X=...)
below, so this comparison is paired and apples-to-apples (same regime-track
fold boundaries, same walk-forward purge/embargo) by construction, not by
convention.

Usage:
    uv run python -m stockmoney.models.backtest_feature_ablation
"""
from __future__ import annotations

from dataclasses import replace

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection
from stockmoney.models.direction import LogisticDirectionModel
from stockmoney.models.feature_matrix import FEATURE_COLUMNS, build_feature_matrix, to_dataset
from stockmoney.models.metrics import bootstrap_paired_diff_ci, brier_per_sample, report_by_regime
from stockmoney.models.regime import KMeansGMMTrack
from stockmoney.models.walk_forward import run_walk_forward

HORIZON = 5
COST_BPS = 5.0
N_FOLDS = 5
BASELINE_COLUMNS = [
    "realized_vol_20d", "adx_14", "xsec_dispersion",
    "yield_curve_10y2y", "dxy_chg_1d_ffill", "oil_chg_1d_ffill",
]


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
    full_dataset = to_dataset(matrix)

    baseline_idx = [FEATURE_COLUMNS.index(c) for c in BASELINE_COLUMNS]
    baseline_dataset = replace(full_dataset, X=full_dataset.X[:, baseline_idx])

    new_columns = [c for c in FEATURE_COLUMNS if c not in BASELINE_COLUMNS]
    print(f"baseline columns: {BASELINE_COLUMNS}")
    print(f"candidate new columns: {new_columns}")

    baseline_result = run_walk_forward(
        baseline_dataset,
        lambda: KMeansGMMTrack(method="gmm", n_regimes=3, seed=0),
        lambda: LogisticDirectionModel(seed=0, calibrate="sigmoid"),
        n_folds=N_FOLDS,
    )
    extended_result = run_walk_forward(
        full_dataset,
        lambda: KMeansGMMTrack(method="gmm", n_regimes=3, seed=0),
        lambda: LogisticDirectionModel(seed=0, calibrate="sigmoid"),
        n_folds=N_FOLDS,
    )

    baseline_report = report_by_regime(baseline_result, horizon=HORIZON, cost_bps=COST_BPS)
    extended_report = report_by_regime(extended_result, horizon=HORIZON, cost_bps=COST_BPS)
    _print_report("Baseline (6 features)", baseline_report)
    _print_report(f"Extended (+{new_columns})", extended_report)

    assert baseline_result.trade_dates == extended_result.trade_dates
    brier_baseline = brier_per_sample(baseline_result.y_true, baseline_result.proba)
    brier_extended = brier_per_sample(extended_result.y_true, extended_result.proba)
    mean_diff, lo, hi = bootstrap_paired_diff_ci(brier_extended, brier_baseline)
    print(f"\n=== Extended vs Baseline: Brier(extended) - Brier(baseline) ===")
    print(f"mean diff: {mean_diff:+.4f}  95% CI: [{lo:+.4f}, {hi:+.4f}]")
    if hi < 0:
        print("-> New features significantly IMPROVE Brier out-of-sample. Consider promoting to production.")
    elif lo > 0:
        print("-> New features significantly WORSEN Brier out-of-sample. Do not promote; keep in feature_store for later re-testing.")
    else:
        print("-> No statistically significant difference; not enough evidence to promote these features yet.")

    conn.close()


if __name__ == "__main__":
    main()
