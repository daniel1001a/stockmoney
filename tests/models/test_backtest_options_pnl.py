from datetime import date

import numpy as np
import pytest

from stockmoney.models.backtest_options_pnl import (
    build_option_outcomes,
    to_ev_outcomes,
)
from stockmoney.models.ev_gate import TradeOutcome

# Three signals: UP (favorable), RANGE (no trade), DOWN (favorable for a put).
TRADE_DATES = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
LABEL_END = [date(2024, 1, 9), date(2024, 1, 10), date(2024, 1, 11)]
PROBA = np.array([[0.2, 0.1, 0.7], [0.1, 0.8, 0.1], [0.7, 0.1, 0.2]])  # UP, RANGE, DOWN
FWD = np.array([0.08, 0.0, -0.08])       # underlying up / flat / down
SPOT = np.array([100.0, 100.0, 100.0])
IV = np.array([0.30, 0.30, 0.30])
REGIME = np.array([2, 0, 1])


def _trades(**over):
    kw = dict(
        trade_dates=TRADE_DATES, label_end_dates=LABEL_END, proba=PROBA,
        fwd_return=FWD, entry_spot=SPOT, entry_iv=IV, regime=REGIME,
    )
    kw.update(over)
    return build_option_outcomes(**kw)


def test_range_signal_produces_no_trade():
    trades = _trades()
    assert len(trades) == 2  # RANGE row dropped
    assert all(t.trade_date != date(2024, 1, 3) for t in trades)


def test_up_buys_a_call_down_buys_a_put():
    trades = _trades()
    up_trade = next(t for t in trades if t.trade_date == date(2024, 1, 2))
    down_trade = next(t for t in trades if t.trade_date == date(2024, 1, 4))
    assert up_trade.position == 1     # call
    assert down_trade.position == -1  # put


def test_favorable_moves_are_profitable_options():
    # +8% under a call and -8% under a put, only ~7 days of a 30-DTE elapsed,
    # IV constant -> both should be net-positive even after the spread.
    trades = _trades()
    assert all(t.pnl_gross > 0 for t in trades)


def test_regime_is_carried_through():
    trades = _trades()
    up_trade = next(t for t in trades if t.trade_date == date(2024, 1, 2))
    assert up_trade.regime == 2


def test_bad_entry_iv_skips_the_trade():
    trades = _trades(entry_iv=np.array([0.0, 0.30, 0.30]))  # UP row has junk IV
    assert all(t.trade_date != date(2024, 1, 2) for t in trades)


def test_to_ev_outcomes_maps_fields_and_drops_regime():
    trades = _trades()
    outcomes = to_ev_outcomes(trades)
    assert len(outcomes) == len(trades)
    assert all(isinstance(o, TradeOutcome) for o in outcomes)
    o0 = outcomes[0]
    assert o0.trade_date == trades[0].trade_date
    assert o0.pnl_gross == pytest.approx(trades[0].pnl_gross)
    assert not hasattr(o0, "regime")


def test_days_held_uses_calendar_gap():
    # A larger entry->exit calendar gap means more theta decay; a flat-underlying
    # long option should lose more over 14 days than over 3.
    flat_proba = np.array([[0.2, 0.1, 0.7]])
    short = build_option_outcomes(
        [date(2024, 1, 2)], [date(2024, 1, 5)], flat_proba, np.array([0.0]),
        np.array([100.0]), np.array([0.30]), np.array([0]),
    )[0]
    long = build_option_outcomes(
        [date(2024, 1, 2)], [date(2024, 1, 16)], flat_proba, np.array([0.0]),
        np.array([100.0]), np.array([0.30]), np.array([0]),
    )[0]
    assert long.pnl_gross < short.pnl_gross < 0
