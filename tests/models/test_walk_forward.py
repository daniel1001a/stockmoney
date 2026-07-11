from datetime import date, datetime, timedelta, timezone

import numpy as np

from stockmoney.models.feature_matrix import Dataset
from stockmoney.models.walk_forward import make_folds, run_walk_forward


def _synthetic_dataset(n=120, horizon=5, seed=0) -> Dataset:
    rng = np.random.default_rng(seed)
    start = date(2020, 1, 1)
    trade_dates = [start + timedelta(days=i) for i in range(n)]
    label_end_dates = [start + timedelta(days=i + horizon) for i in range(n)]
    X = rng.normal(size=(n, 6))
    return Dataset(
        X=X,
        regime_X=X[:, :3],
        y=rng.integers(0, 3, size=n),
        trade_dates=trade_dates,
        available_at=[datetime(d.year, d.month, d.day, tzinfo=timezone.utc) for d in trade_dates],
        label_end_dates=label_end_dates,
        fwd_return=rng.normal(scale=0.02, size=n),
    )


# --- trivial models for harness isolation tests ---
class _SingleRegime:
    def fit(self, train_X):
        self.fit_X = train_X

    def label(self, X):
        return np.zeros(len(X), dtype=int)


class _FreqModel:
    """Predicts the class frequencies of its TRAINING labels (so predictions
    depend on train y — the positive control for the leakage test)."""

    def fit(self, X, y, regimes):
        counts = np.bincount(y, minlength=3).astype(float)
        self.p = counts / counts.sum() if counts.sum() else np.ones(3) / 3

    def predict_proba(self, X, regimes):
        return np.tile(self.p, (len(X), 1))


def test_purge_removes_label_overlap():
    ds = _synthetic_dataset()
    folds = make_folds(ds.trade_dates, ds.label_end_dates, n_folds=4, embargo=5)
    for fold in folds:
        test_start_date = ds.trade_dates[fold.test_idx[0]]
        for i in fold.train_idx:
            assert ds.label_end_dates[i] < test_start_date


def test_embargo_gap_before_test():
    ds = _synthetic_dataset()
    embargo = 5
    folds = make_folds(ds.trade_dates, ds.label_end_dates, n_folds=4, embargo=embargo)
    for fold in folds:
        if len(fold.train_idx) == 0:
            continue
        assert fold.train_idx.max() < fold.test_idx[0] - embargo


def test_train_and_test_disjoint():
    ds = _synthetic_dataset()
    folds = make_folds(ds.trade_dates, ds.label_end_dates, n_folds=4)
    for fold in folds:
        assert set(fold.train_idx.tolist()).isdisjoint(fold.test_idx.tolist())


def test_pure_oos_labels_never_leak_into_fit():
    # The final fold's test rows are pure out-of-sample: nothing comes after
    # them, so they are never used as training data in any fold. Corrupting
    # only those labels must not change a single prediction.
    ds = _synthetic_dataset(seed=1)
    base = run_walk_forward(ds, _SingleRegime, _FreqModel, n_folds=4)

    folds = make_folds(ds.trade_dates, ds.label_end_dates, n_folds=4)
    pure_oos = folds[-1].test_idx

    ds_oos_corrupt = _synthetic_dataset(seed=1)
    ds_oos_corrupt.y[pure_oos] = (ds_oos_corrupt.y[pure_oos] + 1) % 3
    corrupt = run_walk_forward(ds_oos_corrupt, _SingleRegime, _FreqModel, n_folds=4)
    assert np.array_equal(base.proba, corrupt.proba)

    # Positive control: position 0 is training data in every fold, so corrupting
    # it DOES change predictions — proving the test above isn't vacuous.
    ds_train_corrupt = _synthetic_dataset(seed=1)
    ds_train_corrupt.y[0] = (ds_train_corrupt.y[0] + 1) % 3
    changed = run_walk_forward(ds_train_corrupt, _SingleRegime, _FreqModel, n_folds=4)
    assert not np.array_equal(base.proba, changed.proba)


def test_result_covers_all_test_samples_once():
    ds = _synthetic_dataset()
    result = run_walk_forward(ds, _SingleRegime, _FreqModel, n_folds=5)
    folds = make_folds(ds.trade_dates, ds.label_end_dates, n_folds=5)
    expected = sum(len(f.test_idx) for f in folds)
    assert len(result.trade_dates) == expected
    assert result.proba.shape == (expected, 3)
