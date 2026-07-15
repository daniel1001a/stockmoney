"""Tests for the daily win-rate scoreboard harness.

(a) Leak-safety: the harness's own leak-free glue (realized vol / SMA / trend
    momentum / sell-put assembly) must never let a decision at index i see a
    row dated after i -- the whole point of this harness existing at all.
(b) Smoke test on the live 8y DB: run_scoreboard executes and its numbers
    match the known-good output of the three scripts it consolidates
    (scripts/timemachine_report.py, scripts/premium_selling_backtest.py).

Run:
    cd /Users/danielkang/Documents/stockmoney-main
    STOCKMONEY_DB=$(pwd)/data/stockmoney_live.duckdb .venv/bin/python -m pytest tests/backtest/ -q
"""
from __future__ import annotations

import os
from datetime import date, timedelta

import duckdb
import numpy as np
import pytest

from stockmoney.backtest.scoreboard import (
    _realized_vol,
    _sell_put_trades,
    _sma,
    _trend_proba,
    run_scoreboard,
)
from stockmoney.data.db import DEFAULT_DB_PATH


def _dates(n: int) -> list[date]:
    start = date(2020, 1, 1)
    return [start + timedelta(days=i) for i in range(n)]


# =============================================================================
# (a) Leak-safety
# =============================================================================


def test_realized_vol_ignores_future_closes():
    rng = np.random.default_rng(0)
    cl = np.cumprod(1 + rng.normal(0, 0.01, size=100)) * 100
    i = 60

    rv_before = _realized_vol(cl, i, 20)

    mutated_future = cl.copy()
    mutated_future[i + 1 :] *= 5.0  # blow up every close strictly after the decision date
    assert _realized_vol(mutated_future, i, 20) == rv_before

    # Positive control: the trailing window DOES matter -- past closes affect it.
    mutated_past = cl.copy()
    mutated_past[: i + 1] *= 5.0
    assert _realized_vol(mutated_past, i, 20) != rv_before


def test_sma_ignores_future_closes():
    rng = np.random.default_rng(0)
    cl = np.cumprod(1 + rng.normal(0, 0.01, size=100)) * 100
    i = 60

    sma_before = _sma(cl, i, 50)

    mutated_future = cl.copy()
    mutated_future[i + 1 :] *= 5.0
    assert _sma(mutated_future, i, 50) == sma_before

    mutated_past = cl.copy()
    mutated_past[i - 5] *= 5.0
    assert _sma(mutated_past, i, 50) != sma_before


def test_trend_proba_ignores_future_closes():
    rng = np.random.default_rng(1)
    n = 100
    dts = _dates(n)
    cl = np.cumprod(1 + rng.normal(0, 0.01, size=n)) * 100
    closes = list(zip(dts, cl.tolist()))
    adx_by_date = {d: 25.0 for d in dts}  # always above the gate -> never RANGE-gated

    decision_idx = 50
    trade_dates = [dts[decision_idx]]
    p_before = _trend_proba(trade_dates, closes, adx_by_date, lookback=20, adx_min=20.0)

    # Blow up every close strictly AFTER the decision date -- must not move the
    # signal computed for that decision date.
    mutated = list(closes)
    for j in range(decision_idx + 1, n):
        mutated[j] = (dts[j], cl[j] * 5.0)
    p_after = _trend_proba(trade_dates, mutated, adx_by_date, lookback=20, adx_min=20.0)
    assert np.array_equal(p_before, p_after)

    # Positive control: the momentum formula is mom = close[i]/close[i-lookback]-1,
    # so forcing its lookback anchor (i-lookback) to the opposite side of
    # close[i] flips the direction -- proving the "no future leak" result
    # above isn't vacuous (i.e. isn't just because the function ignores its
    # inputs altogether). Pick the anchor value deterministically so the sign
    # is guaranteed to flip regardless of what the random series happened to do.
    mutated_past = list(closes)
    anchor = decision_idx - 20
    current = cl[decision_idx]
    was_up = current > cl[anchor]
    forced_anchor = current * 2.0 if was_up else current * 0.5  # flips mom's sign
    mutated_past[anchor] = (dts[anchor], forced_anchor)
    p_changed = _trend_proba(trade_dates, mutated_past, adx_by_date, lookback=20, adx_min=20.0)
    assert not np.array_equal(p_before, p_changed)


