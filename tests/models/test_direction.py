import numpy as np

from stockmoney.models.direction import LightGBMDirectionModel, LogisticDirectionModel, N_CLASSES


def _separable_data(n_per=50, seed=0):
    rng = np.random.default_rng(seed)
    # Class 0 near [-2,-2,-2], class 2 near [2,2,2], class 1 near origin.
    X0 = rng.normal(loc=[-2, -2, -2], scale=0.3, size=(n_per, 3))
    X1 = rng.normal(loc=[0, 0, 0], scale=0.3, size=(n_per, 3))
    X2 = rng.normal(loc=[2, 2, 2], scale=0.3, size=(n_per, 3))
    X = np.vstack([X0, X1, X2])
    y = np.array([0] * n_per + [1] * n_per + [2] * n_per)
    return X, y


def test_predict_proba_shape_and_sums_to_one():
    X, y = _separable_data()
    regimes = np.zeros(len(y), dtype=int)
    model = LogisticDirectionModel(min_samples=10)
    model.fit(X, y, regimes)

    proba = model.predict_proba(X, regimes)
    assert proba.shape == (len(y), N_CLASSES)
    np.testing.assert_allclose(proba.sum(axis=1), 1.0, atol=1e-6)


def test_fitted_model_recovers_separable_classes():
    X, y = _separable_data()
    regimes = np.zeros(len(y), dtype=int)
    model = LogisticDirectionModel(min_samples=10)
    model.fit(X, y, regimes)

    proba = model.predict_proba(X, regimes)
    assert (proba.argmax(axis=1) == y).mean() > 0.95


def test_sparse_regime_falls_back_to_prior():
    X, y = _separable_data()
    regimes = np.zeros(len(y), dtype=int)
    regimes[:5] = 1  # regime 1 has only 5 samples, below min_samples
    model = LogisticDirectionModel(min_samples=30)
    model.fit(X, y, regimes)

    assert model.per_regime[1][0] == "prior"
    test_regimes = np.array([1, 1])
    proba = model.predict_proba(X[:2], test_regimes)
    # Prior for regime 1's 5 samples (all class 0) → deterministic [1,0,0].
    np.testing.assert_allclose(proba[0], [1.0, 0.0, 0.0])


def test_single_class_regime_falls_back_to_prior():
    X, y = _separable_data()
    regimes = np.zeros(len(y), dtype=int)
    model = LogisticDirectionModel(min_samples=10)
    # Force a regime with 40 samples but only one class present.
    regimes[:40] = 2
    y2 = y.copy()
    y2[:40] = 0
    model.fit(X, y2, regimes)
    assert model.per_regime[2][0] == "prior"


def test_unseen_regime_at_predict_time_falls_back_to_global_prior():
    X, y = _separable_data()
    regimes = np.zeros(len(y), dtype=int)
    model = LogisticDirectionModel(min_samples=10)
    model.fit(X, y, regimes)

    proba = model.predict_proba(X[:1], np.array([99]))  # regime never seen in training
    np.testing.assert_allclose(proba[0], model.global_prior)


def test_calibrated_model_still_predicts_valid_probabilities():
    X, y = _separable_data(n_per=60)
    regimes = np.zeros(len(y), dtype=int)
    model = LogisticDirectionModel(min_samples=10, calibrate="sigmoid")
    model.fit(X, y, regimes)

    assert model.per_regime[0][0] == "model"
    proba = model.predict_proba(X, regimes)
    assert proba.shape == (len(y), N_CLASSES)
    np.testing.assert_allclose(proba.sum(axis=1), 1.0, atol=1e-6)
    # Calibration shouldn't destroy a strongly separable signal.
    assert (proba.argmax(axis=1) == y).mean() > 0.9


def test_calibration_falls_back_to_uncalibrated_when_regime_too_thin_for_cv():
    X, y = _separable_data(n_per=60)
    regimes = np.zeros(len(y), dtype=int)
    # min_samples=10 lets a very small, minority-heavy slice through, too
    # thin for CalibratedClassifierCV's internal folds.
    regimes[:10] = 1
    y2 = y.copy()
    y2[:9] = 0
    y2[9] = 1
    model = LogisticDirectionModel(min_samples=10, calibrate="sigmoid")
    model.fit(X, y2, regimes)

    # Must not raise, and must still produce valid probabilities.
    proba = model.predict_proba(X[:1], np.array([1]))
    np.testing.assert_allclose(proba.sum(), 1.0, atol=1e-6)


def test_lightgbm_predict_proba_shape_and_sums_to_one():
    X, y = _separable_data()
    regimes = np.zeros(len(y), dtype=int)
    model = LightGBMDirectionModel(min_samples=10)
    model.fit(X, y, regimes)

    proba = model.predict_proba(X, regimes)
    assert proba.shape == (len(y), N_CLASSES)
    np.testing.assert_allclose(proba.sum(axis=1), 1.0, atol=1e-6)


def test_lightgbm_fitted_model_recovers_separable_classes():
    X, y = _separable_data()
    regimes = np.zeros(len(y), dtype=int)
    model = LightGBMDirectionModel(min_samples=10)
    model.fit(X, y, regimes)

    proba = model.predict_proba(X, regimes)
    assert (proba.argmax(axis=1) == y).mean() > 0.95


def test_lightgbm_sparse_regime_falls_back_to_prior():
    X, y = _separable_data()
    regimes = np.zeros(len(y), dtype=int)
    regimes[:5] = 1  # regime 1 has only 5 samples, below min_samples
    model = LightGBMDirectionModel(min_samples=30)
    model.fit(X, y, regimes)

    assert model.per_regime[1][0] == "prior"
    proba = model.predict_proba(X[:2], np.array([1, 1]))
    np.testing.assert_allclose(proba[0], [1.0, 0.0, 0.0])


def test_lightgbm_single_class_regime_falls_back_to_prior():
    X, y = _separable_data()
    regimes = np.zeros(len(y), dtype=int)
    model = LightGBMDirectionModel(min_samples=10)
    regimes[:40] = 2
    y2 = y.copy()
    y2[:40] = 0
    model.fit(X, y2, regimes)
    assert model.per_regime[2][0] == "prior"


def test_lightgbm_unseen_regime_at_predict_time_falls_back_to_global_prior():
    X, y = _separable_data()
    regimes = np.zeros(len(y), dtype=int)
    model = LightGBMDirectionModel(min_samples=10)
    model.fit(X, y, regimes)

    proba = model.predict_proba(X[:1], np.array([99]))
    np.testing.assert_allclose(proba[0], model.global_prior)
