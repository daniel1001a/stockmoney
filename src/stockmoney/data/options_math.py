"""Black-Scholes greeks used to bucket an options chain by delta and to
estimate dealer gamma exposure. Pure functions, no I/O — unit-testable in
isolation.

These are v1 approximations built from freely-available chain snapshots
(strike / IV / open interest); CLAUDE.md section 2 explicitly flags the
options-microstructure layer as a self-estimated v1 to be revisited against a
paid feed (ORATS/CBOE) only if the methodology validates.
"""
from __future__ import annotations

import math

CONTRACT_MULTIPLIER = 100


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1(spot: float, strike: float, t: float, sigma: float, r: float) -> float:
    return (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))


def bs_delta(
    spot: float, strike: float, t: float, sigma: float, r: float, *, is_call: bool
) -> float:
    """Black-Scholes delta. Call delta in (0, 1); put delta in (-1, 0)."""
    d1 = _d1(spot, strike, t, sigma, r)
    return norm_cdf(d1) if is_call else norm_cdf(d1) - 1.0


def bs_gamma(spot: float, strike: float, t: float, sigma: float, r: float) -> float:
    """Black-Scholes gamma (same for calls and puts)."""
    d1 = _d1(spot, strike, t, sigma, r)
    return norm_pdf(d1) / (spot * sigma * math.sqrt(t))


def is_valid_option(spot: float, strike: float, t: float, sigma: float) -> bool:
    """Guard against the junk rows options snapshots are full of (zero/NaN IV,
    expired contracts, nonsensical strikes) before feeding the BS formulas."""
    return (
        spot > 0
        and strike > 0
        and t > 0
        and sigma is not None
        and not math.isnan(sigma)
        and sigma > 0
    )


def naive_gex(
    options: list[tuple[float, float, bool]],
    spot: float,
    t_by_index: list[float],
    sigma_by_index: list[float],
    r: float,
) -> float:
    """Very rough dealer gamma-exposure proxy (v1).

    ``options`` is a list of ``(strike, open_interest, is_call)``. Uses the
    common simplifying assumption that dealers are long call gamma and short
    put gamma; scales by ``spot^2`` and a 1% move, times the contract
    multiplier. The sign convention and scale are deliberately a v1 estimate
    to be validated, not a precise dealer-positioning model.
    """
    total = 0.0
    for (strike, oi, is_call), t, sigma in zip(options, t_by_index, sigma_by_index):
        if not is_valid_option(spot, strike, t, sigma) or oi <= 0:
            continue
        gamma = bs_gamma(spot, strike, t, sigma, r)
        sign = 1.0 if is_call else -1.0
        total += sign * gamma * oi * CONTRACT_MULTIPLIER
    return total * spot * spot * 0.01
