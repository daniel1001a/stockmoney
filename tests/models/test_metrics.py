from datetime import date, timedelta

import numpy as np
import pytest

from stockmoney.models.metrics import (
    bootstrap_mean_ci,
    bootstrap_paired_diff_ci,
    brier_score,
    directional_accuracy,
    majority_baseline_accuracy,
    report_by_regime,
    sharpe,
    sortino,
    strategy_net_returns,
)
from stockmoney.models.walk_forward import WalkForwardResult


def test_brier_score_perfect_prediction_is_zero():
    y_true = np.array([0, 1, 2])
    proba = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=float)
    assert brier_score(y_true, proba) == 0.0


def test_brier_score_uniform_guess_known_value():
    y_true = np.array([0])
    proba = np.array([[1 / 3, 1 / 3, 1 / 3]])
    # (1/3-1)^2 + (1/3)^2 + (1/3)^2 = 4/9 + 1/9 + 1/9 = 6/9 = 2/3
    assert brier_score(y_true, proba) == pytest.approx(2 / 3, abs=1e-9)


def test_directional_accuracy():
    y_true = np.array([0, 1, 2, 0])
    proba = np.array([
        [1, 0, 0],   # correct
        [0, 0, 1],   # wrong (predicts 2, true 1)
        [0, 0, 1],   # correct
        [1, 0, 0],   # correct
    ], dtype=float)
    assert directional_accuracy(y_true, proba) == 0.75


def test_majority_baseline_accuracy():
    y_true = np.array([0, 0, 0, 1, 2])
    assert majority_baseline_accuracy(y_true) == 3 / 5


def test_strategy_net_returns_positions_and_cost():
    proba = np.array([
        [0, 0, 1],   # up -> position +1
        [1, 0, 0],   # down -> position -1
        [0, 1, 0],   # range -> position 0
    ], dtype=float)
    fwd_return = np.array([0.02, 0.02, 0.02])
    net = strategy_net_returns(proba, fwd_return, cost_bps=100.0)  # 100bps = 0.01
    np.testing.assert_allclose(net, [0.02 - 0.01, -0.02 - 0.01, 0.0])


def test_sharpe_zero_variance_is_zero():
    assert sharpe(np.array([0.01, 0.01, 0.01]), horizon=5) == 0.0


def test_sharpe_positive_for_positive_mean():
    rng = np.random.default_rng(0)
    returns = rng.normal(loc=0.01, scale=0.02, size=200)
    assert sharpe(returns, horizon=5) > 0


def test_sortino_ignores_upside_volatility():
    # Same mean, but one series has upside-only extra volatility.
    base = np.array([0.01, -0.01, 0.01, -0.01, 0.01, -0.01] * 10)
    upside_noisy = base.copy()
    upside_noisy[::2] += 0.05  # inflate only the positive entries
    assert sortino(upside_noisy, horizon=5) >= sortino(base, horizon=5)


def test_bootstrap_mean_ci_contains_true_mean_with_low_variance_data():
    rng = np.random.default_rng(0)
    values = rng.normal(loc=0.5, scale=0.01, size=500)
    mean, lo, hi = bootstrap_mean_ci(values, n_boot=1000, seed=0)
    assert lo <= mean <= hi
    assert lo < 0.5 < hi


def test_bootstrap_paired_diff_ci_detects_clear_difference():
    rng = np.random.default_rng(0)
    a = rng.normal(loc=1.0, scale=0.1, size=300)
    b = rng.normal(loc=0.5, scale=0.1, size=300)
    mean, lo, hi = bootstrap_paired_diff_ci(a, b, n_boot=1000, seed=0)
    assert lo > 0  # CI excludes 0 -> significant difference detected


def test_report_by_regime_structure():
    n = 40
    result = WalkForwardResult(
        trade_dates=[date(2020, 1, 1) + timedelta(days=i) for i in range(n)],
        label_end_dates=[date(2020, 1, 1) + timedelta(days=i + 5) for i in range(n)],
        y_true=np.array([0, 1, 2] * 13 + [0]),
        proba=np.tile(np.array([1 / 3, 1 / 3, 1 / 3]), (n, 1)),
        regime=np.array([0] * 20 + [1] * 20),
        fwd_return=np.zeros(n),
        n_folds=2,
    )
    report = report_by_regime(result, horizon=5, cost_bps=5.0)
    assert "overall" in report and "per_regime" in report and "baseline_accuracy" in report
    assert {r.regime for r in report["per_regime"]} == {0, 1}
    assert report["overall"].n == n
