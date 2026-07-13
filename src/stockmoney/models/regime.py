"""Two regime-detection tracks over the same observation vector
{realized_vol_20d, adx_14, xsec_dispersion}, per CLAUDE.md section 4. Which one
wins is decided by out-of-sample performance in the walk-forward, not preset.

- `KMeansGMMTrack`: clustering. Each bar is assigned independently from its own
  observation, so there is no temporal leakage by construction.
- `HMMTrack`: a Gaussian HMM whose parameters are fit on the training slice,
  but whose per-bar regime at inference is the **filtered** (online) state
  P(state_t | obs_1..t) from a hand-rolled forward pass — NOT hmmlearn's
  smoothed posterior P(state_t | obs_1..T), which peeks at the future and would
  inject look-ahead bias. This distinction is the whole reason the forward
  filter is implemented by hand here.

All tracks fit their StandardScaler on the training slice only.
"""
from __future__ import annotations

import numpy as np
from scipy.stats import multivariate_normal
from sklearn.cluster import KMeans
from sklearn.mixture import GaussianMixture
from sklearn.preprocessing import StandardScaler

N_REGIMES = 3

# Order of the observation vector every track sees, mirroring
# feature_matrix.REGIME_COLUMNS. Kept here too so describe_regimes() can read a
# centroid positionally without importing the models.feature_matrix layer.
REGIME_OBS_COLUMNS = ("realized_vol_20d", "adx_14", "xsec_dispersion")


def describe_regimes(centroids: dict[int, tuple[float, float, float]]) -> dict[int, str]:
    """Map each regime cluster id to a human label derived from its CENTROID.

    Cluster ids from KMeans/GMM/HMM are arbitrary and unstable across fits, so a
    fixed ``{0: ..., 1: ...}`` map is meaningless on real data. And per CLAUDE.md
    section 4 a regime measures volatility / trend *strength*, never up/down
    price direction -- so the label is built from where the cluster sits on the
    realized-vol and ADX (trend-strength) axes:

        centroids: {regime_id: (realized_vol_20d, adx_14, xsec_dispersion)}
        -> {regime_id: e.g. "高波動趨勢盤" / "低波動震盪盤" / "中波動"}

    Volatility tier comes from the cluster's rank in realized vol (低/中/高波動).
    The market-mode suffix marks the clusters holding the highest / lowest ADX
    centroid: "趨勢盤" (a trending/directional MARKET session -- not up vs down,
    just "moving with conviction") vs "震盪盤" (a choppy, back-and-forth market).
    Deliberately NOT "盤整": that word is the per-stock DIRECTION label
    (up/down/range), and a regime describes the whole market's mode, not any one
    stock's call -- reusing it made "why is a 趨勢 regime showing a 盤整
    direction?" read as a contradiction when it is not (a stock can range inside
    a trending market). Pure + deterministic so it is unit-testable without a DB.
    """
    ids = list(centroids)
    if not ids:
        return {}

    vols = {r: centroids[r][0] for r in ids}
    adxs = {r: centroids[r][1] for r in ids}

    by_vol = sorted(ids, key=lambda r: vols[r])
    n = len(ids)
    vol_tier: dict[int, str] = {}
    for i, r in enumerate(by_vol):
        if n >= 3 and i == 0:
            vol_tier[r] = "低波動"
        elif n >= 3 and i == n - 1:
            vol_tier[r] = "高波動"
        elif n == 2:
            vol_tier[r] = "高波動" if i == 1 else "低波動"
        else:
            vol_tier[r] = "中波動"

    hi_adx, lo_adx = max(adxs.values()), min(adxs.values())
    labels: dict[int, str] = {}
    for r in ids:
        if hi_adx > lo_adx and adxs[r] == hi_adx:
            suffix = "趨勢盤"
        elif hi_adx > lo_adx and adxs[r] == lo_adx:
            suffix = "震盪盤"
        else:
            suffix = ""
        labels[r] = vol_tier[r] + suffix
    return labels


class KMeansGMMTrack:
    def __init__(self, method: str = "gmm", n_regimes: int = N_REGIMES, seed: int = 0):
        assert method in ("gmm", "kmeans")
        self.method = method
        self.n_regimes = n_regimes
        self.seed = seed

    def fit(self, train_X: np.ndarray) -> None:
        self.scaler = StandardScaler().fit(train_X)
        Xs = self.scaler.transform(train_X)
        if self.method == "gmm":
            self.model = GaussianMixture(
                n_components=self.n_regimes, covariance_type="full",
                random_state=self.seed,
            ).fit(Xs)
        else:
            self.model = KMeans(
                n_clusters=self.n_regimes, n_init=10, random_state=self.seed,
            ).fit(Xs)

    def label(self, X: np.ndarray) -> np.ndarray:
        # Independent per-row assignment: uses only obs_t, no leakage.
        return self.model.predict(self.scaler.transform(X)).astype(int)


class HMMTrack:
    def __init__(self, n_regimes: int = N_REGIMES, seed: int = 0):
        self.n_regimes = n_regimes
        self.seed = seed

    def fit(self, train_X: np.ndarray) -> None:
        from hmmlearn.hmm import GaussianHMM

        self.scaler = StandardScaler().fit(train_X)
        Xs = self.scaler.transform(train_X)
        model = GaussianHMM(
            n_components=self.n_regimes, covariance_type="diag",
            n_iter=100, random_state=self.seed,
        )
        model.fit(Xs)
        self.startprob = model.startprob_
        self.transmat = model.transmat_
        self.means = model.means_
        self.covars = model.covars_  # (K, F, F)

    def filtered_posteriors(self, X: np.ndarray) -> np.ndarray:
        """Online forward filter: row t's posterior uses only obs_1..t."""
        Xs = self.scaler.transform(X)
        k = self.n_regimes
        log_b = np.column_stack([
            multivariate_normal.logpdf(Xs, self.means[j], self.covars[j], allow_singular=True)
            for j in range(k)
        ])
        # Per-row constant shift cancels under normalization; keeps b numerically sane.
        b = np.exp(log_b - log_b.max(axis=1, keepdims=True))

        filtered = np.empty((len(Xs), k))
        alpha = self.startprob * b[0]
        alpha = alpha / alpha.sum() if alpha.sum() > 0 else np.full(k, 1.0 / k)
        filtered[0] = alpha
        for t in range(1, len(Xs)):
            alpha = (alpha @ self.transmat) * b[t]
            s = alpha.sum()
            alpha = alpha / s if s > 0 else np.full(k, 1.0 / k)
            filtered[t] = alpha
        return filtered

    def label(self, X: np.ndarray) -> np.ndarray:
        return self.filtered_posteriors(X).argmax(axis=1).astype(int)
