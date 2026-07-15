import math

import pytest

from stockmoney.data.options_math import bs_delta
from stockmoney.models.feature_matrix import DOWN, RANGE, UP
from stockmoney.models.option_selection import (
    SelectionParams,
    select_option,
    strike_for_delta,
)
from stockmoney.models.strike_ladder import is_on_ladder


def test_strike_reproduces_target_delta_call():
    spot, iv, t, target, r = 100.0, 0.3, 30 / 365, 0.40, 0.045
    k = strike_for_delta(spot, iv, t, target, is_call=True, r=r)
    assert bs_delta(spot, k, t, iv, r, is_call=True) == pytest.approx(0.40, abs=1e-6)


def test_strike_reproduces_target_delta_put():
    spot, iv, t, target, r = 100.0, 0.3, 30 / 365, 0.40, 0.045
    k = strike_for_delta(spot, iv, t, target, is_call=False, r=r)
    # put delta is negative; magnitude should hit the target.
    assert bs_delta(spot, k, t, iv, r, is_call=False) == pytest.approx(-0.40, abs=1e-6)


def test_otm_call_strike_above_spot_otm_put_below():
    call_k = strike_for_delta(100.0, 0.3, 30 / 365, 0.40, is_call=True)
    put_k = strike_for_delta(100.0, 0.3, 30 / 365, 0.40, is_call=False)
    assert call_k > 100.0   # 40-delta call is OTM
    assert put_k < 100.0    # 40-delta put is OTM


def test_atm_target_delta_is_near_spot():
    k = strike_for_delta(100.0, 0.3, 30 / 365, 0.50, is_call=True)
    assert k == pytest.approx(100.0, rel=0.02)


def test_up_selects_a_call_down_selects_a_put():
    up = select_option(UP, 100.0, 0.3)
    down = select_option(DOWN, 100.0, 0.3)
    assert up is not None and up.is_call and up.side == "long"
    assert down is not None and not down.is_call


def test_range_selects_no_trade():
    assert select_option(RANGE, 100.0, 0.3) is None


def test_bad_iv_yields_no_trade():
    assert select_option(UP, 100.0, 0.0) is None
    assert select_option(UP, 100.0, None) is None


def test_invalid_direction_raises():
    with pytest.raises(ValueError):
        select_option(7, 100.0, 0.3)


def test_dte_flows_into_entry():
    entry = select_option(UP, 100.0, 0.3, params=SelectionParams(dte_days=45))
    assert entry.t_years == pytest.approx(45 / 365)


# --- snap param: off by default (research/backtest path stays continuous) --

def test_snap_defaults_off_strike_stays_continuous():
    entry = select_option(UP, 967.65, 0.4)  # NFLX-like spot
    assert not is_on_ladder(entry.strike)  # the raw BS-inverted strike, uncorrected


def test_snap_true_produces_a_listed_strike():
    entry = select_option(UP, 967.65, 0.4, snap=True)  # NFLX-like spot
    assert is_on_ladder(entry.strike)


def test_snap_true_preserves_call_put_direction():
    up = select_option(UP, 100.0, 0.3, snap=True)
    down = select_option(DOWN, 100.0, 0.3, snap=True)
    assert up.strike > 100.0  # still OTM after snapping
    assert down.strike < 100.0
    assert is_on_ladder(up.strike)
    assert is_on_ladder(down.strike)
