from datetime import date, timedelta

import numpy as np
import pytest

from stockmoney.models.ev_gate import (
    MIN_RESOLVED_TRADES,
    TradeOutcome,
    TrailingStats,
    compute_ev,
    derive_trade_outcomes,
    expanding_stats_asof,
    run_ev_gate,
)
from stockmoney.models.walk_forward import WalkForwardResult

HORIZON = 5


def _proba_for(position: int) -> np.ndarray:
    # position in {-1, 0, +1} -> argmax at index {0, 1, 2}
    p = np.zeros(3)
    p[position + 1] = 1.0
    return p


def _result(positions: list[int], fwd_returns: list[float], start=date(2020, 1, 1)):
    n = len(positions)
    trade_dates = [start + timedelta(days=i) for i in range(n)]
    label_end_dates = [d + timedelta(days=HORIZON) for d in trade_dates]
    proba = np.array([_proba_for(p) for p in positions])
    return WalkForwardResult(
        trade_dates=trade_dates,
        label_end_dates=label_end_dates,
        y_true=np.zeros(n, dtype=int),
        proba=proba,
        regime=np.zeros(n, dtype=int),
        fwd_return=np.array(fwd_returns),
        n_folds=1,
    )


def test_derive_trade_outcomes_skips_range_and_computes_gross_pnl():
    result = _result(
        positions=[1, 0, -1, 1],
        fwd_returns=[0.02, 0.05, 0.01, -0.03],
    )
    outcomes = derive_trade_outcomes(result)
    assert len(outcomes) == 3  # the position==0 (range) day is dropped
    assert outcomes[0].position == 1 and outcomes[0].pnl_gross == pytest.approx(0.02)
    assert outcomes[1].position == -1 and outcomes[1].pnl_gross == pytest.approx(-0.01)
    assert outcomes[2].position == 1 and outcomes[2].pnl_gross == pytest.approx(-0.03)


def _synthetic_outcomes(n: int, win_rate: float, win_size: float, loss_size: float, seed=0):
    rng = np.random.default_rng(seed)
    start = date(2020, 1, 1)
    outcomes = []
    for i in range(n):
        d = start + timedelta(days=i)
        is_win = rng.random() < win_rate
        pnl = win_size if is_win else -loss_size
        outcomes.append(
            TradeOutcome(
                trade_date=d, label_end_date=d + timedelta(days=HORIZON),
                position=1, pnl_gross=pnl,
            )
        )
    return outcomes


def test_expanding_stats_matches_manual_calc():
    outcomes = _synthetic_outcomes(50, win_rate=0.6, win_size=0.02, loss_size=0.01, seed=1)
    asof = outcomes[-1].label_end_date + timedelta(days=1)  # all resolved
    stats = expanding_stats_asof(outcomes, asof, min_trades=5)

    wins = [o.pnl_gross for o in outcomes if o.pnl_gross > 0]
    losses = [-o.pnl_gross for o in outcomes if o.pnl_gross <= 0]
    assert stats.n_win == len(wins)
    assert stats.n_loss == len(losses)
    assert stats.p_win == pytest.approx(len(wins) / len(outcomes))
    assert stats.avg_win == pytest.approx(sum(wins) / len(wins))
    assert stats.avg_loss == pytest.approx(sum(losses) / len(losses))


def test_expanding_stats_returns_none_below_min_trades():
    outcomes = _synthetic_outcomes(5, win_rate=0.6, win_size=0.02, loss_size=0.01)
    asof = outcomes[-1].label_end_date + timedelta(days=1)
    assert expanding_stats_asof(outcomes, asof, min_trades=MIN_RESOLVED_TRADES) is None


def test_expanding_stats_returns_none_if_only_one_side_present():
    start = date(2020, 1, 1)
    all_wins = [
        TradeOutcome(start + timedelta(days=i), start + timedelta(days=i + HORIZON), 1, 0.01)
        for i in range(20)
    ]
    asof = all_wins[-1].label_end_date + timedelta(days=1)
    assert expanding_stats_asof(all_wins, asof, min_trades=5) is None


def test_expanding_stats_asof_excludes_unresolved_trades_point_in_time():
    """The core anti-leakage property: stats 'as of' day d must not include
    any trade whose label hasn't resolved yet (label_end_date >= d)."""
    outcomes = _synthetic_outcomes(60, win_rate=0.5, win_size=0.02, loss_size=0.02, seed=2)

    mid_date = outcomes[30].trade_date
    stats_at_mid = expanding_stats_asof(outcomes, mid_date, min_trades=5)

    manually_resolved = [o for o in outcomes if o.label_end_date < mid_date]
    assert stats_at_mid.n_win + stats_at_mid.n_loss == len(manually_resolved)
    # None of the trades opened in [mid_date - horizon, mid_date) should count,
    # since their label window extends to/past mid_date.
    unresolved_nearby = [
        o for o in outcomes
        if mid_date - timedelta(days=HORIZON) <= o.trade_date < mid_date
    ]
    assert len(unresolved_nearby) > 0  # sanity: such trades exist in this fixture
    assert all(o.label_end_date >= mid_date for o in unresolved_nearby)


