"""Expanding-window walk-forward harness — the look-ahead firewall for module A.

Every downstream model (regime track, direction model) is fit strictly on the
training slice of each fold and only then applied to the test slice. Folds are
built so that:

- **Purge**: a training sample whose 5-day label window extends into (or past)
  the test period is dropped, so the forward-looking label can never bleed
  across the train/test boundary (López de Prado).
- **Embargo**: an extra gap of `embargo` trading days immediately before the
  test block is dropped from training, guarding against serial-correlation
  leakage in the features themselves.

The timeline is ordered by **trade_date**, not `available_at`: in an 8-year
backfill every row's `available_at` is the backfill time, so only the market
date honestly orders historical decisions. `available_at` remains the guard for
genuinely lagged/live data (e.g. FRED revisions) added later.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable, Protocol

import numpy as np

from stockmoney.models.feature_matrix import Dataset


class RegimeTrack(Protocol):
    def fit(self, train_X: np.ndarray) -> None: ...  # (n, 3) columns = REGIME_COLUMNS only
    def label(self, X: np.ndarray) -> np.ndarray: ...  # (n, 3) columns = REGIME_COLUMNS only; filtered/online regime per row


class DirectionModel(Protocol):
    def fit(self, X: np.ndarray, y: np.ndarray, regimes: np.ndarray) -> None: ...  # (n, 6) columns = FEATURE_COLUMNS
    def predict_proba(self, X: np.ndarray, regimes: np.ndarray) -> np.ndarray: ...  # (n, 6) columns = FEATURE_COLUMNS


@dataclass
class Fold:
    train_idx: np.ndarray
    test_idx: np.ndarray


@dataclass
class WalkForwardResult:
    trade_dates: list[date]
    label_end_dates: list[date]  # when each trade_date's forward-return label resolves
    y_true: np.ndarray
    proba: np.ndarray          # (n, 3) P(down/range/up)
    regime: np.ndarray         # regime label the model routed through
    fwd_return: np.ndarray
    n_folds: int


def make_folds(
    trade_dates: list[date],
    label_end_dates: list[date],
    *,
    n_folds: int = 5,
    initial_frac: float = 0.5,
    embargo: int = 5,
) -> list[Fold]:
    n = len(trade_dates)
    initial_train_end = int(n * initial_frac)
    remaining = n - initial_train_end
    if remaining < n_folds:
        raise ValueError("Not enough samples for the requested number of folds")
    block = remaining // n_folds

    folds: list[Fold] = []
    for k in range(n_folds):
        test_start = initial_train_end + k * block
        test_end = n if k == n_folds - 1 else test_start + block
        test_idx = np.arange(test_start, test_end)
        test_start_date = trade_dates[test_start]

        train_idx = np.array(
            [
                i
                for i in range(test_start)
                if label_end_dates[i] < test_start_date  # purge label-window overlap
                and i < test_start - embargo             # embargo gap
            ],
            dtype=int,
        )
        folds.append(Fold(train_idx=train_idx, test_idx=test_idx))
    return folds


def run_walk_forward(
    dataset: Dataset,
    make_track: Callable[[], RegimeTrack],
    make_direction_model: Callable[[], DirectionModel],
    *,
    n_folds: int = 5,
    initial_frac: float = 0.5,
    embargo: int = 5,
) -> WalkForwardResult:
    folds = make_folds(
        dataset.trade_dates,
        dataset.label_end_dates,
        n_folds=n_folds,
        initial_frac=initial_frac,
        embargo=embargo,
    )

    dates: list[date] = []
    label_end_dates: list[date] = []
    y_true_parts, proba_parts, regime_parts, fwd_parts = [], [], [], []

    for fold in folds:
        train_idx, test_idx = fold.train_idx, fold.test_idx
        if len(train_idx) == 0:
            continue

        track = make_track()
        track.fit(dataset.regime_X[train_idx])

        # Label leak-free over history up to the end of this test block; filtered
        # regimes use past-only observations, model params are train-fit.
        upto = int(test_idx[-1]) + 1
        regimes_all = track.label(dataset.regime_X[:upto])
        train_reg = regimes_all[train_idx]
        test_reg = regimes_all[test_idx]

        model = make_direction_model()
        model.fit(dataset.X[train_idx], dataset.y[train_idx], train_reg)
        proba = model.predict_proba(dataset.X[test_idx], test_reg)

        dates.extend(dataset.trade_dates[i] for i in test_idx)
        label_end_dates.extend(dataset.label_end_dates[i] for i in test_idx)
        y_true_parts.append(dataset.y[test_idx])
        proba_parts.append(proba)
        regime_parts.append(test_reg)
        fwd_parts.append(dataset.fwd_return[test_idx])

    return WalkForwardResult(
        trade_dates=dates,
        label_end_dates=label_end_dates,
        y_true=np.concatenate(y_true_parts),
        proba=np.concatenate(proba_parts),
        regime=np.concatenate(regime_parts),
        fwd_return=np.concatenate(fwd_parts),
        n_folds=len(folds),
    )
