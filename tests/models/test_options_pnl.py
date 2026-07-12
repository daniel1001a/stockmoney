import pytest

from stockmoney.models.options_pnl import (
    OptionEntry,
    OptionExit,
    entry_premium,
    exit_premium,
    option_return,
)


def _atm_call(**over):
    base = dict(spot=100.0, strike=100.0, is_call=True, iv=0.30, t_years=30 / 365, side="long")
    base.update(over)
    return OptionEntry(**base)


def test_side_validation():
    with pytest.raises(ValueError):
        _atm_call(side="sideways")


def test_favorable_move_makes_a_long_call_profit():
    entry = _atm_call()
    # Underlying rallies 8% with 10 days left, IV held constant.
    exit = OptionExit(spot=108.0, iv=None, days_held=10)
    assert option_return(entry, exit) > 0


def test_theta_decay_flat_underlying_loses_money():
    # Nothing moves, IV constant: a long option bleeds theta and loses (also pays
    # the round-trip spread). This is the effect a pure-underlying backtest misses.
    entry = _atm_call()
    exit = OptionExit(spot=100.0, iv=None, days_held=20)
    assert option_return(entry, exit) < 0


def test_iv_crush_hurts_a_long_even_on_a_small_up_move():
    # Small favorable move but IV halves: vega loss can swamp the tiny delta gain.
    entry = _atm_call()
    flat_iv = option_return(entry, OptionExit(spot=101.0, iv=None, days_held=5))
    crushed = option_return(entry, OptionExit(spot=101.0, iv=0.15, days_held=5))
    assert crushed < flat_iv


def test_long_loss_is_floored_near_minus_one():
    # Deep adverse move to expiry: the long can lose at most ~the premium (-1),
    # never more. The (1+half spread) on entry pushes it slightly past -1.
    entry = _atm_call()
    exit = OptionExit(spot=60.0, iv=None, days_held=30)  # expires worthless
    r = option_return(entry, exit)
    assert -1.05 < r <= -0.95


def test_short_put_keeps_premium_when_it_expires_worthless():
    # Sell an ATM put; underlying rises so it expires worthless -> seller keeps
    # ~all premium (return near +1, minus spread), never much above +1.
    entry = OptionEntry(spot=100.0, strike=100.0, is_call=False, iv=0.30, t_years=30 / 365, side="short")
    exit = OptionExit(spot=115.0, iv=None, days_held=30)
    r = option_return(entry, exit)
    assert 0.9 < r <= 1.0


def test_short_loss_is_not_floored_at_minus_one():
    # A short's loss is unbounded: a big adverse move loses well more than the
    # premium collected (unlike a long). This asymmetry is why the EV gate must
    # see the option distribution, not the underlying's.
    entry = OptionEntry(spot=100.0, strike=100.0, is_call=False, iv=0.30, t_years=30 / 365, side="short")
    exit = OptionExit(spot=70.0, iv=None, days_held=30)  # put deep ITM against seller
    assert option_return(entry, exit) < -1.0


def test_spread_cost_drags_return_down():
    entry = _atm_call()
    exit = OptionExit(spot=108.0, iv=None, days_held=10)
    frictionless = option_return(entry, exit, spread_pct=0.0)
    with_spread = option_return(entry, exit, spread_pct=0.10)
    assert with_spread < frictionless


def test_exit_premium_defaults_to_entry_iv_when_none():
    entry = _atm_call()
    # days_held=0 and IV=None and same spot -> exit mid premium == entry mid premium.
    same = exit_premium(entry, OptionExit(spot=100.0, iv=None, days_held=0))
    assert same == pytest.approx(entry_premium(entry), abs=1e-9)


def test_expired_option_uses_intrinsic_value():
    entry = _atm_call(strike=100.0)
    # Held to expiry (days_held == t at entry in days), ITM by 7.
    exit = OptionExit(spot=107.0, iv=None, days_held=30)
    # Long call bought at ~mid, exit intrinsic 7.0; return should be strongly positive.
    assert exit_premium(entry, exit) == pytest.approx(7.0, abs=1e-9)
    assert option_return(entry, exit) > 0
