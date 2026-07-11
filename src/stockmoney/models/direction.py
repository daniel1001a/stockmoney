"""Per-regime direction model (CLAUDE.md section 5): each regime gets its own
independently-fit classifier over P(down/range/up). Logistic regression is the
baseline; LightGBMDirectionModel below is CLAUDE.md section 14's originally
planned v1 algorithm, slotted behind the same interface so
`walk_forward.run_walk_forward` can compare them like-for-like.

Regimes with too few (or single-class) training samples fall back to that
regime's class-frequency prior; regimes never seen in training fall back to the
global training prior — so the model degrades gracefully instead of crashing on
a sparse regime.

Optional Platt-scaling calibration (`calibrate="sigmoid"`): raw logistic-
regression probabilities on a weak-edge signal tend to be overconfident, which
directly inflates Brier score even when ranking/accuracy is fine. Sigmoid
(not isotonic) is used because per-regime training slices here are only a few
hundred rows -- isotonic regression needs more data than that to avoid
overfitting the calibration curve itself. Calibration is fit via sklearn's
internal cross-validation strictly within the training slice already handed
to `fit()` by the walk-forward harness, so it never touches OOS test data;
that internal CV is not chronology-aware, a second-order caveat noted here
rather than hidden.
"""
from __future__ import annotations

import numpy as np
from lightgbm import LGBMClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

N_CLASSES = 3


class LogisticDirectionModel:
    def __init__(self, min_samples: int = 30, seed: int = 0, calibrate: str | None = None):
        self.min_samples = min_samples
        self.seed = seed
        self.calibrate = calibrate  # None | "sigmoid" | "isotonic"

    def fit(self, X: np.ndarray, y: np.ndarray, regimes: np.ndarray) -> None:
        self.global_prior = _prior(y)
        self.per_regime: dict[int, tuple] = {}
        for r in np.unique(regimes):
            mask = regimes == r
            Xr, yr = X[mask], y[mask]
            if len(yr) >= self.min_samples and len(np.unique(yr)) >= 2:
                scaler = StandardScaler().fit(Xr)
                base = LogisticRegression(max_iter=1000, random_state=self.seed)
                clf = self._maybe_calibrate(base, yr)
                clf.fit(scaler.transform(Xr), yr)
                self.per_regime[int(r)] = ("model", scaler, clf)
            else:
                self.per_regime[int(r)] = ("prior", _prior(yr))

    def _maybe_calibrate(self, base: LogisticRegression, yr: np.ndarray):
        if not self.calibrate:
            return base
        # CalibratedClassifierCV needs every class represented at least `cv`
        # times; fall back to the uncalibrated model rather than crash on a
        # thin regime that only just cleared min_samples.
        class_counts = np.bincount(yr)
        min_class_count = class_counts[class_counts > 0].min()
        cv = min(3, int(min_class_count))
        if cv < 2:
            return base
        return CalibratedClassifierCV(base, method=self.calibrate, cv=cv)

    def predict_proba(self, X: np.ndarray, regimes: np.ndarray) -> np.ndarray:
        out = np.zeros((len(X), N_CLASSES))
        for i, r in enumerate(regimes):
            entry = self.per_regime.get(int(r))
            if entry is None:
                out[i] = self.global_prior
            elif entry[0] == "prior":
                out[i] = entry[1]
            else:
                _, scaler, clf = entry
                p = clf.predict_proba(scaler.transform(X[i : i + 1]))[0]
                full = np.zeros(N_CLASSES)
                full[clf.classes_] = p
                out[i] = full
        return out


class LightGBMDirectionModel:
    """CLAUDE.md section 14's originally-planned v1 algorithm (LightGBM),
    slotted behind the exact same per-regime interface as
    `LogisticDirectionModel` so `walk_forward.run_walk_forward` can swap
    between them with no other code change.

    Higher `min_samples` default than the logistic baseline: gradient-boosted
    trees have more capacity to overfit a thin training slice than a linear
    model does, and this project's per-regime slices are only a few hundred
    rows even on the full 8-year history -- CLAUDE.md section 12 explicitly
    warns against over-fitting small samples. Hyperparameters (few leaves,
    shallow trees, few estimators) are deliberately conservative for the same
    reason, not tuned for best backtest score.
    """

    def __init__(self, min_samples: int = 50, seed: int = 0):
        self.min_samples = min_samples
        self.seed = seed

    def fit(self, X: np.ndarray, y: np.ndarray, regimes: np.ndarray) -> None:
        self.global_prior = _prior(y)
        self.per_regime: dict[int, tuple] = {}
        for r in np.unique(regimes):
            mask = regimes == r
            Xr, yr = X[mask], y[mask]
            if len(yr) >= self.min_samples and len(np.unique(yr)) >= 2:
                clf = LGBMClassifier(
                    n_estimators=50, num_leaves=7, max_depth=3,
                    min_child_samples=10, random_state=self.seed, verbose=-1,
                )
                clf.fit(Xr, yr)
                self.per_regime[int(r)] = ("model", clf)
            else:
                self.per_regime[int(r)] = ("prior", _prior(yr))

    def predict_proba(self, X: np.ndarray, regimes: np.ndarray) -> np.ndarray:
        out = np.zeros((len(X), N_CLASSES))
        for i, r in enumerate(regimes):
            entry = self.per_regime.get(int(r))
            if entry is None:
                out[i] = self.global_prior
            elif entry[0] == "prior":
                out[i] = entry[1]
            else:
                _, clf = entry
                p = clf.predict_proba(X[i : i + 1])[0]
                full = np.zeros(N_CLASSES)
                full[clf.classes_] = p
                out[i] = full
        return out


def _prior(y: np.ndarray) -> np.ndarray:
    if len(y) == 0:
        return np.full(N_CLASSES, 1.0 / N_CLASSES)
    counts = np.bincount(y, minlength=N_CLASSES).astype(float)
    return counts / counts.sum()
