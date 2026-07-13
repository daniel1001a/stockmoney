from datetime import date, timedelta

import numpy as np
import pytest

from stockmoney.models.vrp_gate import (
    MIN_RESOLVED_OBS,
    VrpObservation,
    predict_vrp_expanding_mean,
    run_vrp_gate,
)

HORIZON = 5


def _synthetic_observations(n: int, vrp_values: list[float], seed=0) -> list[VrpObservation]:
    start = date(2020, 1, 1)
    out = []
    for i in range(n):
        d = start + timedelta(days=i)
        out.append(
            VrpObservation(
                trade_date=d, label_end_date=d + timedelta(days=HORIZON),
                vrp=vrp_values[i % len(vrp_values)], entry_iv=0.20, entry_spot=400.0,
                fwd_return=0.01,
            )
        )
    return out


def test_predict_vrp_expanding_mean_matches_manual_calc():
    obs = _synthetic_observations(50, [0.02, -0.01, 0.03, 0.0])
    asof = obs[-1].label_end_date + timedelta(days=1)  # all resolved
    predicted = predict_vrp_expanding_mean(obs, asof, min_obs=5)

    expected = sum(o.vrp for o in obs) / len(obs)
    assert predicted == pytest.approx(expected)


def test_predict_vrp_returns_none_below_min_obs():
    obs = _synthetic_observations(5, [0.01])
    asof = obs[-1].label_end_date + timedelta(days=1)
    assert predict_vrp_expanding_mean(obs, asof, min_obs=MIN_RESOLVED_OBS) is None


def test_predict_vrp_excludes_unresolved_observations_point_in_time():
    """Core anti-leakage property: the prediction 'as of' day d must not
    include any observation whose label hasn't resolved yet."""
    obs = _synthetic_observations(60, [0.02, -0.02])

    mid_date = obs[30].trade_date
    predicted = predict_vrp_expanding_mean(obs, mid_date, min_obs=5)

    manually_resolved = [o.vrp for o in obs if o.label_end_date < mid_date]
    assert predicted == pytest.approx(sum(manually_resolved) / len(manually_resolved))
    unresolved_nearby = [
        o for o in obs if mid_date - timedelta(days=HORIZON) <= o.trade_date < mid_date
    ]
    assert len(unresolved_nearby) > 0  # sanity: such observations exist
    assert all(o.label_end_date >= mid_date for o in unresolved_nearby)


def test_appending_future_observations_does_not_change_past_predictions():
    """Same 'append future data, past result unchanged' canary as ev_gate."""
    base = _synthetic_observations(60, [0.02, -0.01], seed=3)
    extra = _synthetic_observations(30, [0.05, -0.03], seed=4)
    offset = base[-1].trade_date - extra[0].trade_date + timedelta(days=1)
    future = [
        VrpObservation(
            o.trade_date + offset, o.label_end_date + offset, o.vrp, o.entry_iv,
            o.entry_spot, o.fwd_return,
        )
        for o in extra
    ]

    asof = base[40].trade_date
    pred_without_future = predict_vrp_expanding_mean(base, asof, min_obs=5)
    pred_with_future = predict_vrp_expanding_mean(base + future, asof, min_obs=5)

    assert pred_without_future == pred_with_future


def test_run_vrp_gate_no_trade_before_window_is_full():
    obs = _synthetic_observations(80, [0.02, -0.02], seed=5)
    results = run_vrp_gate(obs, window=90, min_obs=5)
    assert all(r.bias == "no_trade" for r in results)
    assert all(r.upper is None for r in results)


def test_run_vrp_gate_flips_both_ways_and_matches_manual_percentiles():
    # Alternating high/low VRP so predicted (expanding mean) drifts and both
    # the long_vol and short_vol branches get exercised as the mean shifts.
    n = 200
    rng = np.random.default_rng(7)
    vrp_values = [float(v) for v in rng.normal(loc=0.0, scale=0.05, size=n)]
    obs = _synthetic_observations(n, vrp_values)

    window, min_obs, quantile = 15, 5, 0.75
    results = run_vrp_gate(obs, window=window, quantile=quantile, min_obs=min_obs)

    ordered = sorted(obs, key=lambda o: o.trade_date)
    pred_history: list[float] = []
    saw_long, saw_short, saw_no_trade_after_warmup = False, False, False
    for o, r in zip(ordered, results):
        predicted = predict_vrp_expanding_mean(ordered, o.trade_date, min_obs=min_obs)
        assert r.predicted_vrp == predicted

        if predicted is not None and len(pred_history) >= window:
            recent = pred_history[-window:]
            expected_upper = float(np.percentile(recent, quantile * 100))
            expected_lower = float(np.percentile(recent, (1 - quantile) * 100))
            assert r.upper == pytest.approx(expected_upper)
            assert r.lower == pytest.approx(expected_lower)
            if predicted >= expected_upper:
                assert r.bias == "long_vol"
                saw_long = True
            elif predicted <= expected_lower:
                assert r.bias == "short_vol"
                saw_short = True
            else:
                assert r.bias == "no_trade"
                saw_no_trade_after_warmup = True
        if predicted is not None:
            pred_history.append(predicted)

    assert saw_long
    assert saw_short
    assert saw_no_trade_after_warmup


def test_run_vrp_gate_upper_never_below_lower_when_both_present():
    n = 150
    rng = np.random.default_rng(11)
    vrp_values = [float(v) for v in rng.normal(loc=0.01, scale=0.03, size=n)]
    obs = _synthetic_observations(n, vrp_values)
    results = run_vrp_gate(obs, window=20, quantile=0.75, min_obs=5)

    for r in results:
        if r.upper is not None and r.lower is not None:
            assert r.upper >= r.lower


def test_appending_future_observations_does_not_change_past_gate_results():
    base = _synthetic_observations(150, [0.02, -0.01], seed=8)
    extra = _synthetic_observations(30, [0.06, -0.04], seed=9)
    offset = base[-1].trade_date - extra[0].trade_date + timedelta(days=1)
    future = [
        VrpObservation(
            o.trade_date + offset, o.label_end_date + offset, o.vrp, o.entry_iv,
            o.entry_spot, o.fwd_return,
        )
        for o in extra
    ]

    results_base = run_vrp_gate(base, window=20, min_obs=5)
    results_extended = run_vrp_gate(base + future, window=20, min_obs=5)

    assert results_base == results_extended[: len(results_base)]
