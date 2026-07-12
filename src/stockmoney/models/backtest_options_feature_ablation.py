"""Options-microstructure feature ablation (Part 2A of
~/.claude/plans/buzzing-yawning-squid.md): does adding gex_estimate,
skew_25delta_chg_1d, and put_call_ratio to module A's feature set actually
help, tested one at a time via the exact same paired-bootstrap discipline
backtest_feature_ablation.py used for rsi_14/volume_zscore_20d (which failed).

Unlike that script, this one does not require hand-editing the production
FEATURE_COLUMNS constant to test a candidate: build_feature_matrix/to_dataset
now accept an optional `feature_columns` override (added alongside this
script) specifically so a candidate list can be tested and discarded without
ever touching the constant every other backtest/production path reads.

CANNOT be run meaningfully yet on this machine: options_derived_daily and
put_call_ratio_daily have no real backfilled history (this checkout has no
populated DB at all -- see options_derived.py's module docstring on why gex_
estimate/skew_25delta/put_call_ratio need real options-chain ingestion first).
Running this against an empty/synthetic DB will not error, but the resulting
CI will be meaningless noise, not evidence. This script exists now so that,
the moment a real DB has enough options-chain history, the significance test
is a single command away.

Usage (once options_derived_daily/put_call_ratio_daily have real history):
    uv run python scripts/compute_features.py   # promote raw metrics into feature_store first
    uv run python -m stockmoney.models.backtest_options_feature_ablation
"""
from __future__ import annotations

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection
from stockmoney.models.direction import LogisticDirectionModel
from stockmoney.models.feature_matrix import FEATURE_COLUMNS, build_feature_matrix, to_dataset
from stockmoney.models.metrics import bootstrap_paired_diff_ci, brier_per_sample, report_by_regime
from stockmoney.models.regime import KMeansGMMTrack
from stockmoney.models.walk_forward import run_walk_forward

HORIZON = 5
COST_BPS = 5.0
N_FOLDS = 5
CANDIDATE_COLUMNS = ["gex_estimate", "skew_25delta_chg_1d", "put_call_ratio"]


def _print_report(name: str, report: dict) -> None:
    print(f"\n=== {name} ===")
    print(f"baseline (majority class) accuracy: {report['baseline_accuracy']:.3f}")
    o = report["overall"]
    print(f"overall: n={o.n} accuracy={o.accuracy:.3f} brier={o.brier:.3f} sharpe={o.sharpe:.2f}")
    for r in report["per_regime"]:
        print(f"  regime {r.regime}: n={r.n} accuracy={r.accuracy:.3f} "
              f"brier={r.brier:.3f} sharpe={r.sharpe:.2f}")


def main(db_path: str = DEFAULT_DB_PATH, candidate: str | None = None) -> None:
    """Test one candidate at a time by default (`candidate`), or all of
    CANDIDATE_COLUMNS combined if `candidate` is None -- matching
    backtest_feature_ablation.py's all-at-once shape, but callable per-feature
    too since combining untested candidates would muddy which one (if any)
    actually carries signal."""
    conn = get_connection(db_path)

    candidates = [candidate] if candidate else CANDIDATE_COLUMNS
    baseline_columns = list(FEATURE_COLUMNS)
    extended_columns = baseline_columns + candidates

    matrix = build_feature_matrix(
        conn, target_symbol="SOXL", sector="semiconductor", horizon=HORIZON,
        feature_columns=extended_columns,
    )
    print(f"feature matrix: {matrix.height} rows, "
          f"{matrix['trade_date'].min() if matrix.height else None} -> "
          f"{matrix['trade_date'].max() if matrix.height else None}")
    if matrix.height == 0:
        print("empty feature matrix -- options_derived_daily/put_call_ratio_daily "
              "have no history yet on this DB (see module docstring). Nothing to test.")
        conn.close()
        return

    baseline_dataset = to_dataset(matrix, feature_columns=baseline_columns)
    extended_dataset = to_dataset(matrix, feature_columns=extended_columns)
    print(f"baseline columns: {baseline_columns}")
    print(f"candidate new columns: {candidates}")

    baseline_result = run_walk_forward(
        baseline_dataset,
        lambda: KMeansGMMTrack(method="gmm", n_regimes=3, seed=0),
        lambda: LogisticDirectionModel(seed=0, calibrate="sigmoid"),
        n_folds=N_FOLDS,
    )
    extended_result = run_walk_forward(
        extended_dataset,
        lambda: KMeansGMMTrack(method="gmm", n_regimes=3, seed=0),
        lambda: LogisticDirectionModel(seed=0, calibrate="sigmoid"),
        n_folds=N_FOLDS,
    )

    baseline_report = report_by_regime(baseline_result, horizon=HORIZON, cost_bps=COST_BPS)
    extended_report = report_by_regime(extended_result, horizon=HORIZON, cost_bps=COST_BPS)
    _print_report("Baseline (6 features)", baseline_report)
    _print_report(f"Extended (+{candidates})", extended_report)

    assert baseline_result.trade_dates == extended_result.trade_dates
    brier_baseline = brier_per_sample(baseline_result.y_true, baseline_result.proba)
    brier_extended = brier_per_sample(extended_result.y_true, extended_result.proba)
    mean_diff, lo, hi = bootstrap_paired_diff_ci(brier_extended, brier_baseline)
    print(f"\n=== Extended vs Baseline: Brier(extended) - Brier(baseline) ===")
    print(f"mean diff: {mean_diff:+.4f}  95% CI: [{lo:+.4f}, {hi:+.4f}]")
    if hi < 0:
        print("-> Candidate(s) significantly IMPROVE Brier out-of-sample. Consider promoting to production.")
    elif lo > 0:
        print("-> Candidate(s) significantly WORSEN Brier out-of-sample. Do not promote; keep in feature_store for later re-testing.")
    else:
        print("-> No statistically significant difference; not enough evidence to promote yet.")

    conn.close()


if __name__ == "__main__":
    main()