def test_sell_put_trades_ignore_future_closes():
    rng = np.random.default_rng(2)
    n = 120
    dts = _dates(n)
    cl = np.cumprod(1 + rng.normal(0, 0.01, size=n)) * 100
    hold_td = 5

    trades_before = _sell_put_trades(dts, cl, otm=0.05, hold_td=hold_td, gate=True)
    assert trades_before  # sanity: the synthetic series actually produced trades

    target_date, target_led, target_ret, target_won = trades_before[10]
    target_idx = dts.index(target_date)
    interior = target_idx + 1
    assert interior < target_idx + hold_td  # strictly between entry and exit

    # A decision at target_idx must be computed from cl[:target_idx+1] (entry
    # sizing + gate) plus cl[target_idx+hold_td] (the resolution/label) only --
    # nothing dated in between should matter, and nothing dated after the
    # decision's own OWN resolution should matter either.
    mutated = cl.copy()
    mutated[interior] *= 10.0
    trades_after = _sell_put_trades(dts, mutated, otm=0.05, hold_td=hold_td, gate=True)
    after_row = next(t for t in trades_after if t[0] == target_date)
    assert after_row == (target_date, target_led, target_ret, target_won)

    # Positive control: mutating the ENTRY close itself changes the strike/
    # premium sizing, so the outcome for that decision date changes.
    mutated_entry = cl.copy()
    mutated_entry[target_idx] *= 1.5
    trades_entry_mut = _sell_put_trades(dts, mutated_entry, otm=0.05, hold_td=hold_td, gate=True)
    entry_mut_row = next((t for t in trades_entry_mut if t[0] == target_date), None)
    assert entry_mut_row is None or entry_mut_row[2] != target_ret


# =============================================================================
# (b) Smoke test on the live DB
# =============================================================================

_LIVE_DB = os.environ.get("STOCKMONEY_DB", DEFAULT_DB_PATH)


@pytest.mark.skipif(not os.path.exists(_LIVE_DB), reason=f"live DB not found at {_LIVE_DB}")
def test_scoreboard_smoke_matches_known_script_outputs():
    """Cross-checked against real runs of the scripts this harness supersedes
    (2026-07-13, on data/stockmoney_live.duckdb):

        scripts.timemachine_report      MODEL   horizon=5  mean_ret=-0.0378 n=240
        scripts.premium_selling_backtest sellput_otm5_hold5td_naive win=90.4% n=29596

    Restricted to horizon=5 only -- both target numbers live there, and it
    keeps the walk-forward fit + sell-put grid from being repeated for 2 and 3
    as well (this smoke test only needs one horizon to prove the harness
    reproduces the known numbers; the CLI run separately exercises all three).
    """
    conn = duckdb.connect(_LIVE_DB, read_only=True)
    try:
        result = run_scoreboard(conn, horizons=(5,))
    finally:
        conn.close()

    assert result.reports, "run_scoreboard produced no reports at all"

    for r in result.reports:
        assert r.n > 0, f"{r.strategy} produced zero trades"
        assert np.isfinite(r.mean_ret), r.strategy
        assert np.isfinite(r.win_rate) and 0.0 <= r.win_rate <= 1.0, r.strategy
        assert np.isfinite(r.ci_lo) and np.isfinite(r.ci_hi), r.strategy
        for rs in r.per_regime:
            assert np.isfinite(rs.mean_ret) and rs.n > 0

    model = result.get("old_direction_model", 5)
    assert model is not None
    assert model.n == 240
    assert model.mean_ret == pytest.approx(-0.038, abs=0.005)

    sellput = result.get("sellput_otm5_naive", 5)
    assert sellput is not None
    assert sellput.win_rate == pytest.approx(0.90, abs=0.02)
