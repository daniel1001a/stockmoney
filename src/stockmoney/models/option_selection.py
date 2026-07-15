"""Signal -> concrete option (Part 1B of ~/.claude/plans/buzzing-yawning-squid.md).

Module A emits an *underlying* direction (DOWN/RANGE/UP). The options-P&L
backtest needs a *specific* option to price, decided using only entry-available
information. This module is the deterministic, pure-function bridge:

    up   -> buy a call
    down -> buy a put
    range-> no trade (mirrors ev_gate's position==0 convention)

Strike is chosen by *target delta*, not by picking a listed strike, on purpose:
the free options data (iv_surface_daily) stores IV at 25-delta / 50-delta
buckets per expiry, not a full strike grid, so there is no historical strike
ladder to pick from. Instead we invert Black-Scholes delta to the strike that
would carry the target delta at entry — the standard way options desks quote a
"40-delta call". `statistics.NormalDist().inv_cdf` supplies the inverse normal
CDF so this needs no scipy.

Look-ahead: every input (spot, iv, dte, r) is an entry-date quantity; this
function has no access to anything dated after entry. `iv` must be the entry-
date implied vol for roughly the chosen delta bucket (the caller pulls it from
iv_surface_daily as of the entry date).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

from stockmoney.models.feature_matrix import DOWN, RANGE, UP
from stockmoney.models.options_pnl import DEFAULT_RATE, DAYS_PER_YEAR, OptionEntry
from stockmoney.models.strike_ladder import snap_strike

_NORM = NormalDist()


@dataclass(frozen=True)
class SelectionParams:
    """v1 starting points (CLAUDE.md section 16: to calibrate via backtest)."""
    target_delta: float = 0.40    # |delta| of the option to buy; 0.5=ATM, lower=further OTM
    dte_days: int = 30            # days to expiry at entry (CLAUDE.md section 0: short-mid term)
    side: str = "long"           # v1 is a long-premium directional book


def strike_for_delta(
    spot: float, iv: float, t_years: float, target_delta: float, *, is_call: bool, r: float = DEFAULT_RATE
) -> float:
    """The strike whose Black-Scholes delta equals `target_delta` (magnitude).

    Inverts delta = N(d1) (call) / N(d1)-1 (put): a call at |delta| d wants
    N(d1)=d, a put wants N(d1)=1-d, then K = S*exp((r+sigma^2/2)t - d1*sigma*sqrt(t)).
    """
    p = target_delta if is_call else 1.0 - target_delta
    d1 = _NORM.inv_cdf(p)
    return spot * math.exp((r + 0.5 * iv * iv) * t_years - d1 * iv * math.sqrt(t_years))


def select_option(
    direction: int,
    spot: float,
    iv: float,
    *,
    params: SelectionParams = SelectionParams(),
    r: float = DEFAULT_RATE,
    snap: bool = False,
) -> OptionEntry | None:
    """Turn a module-A direction label into the option to trade, or None for a
    RANGE call (no directional edge -> no trade). `direction` uses the same
    {DOWN,RANGE,UP} integer labels as feature_matrix / the walk-forward result.

    `snap` (default False): whether to round the continuous Black-Scholes
    strike to the nearest realistic listed increment (strike_ladder.snap_strike).
    Defaults OFF here because this is also the entry point research backtests
    (backtest_options_pnl.py) use to evaluate the target-delta methodology
    itself on a smooth continuum -- snapping there would inject ladder-tier
    rounding noise into a methodology test that doesn't care about it. The
    live/display path (league/option_bridge.build_option_structure) is the one
    that must never show an unrealizable strike, so it passes snap=True.
    """
    if direction == RANGE:
        return None
    if direction not in (DOWN, UP):
        raise ValueError(f"direction must be one of DOWN/RANGE/UP, got {direction!r}")
    if iv is None or iv <= 0 or spot <= 0:
        return None  # can't size a strike without a usable entry IV

    is_call = direction == UP
    t_years = params.dte_days / DAYS_PER_YEAR
    strike = strike_for_delta(spot, iv, t_years, params.target_delta, is_call=is_call, r=r)
    if snap:
        strike = snap_strike(strike)
    return OptionEntry(
        spot=spot, strike=strike, is_call=is_call, iv=iv, t_years=t_years, side=params.side
    )
