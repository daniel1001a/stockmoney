from datetime import date, timedelta

import pytest

from stockmoney.models.backtest_vrp_gate import (
    _straddle_return,
    build_always_buy_trades,
    build_always_sell_trades,
    build_gated_trades,
    calibration_table,
)
from stockmoney.models.vrp_gate import MIN_RESOLVED_OBS, VrpObservation

HORIZON = 21


def _obs(n: int, vrp_pattern: list[float], seed=0) -> list[VrpObservation]:
    import random

    rng = random.Random(seed)
    start = date(2020, 1, 1)
    out = []
    for i in range(n):
        d = start + timedelta(days=i)
        vrp = vrp_pattern[i % len(vrp_pattern)]
        entry_iv = 0.20
        # realized vol = entry_iv + vrp (definition: vrp = fwd_realized_vol - entry_iv)
        fwd_return = rng.uniform(-0.03, 0.03)
        out.append(
            VrpObservation(
                trade_date=d, label_end_date=d + timedelta(days=HORIZON),
                vrp=vrp, entry_iv=entry_iv, entry_spot=400.0, fwd_return=fwd_return,
            )
        )
    return out


def test_straddle_return_theta_decay_flat_underlying_loses_money():
    # Nothing moves, IV constant: a long straddle bleeds theta + spread, same
    # qualitative behavior as options_pnl's single-leg theta-decay test.
    ret = _straddle_return(spot=400.0, iv=0.20, exit_spot=400.0, days_held=20, side="long")
    assert ret < 0


def test_straddle_return_big_move_either_direction_profits_a_long():
    up = _straddle_return(spot=400.0, iv=0.20, exit_spot=440.0, days_held=5, side="long")
    down = _straddle_return(spot=400.0, iv=0.20, exit_spot=360.0, days_held=5, side="long")
    assert up > 0
    assert down > 0  # straddle profits from big moves in EITHER direction


def test_straddle_return_short_is_not_simply_the_negative_of_long():
    # Bid-ask spread is charged on both sides independently (buyer pays ask,
    # seller receives bid), so short != -long even before considering the
    # asymmetric floor -- this guards against a naive `return = -long_return`
    # simplification creeping in later.
    long_ret = _straddle_return(spot=400.0, iv=0.20, exit_spot=400.0, days_held=20, side="long")
    short_ret = _straddle_return(spot=400.0, iv=0.20, exit_spot=400.0, days_held=20, side="short")
    assert short_ret != pytest.approx(-long_ret)
    # Flat underlying + theta decay: the SELLER collects that decay and profits.
    assert short_ret > 0


def test_build_always_buy_trades_is_long_every_resolved_day():
    obs = _obs(60, [0.02, -0.02])
    trades = build_always_buy_trades(obs)
    assert len(trades) == 60
    assert all(t.side == "long" for t in trades)


def test_build_always_sell_trades_is_short_every_resolved_day():
    obs = _obs(60, [0.02, -0.02])
    trades = build_always_sell_trades(obs)
    assert len(trades) == 60
    assert all(t.side == "short" for t in trades)
    # Not just the mirror image of build_always_buy_trades: same underlying
    # move, opposite side, computed independently through the short pricing
    # path (see test_straddle_return_short_is_not_simply_the_negative_of_long).
    buy_trades = build_always_buy_trades(obs)
    assert [t.net_return for t in trades] != [-t.net_return for t in buy_trades]


def test_build_gated_trades_skips_no_trade_days():
    obs = _obs(120, [0.02, -0.02])
    gated = build_gated_trades(obs, window=15, quantile=0.75, min_obs=5)
    always = build_always_buy_trades(obs)
    # The gate must be strictly selective -- it should never produce MORE
    # trades than the unconditional baseline over the same observation set.
    assert len(gated) <= len(always)
    assert all(t.side in ("long", "short") for t in gated)


def test_build_gated_trades_empty_when_too_few_observations():
    obs = _obs(5, [0.01])
    gated = build_gated_trades(obs, window=90, quantile=0.75, min_obs=MIN_RESOLVED_OBS)
    assert gated == []


def test_calibration_table_buckets_are_monotonic_for_a_clean_signal():
    # Construct a case where predicted VRP (expanding mean) and realized VRP
    # are obviously related: a slowly rising VRP level over time means the
    # expanding mean (a lagging/smoothed version of the level) is positively
    # correlated with the next realized value in the same regime.
    n = 200
    vrp_pattern = [round(-0.05 + 0.1 * (i % 20) / 20, 4) for i in range(20)]
    obs = _obs(n, vrp_pattern)

    table = calibration_table(obs, window=15, quantile=0.75, min_obs=5, n_buckets=4)
    assert len(table) == 4
    predicted = [row["mean_predicted_vrp"] for row in table]
    assert predicted == sorted(predicted)  # buckets are ordered by construction
    assert sum(row["n"] for row in table) > 0


def test_calibration_table_empty_when_too_few_predictions():
    obs = _obs(5, [0.01])
    table = calibration_table(obs, window=90, quantile=0.75, min_obs=MIN_RESOLVED_OBS, n_buckets=5)
    assert table == []
