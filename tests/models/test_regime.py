import numpy as np

from stockmoney.models.regime import HMMTrack, KMeansGMMTrack, N_REGIMES, describe_regimes


def test_describe_regimes_labels_from_centroid_not_id():
    # (realized_vol, adx, dispersion): calm / neutral / stormy. The id ordering
    # is deliberately scrambled to prove the label follows the centroid, not id.
    labels = describe_regimes({2: (0.14, 13.0, 0.35), 0: (0.48, 36.0, 1.2), 1: (0.25, 21.0, 0.7)})
    assert labels[0] == "高波動趨勢盤"   # highest vol + highest adx
    assert labels[2] == "低波動震盪盤"   # lowest vol + lowest adx
    assert labels[1] == "中波動"       # middle vol, non-extreme adx
    # Never up/down direction words -- regimes measure strength, not direction.
    assert not any("多頭" in v or "空頭" in v for v in labels.values())


def test_describe_regimes_two_clusters_and_empty():
    two = describe_regimes({5: (0.2, 20.0, 0.5), 9: (0.4, 30.0, 0.9)})
    assert two == {5: "低波動震盪盤", 9: "高波動趨勢盤"}
    assert describe_regimes({}) == {}


def test_describe_regimes_equal_adx_drops_trend_suffix():
    # When ADX centroids tie, no cluster is "trending"/"ranging" -- vol tier only.
    labels = describe_regimes({0: (0.1, 20.0, 0.5), 1: (0.3, 20.0, 0.5), 2: (0.5, 20.0, 0.5)})
    assert labels == {0: "低波動", 1: "中波動", 2: "高波動"}


def _two_cluster_data(seed=0, n_per=100):
    rng = np.random.default_rng(seed)
    low = rng.normal(loc=[0.0, 0.0, 0.0], scale=0.1, size=(n_per, 3))
    high = rng.normal(loc=[5.0, 5.0, 5.0], scale=0.1, size=(n_per, 3))
    X = np.vstack([low, high])
    return X


def test_kmeans_gmm_track_separates_obvious_clusters():
    X = _two_cluster_data()
    for method in ("kmeans", "gmm"):
        track = KMeansGMMTrack(method=method, n_regimes=2, seed=0)
        track.fit(X)
        labels = track.label(X)
        # First half (low cluster) should all share one label, second half another.
        assert len(set(labels[:100].tolist())) == 1
        assert len(set(labels[100:].tolist())) == 1
        assert labels[0] != labels[-1]


def test_kmeans_gmm_label_is_independent_per_row():
    # Labeling a prefix must give identical results to labeling the full
    # series and slicing — no row depends on rows after it.
    X = _two_cluster_data()
    track = KMeansGMMTrack(method="kmeans", n_regimes=2, seed=0)
    track.fit(X)
    full = track.label(X)
    prefix = track.label(X[:50])
    assert np.array_equal(full[:50], prefix)


def _regime_switching_series(seed=0, n=400):
    """A 2-state Markov-switching series with *overlapping* emissions: a
    single observation alone is ambiguous about its state, so smoothing
    (which resolves ambiguity using future context) measurably disagrees with
    filtering (past-only) — this ambiguity is what makes the filtered-vs-
    smoothed test below meaningful rather than vacuous."""
    rng = np.random.default_rng(seed)
    means = np.array([[0.0, 0.0, 0.0], [1.5, 1.5, 1.5]])
    state = 0
    states, obs = [], []
    for _ in range(n):
        if rng.random() < 0.08:
            state = 1 - state
        states.append(state)
        obs.append(rng.normal(loc=means[state], scale=1.2))
    return np.array(obs), np.array(states)


def test_hmm_track_fits_and_labels():
    X, _ = _regime_switching_series()
    track = HMMTrack(n_regimes=2, seed=0)
    track.fit(X[:300])
    labels = track.label(X[:300])
    assert labels.shape == (300,)
    assert set(labels.tolist()) <= {0, 1}


def test_hmm_filtered_state_unchanged_by_future_observations():
    """The decisive anti-look-ahead test: filtered state at time t must be
    identical whether or not future observations exist yet. If appending rows
    changes an earlier row's label, the model is peeking at the future."""
    X, _ = _regime_switching_series(seed=1)
    train, X_prefix, X_extended = X[:250], X[250:300], X[250:400]

    track = HMMTrack(n_regimes=2, seed=0)
    track.fit(train)

    filtered_prefix = track.filtered_posteriors(X_prefix)
    filtered_extended = track.filtered_posteriors(X_extended)

    # The first len(X_prefix) rows must match exactly regardless of what
    # comes after them in the second call.
    np.testing.assert_allclose(
        filtered_prefix, filtered_extended[: len(X_prefix)], atol=1e-10
    )


def test_hmmlearn_smoothed_posterior_DOES_change_with_future_data():
    """Positive control proving the distinction matters: hmmlearn's own
    smoothed predict_proba (what a naive implementation would use) changes
    historical posteriors when future data is appended — the leak this
    module is built to avoid."""
    from hmmlearn.hmm import GaussianHMM
    from sklearn.preprocessing import StandardScaler

    X, _ = _regime_switching_series(seed=2)
    train, X_prefix, X_extended = X[:250], X[250:300], X[250:400]

    scaler = StandardScaler().fit(train)
    model = GaussianHMM(n_components=2, covariance_type="diag", n_iter=100, random_state=0)
    model.fit(scaler.transform(train))

    smoothed_prefix = model.predict_proba(scaler.transform(X_prefix))
    smoothed_extended = model.predict_proba(scaler.transform(X_extended))

    # Unlike the filtered case, these are generally NOT equal on the overlap.
    assert not np.allclose(smoothed_prefix, smoothed_extended[: len(X_prefix)], atol=1e-10)