def test_appending_future_trades_does_not_change_past_stats():
    """Same 'append future data, past result unchanged' canary used for
    module A's feature functions and HMM filtering, applied here."""
    base = _synthetic_outcomes(60, win_rate=0.55, win_size=0.02, loss_size=0.015, seed=3)
    extra = _synthetic_outcomes(30, win_rate=0.55, win_size=0.02, loss_size=0.015, seed=4)
    # Shift the extra trades to start right after `base` ends.
    offset = base[-1].trade_date - extra[0].trade_date + timedelta(days=1)
    future = [
        TradeOutcome(o.trade_date + offset, o.label_end_date + offset, o.position, o.pnl_gross)
        for o in extra
    ]

    asof = base[40].trade_date
    stats_without_future = expanding_stats_asof(base, asof, min_trades=5)
    stats_with_future = expanding_stats_asof(base + future, asof, min_trades=5)

    assert stats_without_future == stats_with_future


def test_compute_ev_known_values():
    stats = TrailingStats(n_win=6, n_loss=4, p_win=0.6, avg_win=0.02, avg_loss=0.01)
    # EV = 0.6*0.02 - 0.4*0.01 - cost
    ev = compute_ev(stats, cost_bps=10.0)  # 10bps = 0.001
    expected = 0.6 * 0.02 - 0.4 * 0.01 - 0.001
    assert ev == pytest.approx(expected)


def test_compute_ev_cost_only_subtracted_once():
    stats = TrailingStats(n_win=5, n_loss=5, p_win=0.5, avg_win=0.01, avg_loss=0.01)
    ev_no_cost = compute_ev(stats, cost_bps=0.0)
    ev_with_cost = compute_ev(stats, cost_bps=20.0)
    assert ev_no_cost - ev_with_cost == pytest.approx(20.0 / 1e4)


def test_run_ev_gate_not_tradeable_before_window_is_full():
    outcomes = _synthetic_outcomes(80, win_rate=0.7, win_size=0.02, loss_size=0.01, seed=5)
    results = run_ev_gate(outcomes, window=90, min_trades=5, cost_bps=5.0)
    # Fewer than 90 resolved EV values ever accumulate in this fixture, so
    # nothing should ever be marked tradeable, regardless of how good EV looks.
    assert all(r.tradeable is False for r in results)
    assert all(r.threshold is None for r in results)


def test_run_ev_gate_threshold_matches_manual_percentile_and_flips_both_ways():
    outcomes = _synthetic_outcomes(150, win_rate=0.6, win_size=0.02, loss_size=0.012, seed=6)
    window, min_trades, cost_bps = 20, 5, 5.0
    results = run_ev_gate(outcomes, window=window, min_trades=min_trades, cost_bps=cost_bps)

    ordered = sorted(outcomes, key=lambda o: o.trade_date)
    ev_history = []
    checked_tradeable, checked_not_tradeable = False, False
    for o, r in zip(ordered, results):
        stats = expanding_stats_asof(ordered, o.trade_date, min_trades=min_trades)
        ev = compute_ev(stats, cost_bps=cost_bps) if stats is not None else None
        assert r.ev == ev

        if ev is not None and len(ev_history) >= window:
            expected_threshold = float(np.percentile(ev_history[-window:], 75))
            assert r.threshold == pytest.approx(expected_threshold)
            assert r.tradeable == (ev >= expected_threshold)
            checked_tradeable = checked_tradeable or r.tradeable
            checked_not_tradeable = checked_not_tradeable or not r.tradeable
        if ev is not None:
            ev_history.append(ev)

    # Both branches of the boundary comparison must actually be exercised,
    # not just always-true or always-false (by construction ~25% should pass).
    assert checked_tradeable
    assert checked_not_tradeable


def test_appending_future_trades_does_not_change_past_gate_results():
    base = _synthetic_outcomes(150, win_rate=0.55, win_size=0.02, loss_size=0.015, seed=8)
    extra = _synthetic_outcomes(30, win_rate=0.55, win_size=0.02, loss_size=0.015, seed=9)
    offset = base[-1].trade_date - extra[0].trade_date + timedelta(days=1)
    future = [
        TradeOutcome(o.trade_date + offset, o.label_end_date + offset, o.position, o.pnl_gross)
        for o in extra
    ]

    results_base = run_ev_gate(base, window=20, min_trades=5, cost_bps=5.0)
    results_extended = run_ev_gate(base + future, window=20, min_trades=5, cost_bps=5.0)

    assert results_base == results_extended[: len(results_base)]
