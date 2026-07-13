"""Ablation: does adding GDELT market-wide news sentiment (gdelt_avgtone_1d,
gdelt_goldstein_1d) to module A's direction features actually improve
out-of-sample calibration, or is it noise?

Same discipline as backtest_vix_term_ablation.py / backtest_feature_ablation.py
(CLAUDE.md section 12): a candidate only "passes" via a paired bootstrap CI on
its OOS per-sample Brier delta, never an eyeballed single number. Two things
make this a fair, paired test:

  * ONE feature matrix is built per symbol requiring baseline + GDELT columns all
    non-null, so both arms score the exact same rows/trade_dates.
  * The regime track always observes REGIME_COLUMNS in both arms (GDELT features
    are added only to the direction model's X), so regime fold boundaries and
    walk-forward purge/embargo are identical by construction.

GDELT is a market-wide (MARKET_SYMBOL) daily sentiment aggregate with full free
history via BigQuery (scripts/backfill_gdelt.py) -- so, like the VIX term
structure and unlike the per-underlying option-chain candidates, it is genuinely
backtestable. Pooled across several liquid names (not SOXL alone) so the
bootstrap has real power and the result isn't a single-symbol fluke.

Per CLAUDE.md section 12: promote to feature_matrix.FEATURE_COLUMNS ONLY if the
pooled 95% CI on Brier(+GDELT) - Brier(baseline) is fully < 0. Otherwise it stays
a candidate in feature_store (already loaded by build_feature_matrix) and is
reported honestly as "tried, not promoted" -- not deleted.

Usage:
    .venv/bin/python -m stockmoney.models.backtest_gdelt_ablation --db data/stockmoney_live.duckdb
"""
from __future__ import annotations

import argparse

import numpy as np

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection
from stockmoney.models.direction import LogisticDirectionModel
from stockmoney.models.feature_matrix import build_feature_matrix, to_dataset
from stockmoney.models.metrics import bootstrap_paired_diff_ci, brier_per_sample, report_by_regime
from stockmoney.models.regime import KMeansGMMTrack
from stockmoney.models.walk_forward import run_walk_forward

HORIZON = 5
COST_BPS = 5.0
N_FOLDS = 5

BASELINE_COLUMNS = [
    "realized_vol_20d", "adx_14", "xsec_dispersion",
    "yield_curve_10y2y_ffill", "dxy_chg_1d_ffill", "oil_chg_1d_ffill",
]
GDELT_COLUMNS = ["gdelt_avgtone_1d", "gdelt_goldstein_1d"]
EXTENDED_COLUMNS = BASELINE_COLUMNS + GDELT_COLUMNS

# (symbol, sector) -- sector drives the xsec_dispersion group.
SYMBOLS = [
    ("SOXL", "semiconductor"),
    ("NVDA", "semiconductor"),
    ("AMD", "semiconductor"),
    ("AAPL", "big_tech"),
    ("MSFT", "big_tech"),
]


def _arms_for_symbol(conn, symbol: str, sector: str):
    """Return (baseline_brier, extended_brier, baseline_report, extended_report)
    per-sample arrays for one symbol, or None if too little data."""
    matrix = build_feature_matrix(
        conn, target_symbol=symbol, sector=sector, horizon=HORIZON,
        feature_columns=EXTENDED_COLUMNS,
    )
    if matrix.height < 60:
        return None
    baseline = to_dataset(matrix, feature_columns=BASELINE_COLUMNS)
    extended = to_dataset(matrix, feature_columns=EXTENDED_COLUMNS)

    def _run(ds):
        return run_walk_forward(
            ds,
            lambda: KMeansGMMTrack(method="gmm", n_regimes=3, seed=0),
            lambda: LogisticDirectionModel(seed=0, calibrate="sigmoid"),
            n_folds=N_FOLDS,
        )

    b_res, e_res = _run(baseline), _run(extended)
    assert b_res.trade_dates == e_res.trade_dates  # paired by construction
    return (
        brier_per_sample(b_res.y_true, b_res.proba),
        brier_per_sample(e_res.y_true, e_res.proba),
        report_by_regime(b_res, horizon=HORIZON, cost_bps=COST_BPS),
        report_by_regime(e_res, horizon=HORIZON, cost_bps=COST_BPS),
    )


def main(db_path: str = DEFAULT_DB_PATH) -> None:
    conn = get_connection(db_path)
    print(f"Baseline: {BASELINE_COLUMNS}")
    print(f"Candidate +GDELT: {GDELT_COLUMNS}\n")

    pooled_b, pooled_e = [], []
    for symbol, sector in SYMBOLS:
        arms = _arms_for_symbol(conn, symbol, sector)
        if arms is None:
            print(f"{symbol}: insufficient data, skipped")
            continue
        b_brier, e_brier, b_rep, e_rep = arms
        pooled_b.append(b_brier)
        pooled_e.append(e_brier)
        bo, eo = b_rep["overall"], e_rep["overall"]
        print(f"{symbol:5} n={bo.n:4}  "
              f"baseline: acc={bo.accuracy:.3f} brier={bo.brier:.3f}  |  "
              f"+GDELT: acc={eo.accuracy:.3f} brier={eo.brier:.3f}  "
              f"(Δbrier {eo.brier - bo.brier:+.4f})")

    conn.close()
    if not pooled_b:
        print("\nNo symbols had enough data.")
        return

    b_all = np.concatenate(pooled_b)
    e_all = np.concatenate(pooled_e)
    mean_diff, lo, hi = bootstrap_paired_diff_ci(e_all, b_all)
    print(f"\n=== POOLED (n={len(b_all)}) Brier(+GDELT) - Brier(baseline) ===")
    print(f"mean diff: {mean_diff:+.4f}  95% CI: [{lo:+.4f}, {hi:+.4f}]")
    if hi < 0:
        print("-> GDELT sentiment SIGNIFICANTLY improves OOS Brier. Promote to FEATURE_COLUMNS.")
    elif lo > 0:
        print("-> GDELT sentiment SIGNIFICANTLY worsens OOS Brier. Keep in feature_store, do not promote.")
    else:
        print("-> No statistically significant difference; not enough evidence to promote yet.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    main(args.db)
