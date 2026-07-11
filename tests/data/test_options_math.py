import math

import pytest

from stockmoney.data.options_math import (
    bs_delta,
    bs_gamma,
    is_valid_option,
    naive_gex,
    norm_cdf,
)


def test_norm_cdf_known_values():
    assert norm_cdf(0.0) == pytest.approx(0.5)
    assert norm_cdf(1.96) == pytest.approx(0.975, abs=1e-3)


def test_atm_call_delta_is_about_half():
    # At-the-money, short dated: call delta slightly above 0.5.
    delta = bs_delta(100.0, 100.0, 0.08, 0.3, 0.045, is_call=True)
    assert 0.5 < delta < 0.6


def test_put_call_delta_parity():
    # call_delta - put_delta == 1 for the same contract.
    args = dict(spot=100.0, strike=110.0, t=0.5, sigma=0.4, r=0.045)
    call = bs_delta(**args, is_call=True)
    put = bs_delta(**args, is_call=False)
    assert call - put == pytest.approx(1.0, abs=1e-9)


def test_deep_itm_call_delta_near_one_otm_near_zero():
    itm = bs_delta(100.0, 50.0, 0.25, 0.3, 0.045, is_call=True)
    otm = bs_delta(100.0, 200.0, 0.25, 0.3, 0.045, is_call=True)
    assert itm > 0.95
    assert otm < 0.05


def test_gamma_positive_and_peaks_near_atm():
    atm = bs_gamma(100.0, 100.0, 0.25, 0.3, 0.045)
    wing = bs_gamma(100.0, 150.0, 0.25, 0.3, 0.045)
    assert atm > 0
    assert atm > wing


def test_is_valid_option_rejects_junk():
    assert is_valid_option(100.0, 100.0, 0.1, 0.3)
    assert not is_valid_option(100.0, 100.0, 0.1, float("nan"))
    assert not is_valid_option(100.0, 100.0, 0.0, 0.3)  # expired
    assert not is_valid_option(100.0, 100.0, 0.1, 0.0)  # zero IV
    assert not is_valid_option(0.0, 100.0, 0.1, 0.3)    # no spot


def test_naive_gex_sign_calls_positive_puts_negative():
    # One call vs one identical put (same strike/OI) → calls push GEX positive.
    opts = [(100.0, 10.0, True), (100.0, 10.0, False)]
    t = [0.25, 0.25]
    sigma = [0.3, 0.3]
    call_only = naive_gex([opts[0]], 100.0, [t[0]], [sigma[0]], 0.045)
    put_only = naive_gex([opts[1]], 100.0, [t[1]], [sigma[1]], 0.045)
    assert call_only > 0
    assert put_only < 0
    assert call_only == pytest.approx(-put_only)


def test_naive_gex_skips_zero_oi_and_junk():
    opts = [(100.0, 0.0, True), (100.0, 10.0, True)]
    t = [0.25, 0.25]
    sigma = [0.3, 0.3]
    only_second = naive_gex(opts, 100.0, t, sigma, 0.045)
    just_second = naive_gex([opts[1]], 100.0, [t[1]], [sigma[1]], 0.045)
    assert only_second == pytest.approx(just_second)
