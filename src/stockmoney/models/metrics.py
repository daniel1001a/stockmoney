"""Out-of-sample evaluation for the walk-forward result.

Metrics follow CLAUDE.md section 12: directional accuracy vs a baseline, Brier
score (probability calibration), and Sharpe/Sortino of a simple cost-aware
strategy — reported per regime, never merged into one win rate. Track-vs-track
decisions use bootstrap confidence intervals rather than a raw metric compare.

The Sharpe here validates *signal quality* via a toy long/flat/short position
(up→+1, range→0, down→-1); it is deliberately not the real options P&L, which
belongs downstream in the EV gate / Kelly sizing (module B).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from stockmoney.models.walk_forward import WalkForwardResult

TRADING_DAYS = 252


def brier_per_sample(y_true: np.ndarray, proba: np.ndarray) -> np.ndarray:
    onehot = np.zeros_like(proba)
    onehot[np.arange(len(y_true)), y_true] = 1.0
    return ((proba - onehot) ** 2).sum(axis=1)


def brier_score(y_true: np.ndarray, proba: np.ndarray) -> float:
    return float(brier_per_sample(y_true, proba).mean())


def directional_accuracy(y_true: np.ndarray, proba: np.ndarray) -> float:
    return float((proba.argmax(axis=1) == y_true).mean())


def majority_baseline_accuracy(y_true: np.ndarray) -> float:
    counts = np.bincount(y_true, minlength=3)
    return float(counts.max() / counts.sum())


def strategy_net_returns(
    proba: np.ndarray, fwd_return: np.ndarray, cost_bps: float
) -> np.ndarray:
    position = proba.argmax(axis=1) - 1  # {0,1,2} -> {-1,0,+1}
    cost = cost_bps / 1e4 * np.abs(position)
    return position * fwd_return - cost


def sharpe(returns: np.ndarray, horizon: int) -> float:
    if len(returns) < 2:
        return 0.0
    sd = returns.std(ddof=1)
    if sd == 0:
        return 0.0
    return float(returns.mean() / sd * math.sqrt(TRADING_DAYS / horizon))


def sortino(returns: np.ndarray, horizon: int) -> float:
    if len(returns) < 2:
        return 0.0
    downside = returns[returns < 0]
    dd = downside.std(ddof=1) if len(downside) > 1 else 0.0
    if dd == 0:
        return 0.0
    return float(returns.mean() / dd * math.sqrt(TRADING_DAYS / horizon))


def _nonoverlap(returns: np.ndarray, horizon: int) -> np.ndarray:
    """Subsample every `horizon`-th trade so the Sharpe isn't inflated by the
    autocorrelation of overlapping h-day forward returns."""
    return returns[::horizon]


def bootstrap_mean_ci(
    values: np.ndarray, *, n_boot: int = 2000, alpha: float = 0.05, seed: int = 0
) -> tuple[float, float, float]:
    rng = np.random.default_rng(seed)
    n = len(values)
    if n == 0:
        return 0.0, 0.0, 0.0
    means = np.array([values[rng.integers(0, n, n)].mean() for _ in range(n_boot)])
    lo, hi = np.percentile(means, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return float(values.mean()), float(lo), float(hi)


def bootstrap_paired_diff_ci(
    a: np.ndarray, b: np.ndarray, **kw
) -> tuple[float, float, float]:
    """CI for mean(a - b) on paired samples (same OOS points). If the interval
    excludes 0, one track's per-sample metric is significantly better."""
    return bootstrap_mean_ci(a - b, **kw)


@dataclass
class RegimeReport:
    regime: int
    n: int
    accuracy: float
    brier: float
    sharpe: float


def report_by_regime(
    result: WalkForwardResult, *, horizon: int = 5, cost_bps: float = 5.0
) -> dict:
    net = strategy_net_returns(result.proba, result.fwd_return, cost_bps)
    out = {
        "overall": RegimeReport(
            regime=-1,
            n=len(result.y_true),
            accuracy=directional_accuracy(result.y_true, result.proba),
            brier=brier_score(result.y_true, result.proba),
            sharpe=sharpe(_nonoverlap(net, horizon), horizon),
        ),
        "baseline_accuracy": majority_baseline_accuracy(result.y_true),
        "per_regime": [],
    }
    for r in sorted(set(result.regime.tolist())):
        mask = result.regime == r
        out["per_regime"].append(
            RegimeReport(
                regime=int(r),
                n=int(mask.sum()),
                accuracy=directional_accuracy(result.y_true[mask], result.proba[mask]),
                brier=brier_score(result.y_true[mask], result.proba[mask]),
                sharpe=sharpe(_nonoverlap(net[mask], horizon), horizon),
            )
        )
    return out
