"""VRP gate options-P&L backtest (IMPROVEMENT_PLAN.md §S2 acceptance
criterion) — the VRP analogue of backtest_options_pnl.py.

**Instrument**: a 30-DTE ATM SPY straddle (long call + long put, same strike =
entry spot, same entry IV = VIX/100). A straddle is the natural instrument for
a volatility-only view (no directional bet), which is exactly what a VRP
signal is — it says nothing about up/down, only "will realized vol be more or
less than what's priced". Reusing options_pnl.py's per-leg pricing rather than
adding a new "straddle" primitive there keeps that module's tested surface
untouched; this file just prices two legs and sums them.

**Gate semantics**: `bias='long_vol'` -> buy the straddle (pay premium,
profit if realized vol beats VIX). `bias='short_vol'` -> sell it (collect
premium, profit if realized vol undershoots VIX). `bias='no_trade'` -> skip
the day entirely, same convention as ev_gate's position==0 / RANGE skip.

**Baseline**: "always buy" — hold the long straddle on every single resolved
day regardless of what the gate says. This is the literal acceptance-criterion
baseline (IMPROVEMENT_PLAN.md §S2: "對照『永遠買方』baseline"): does using the
VRP gate to decide buyer-vs-seller (and to skip when there's no edge) actually
beat blindly buying every day?

**No walk-forward fold split here, and why that's still honest**: unlike
module A's LightGBM/logistic direction model, vrp_gate's "prediction head" is
a running historical mean with zero fitted hyperparameters — nothing to
overfit via repeated fold reuse. Its only leakage risk is temporal (using a
future VRP to predict the past), which run_vrp_gate already forecloses by
construction (predict_vrp_expanding_mean only ever averages observations with
label_end_date < asof_date) — verified by tests/models/test_vrp_gate.py's
point-in-time and append-future-unchanged-past canaries. So every row this
backtest scores is already point-in-time "out of sample" relative to its own
future; no separate held-out split is needed on top of that.

**Reliability check** (§S2's "OOS 校準(reliability curve)" requirement):
`calibration_table` buckets observations by predicted_vrp quantile and reports
each bucket's mean predicted vs mean *realized* VRP — if the simple expanding-
mean predictor carries any information, higher-predicted buckets should show
higher realized VRP on average (monotonic, not necessarily well-calibrated in
magnitude).

Usage:
    .venv/bin/python -m stockmoney.models.backtest_vrp_gate --db data/stockmoney_live.duckdb
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date

import numpy as np

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection
from stockmoney.models.options_pnl import DEFAULT_RATE, DAYS_PER_YEAR, OptionEntry, OptionExit, entry_premium, exit_premium
from stockmoney.models.vrp import build_vrp_matrix
from stockmoney.models.vrp_gate import (
    DEFAULT_QUANTILE,
    DEFAULT_WINDOW,
    MIN_RESOLVED_OBS,
    VrpObservation,
    run_vrp_gate,
    to_vrp_observations,
)

DTE_DAYS = 30           # matches vrp.py's VIX_TENOR_DAYS convention
SPREAD_PCT = 0.05       # v1 bid-ask, same default as options_pnl.DEFAULT_SPREAD_PCT (CLAUDE.md §16)


@dataclass(frozen=True)
class StraddleTrade:
    trade_date: date
    label_end_date: date
    side: str            # 'long' | 'short'
    net_return: float     # net return on total premium, after bid-ask spread
    predicted_vrp: float
    realized_vrp: float


def _straddle_return(
    spot: float, iv: float, exit_spot: float, days_held: int, *, side: str,
    spread_pct: float = SPREAD_PCT, r: float = DEFAULT_RATE,
) -> float:
    """Combined long/short straddle return: two legs (call+put, same strike=
    spot, same iv), dollar-weighted (NOT the average of two fractional
    returns — a straddle's return is total proceeds over total cost)."""
    t_years = DTE_DAYS / DAYS_PER_YEAR
    call = OptionEntry(spot=spot, strike=spot, is_call=True, iv=iv, t_years=t_years, side=side)
    put = OptionEntry(spot=spot, strike=spot, is_call=False, iv=iv, t_years=t_years, side=side)
    exit_ = OptionExit(spot=exit_spot, iv=None, days_held=days_held)  # constant-IV, see options_pnl.py

    call_in, put_in = entry_premium(call, r=r), entry_premium(put, r=r)
    call_out, put_out = exit_premium(call, exit_, r=r), exit_premium(put, exit_, r=r)
    p_in = call_in + put_in
    if p_in <= 0:
        return 0.0
    p_out = call_out + put_out
    half = spread_pct / 2.0

    if side == "long":
        cost_in = p_in * (1.0 + half)
        proceeds_out = p_out * (1.0 - half)
        return (proceeds_out - cost_in) / cost_in
    else:
        proceeds_in = p_in * (1.0 - half)
        cost_out = p_out * (1.0 + half)
        return (proceeds_in - cost_out) / proceeds_in


def build_gated_trades(
    observations: list[VrpObservation], *, window: int, quantile: float, min_obs: int,
) -> list[StraddleTrade]:
    """The gate-directed strategy: skip 'no_trade' days, long the straddle on
    'long_vol' days, short it on 'short_vol' days."""
    gate = run_vrp_gate(observations, window=window, quantile=quantile, min_obs=min_obs)
    trades = []
    for o, g in zip(sorted(observations, key=lambda x: x.trade_date), gate):
        if g.bias == "no_trade" or o.fwd_return is None:
            continue
        side = "long" if g.bias == "long_vol" else "short"
        exit_spot = o.entry_spot * (1.0 + o.fwd_return)
        days_held = max((o.label_end_date - o.trade_date).days, 1)
        ret = _straddle_return(o.entry_spot, o.entry_iv, exit_spot, days_held, side=side)
        trades.append(
            StraddleTrade(
                trade_date=o.trade_date, label_end_date=o.label_end_date, side=side,
                net_return=ret, predicted_vrp=g.predicted_vrp, realized_vrp=o.vrp,
            )
        )
    return trades


def build_always_buy_trades(observations: list[VrpObservation]) -> list[StraddleTrade]:
    """Baseline: long the straddle every single resolved day, no gate."""
    return _unconditional_trades(observations, side="long")


def build_always_sell_trades(observations: list[VrpObservation]) -> list[StraddleTrade]:
    """Second baseline, NOT in the §S2 acceptance criterion but necessary for
    an honest verdict: short the straddle every single resolved day, no gate.
    VRP is well-documented to be negative on average (IV usually overprices
    realized vol -- see this backtest's own calibration_table output), so an
    unconditional short-vol book can look "profitable" for reasons that have
    nothing to do with the gate's skill. Comparing the GATED strategy against
    this too is what actually tells "does the gate add value" apart from
    "selling premium has a generic edge" -- CLAUDE.md §12's ban on reporting a
    cherry-picked single comparison applies here."""
    return _unconditional_trades(observations, side="short")


def _unconditional_trades(observations: list[VrpObservation], *, side: str) -> list[StraddleTrade]:
    trades = []
    for o in sorted(observations, key=lambda x: x.trade_date):
        if o.fwd_return is None:
            continue
        exit_spot = o.entry_spot * (1.0 + o.fwd_return)
        days_held = max((o.label_end_date - o.trade_date).days, 1)
        ret = _straddle_return(o.entry_spot, o.entry_iv, exit_spot, days_held, side=side)
        trades.append(
            StraddleTrade(
                trade_date=o.trade_date, label_end_date=o.label_end_date, side=side,
                net_return=ret, predicted_vrp=float("nan"), realized_vrp=o.vrp,
            )
        )
    return trades


def calibration_table(
    observations: list[VrpObservation], *, window: int, quantile: float, min_obs: int, n_buckets: int = 5,
) -> list[dict]:
    """Bucket by predicted_vrp quantile (ties to observations with a
    prediction at all), report mean predicted vs mean realized VRP per
    bucket -- the §S2 'reliability curve' acceptance check."""
    gate = run_vrp_gate(observations, window=window, quantile=quantile, min_obs=min_obs)
    ordered = sorted(observations, key=lambda x: x.trade_date)
    paired = [
        (g.predicted_vrp, o.vrp)
        for o, g in zip(ordered, gate)
        if g.predicted_vrp is not None
    ]
    if len(paired) < n_buckets:
        return []
    paired.sort(key=lambda p: p[0])
    buckets = np.array_split(np.array(paired), n_buckets)
    return [
        {
            "bucket": i,
            "n": len(b),
            "mean_predicted_vrp": float(b[:, 0].mean()),
            "mean_realized_vrp": float(b[:, 1].mean()),
        }
        for i, b in enumerate(buckets)
        if len(b) > 0
    ]


def _group_stats(rets: list[float]) -> str:
    if not rets:
        return "n=0"
    wins = [r for r in rets if r > 0]
    return f"n={len(rets)} win_rate={len(wins) / len(rets):.3f} mean_ret={statistics.fmean(rets):+.4f}"


def main(db_path: str = DEFAULT_DB_PATH, *, horizon: int = 21) -> None:
    from stockmoney.models.metrics import bootstrap_mean_ci
    from stockmoney.models.vrp import build_vrp_matrix

    conn = get_connection(db_path)
    matrix = build_vrp_matrix(conn, horizon=horizon)
    conn.close()

    if matrix.height < MIN_RESOLVED_OBS * 2:
        print(f"Only {matrix.height} resolved VRP rows -- not enough history to backtest yet.")
        print("Run scripts/backfill_market_vrp_inputs.py first (needs years of SPY+VIX history).")
        return

    observations = to_vrp_observations(matrix)
    print(f"Resolved VRP observations: {len(observations)} "
          f"({observations[0].trade_date} .. {observations[-1].trade_date})")

    # Descriptive only (all daily-resolved rows -- larger n, not a significance
    # claim, no CI is computed from this table).
    calib = calibration_table(observations, window=DEFAULT_WINDOW, quantile=DEFAULT_QUANTILE, min_obs=MIN_RESOLVED_OBS)
    print("\nCalibration (predicted VRP bucket -> realized VRP, descriptive, all daily rows):")
    for row in calib:
        print(f"  bucket {row['bucket']}: n={row['n']:4}  "
              f"predicted={row['mean_predicted_vrp']:+.4f}  realized={row['mean_realized_vrp']:+.4f}")

    # Every CI claim below uses NON-OVERLAPPING trade_dates only (every
    # horizon-th resolved row). Consecutive daily rows share ~(horizon-1)/
    # horizon of their forward window, so treating all ~2000 daily "trades" as
    # independent bootstrap samples badly understates the true uncertainty --
    # the exact autocorrelation problem metrics._nonoverlap already exists to
    # fix for module A's Sharpe. A 30-DTE straddle rolled roughly once a
    # holding period (not reopened every single day) is also the realistic
    # instrument turnover this backtest should be measuring.
    nonoverlap_obs = observations[::horizon]
    print(f"\nNon-overlapping observations for CI (every {horizon}th day): {len(nonoverlap_obs)}")

    gated = build_gated_trades(nonoverlap_obs, window=DEFAULT_WINDOW, quantile=DEFAULT_QUANTILE, min_obs=MIN_RESOLVED_OBS)
    buy_all = build_always_buy_trades(nonoverlap_obs)
    sell_all = build_always_sell_trades(nonoverlap_obs)
    buy_by_date = {t.trade_date: t.net_return for t in buy_all}
    sell_by_date = {t.trade_date: t.net_return for t in sell_all}

    print(f"\nGated strategy trades: {len(gated)}  |  baseline trades: {len(buy_all)}")
    print(f"  gated    : {_group_stats([t.net_return for t in gated])}")
    print(f"  buy-all  : {_group_stats([t.net_return for t in buy_all])}")
    print(f"  sell-all : {_group_stats([t.net_return for t in sell_all])}")

    long_trades = [t.net_return for t in gated if t.side == "long"]
    short_trades = [t.net_return for t in gated if t.side == "short"]
    print(f"  gated long_vol : {_group_stats(long_trades)}")
    print(f"  gated short_vol: {_group_stats(short_trades)}")

    if len(gated) < MIN_RESOLVED_OBS:
        print(f"\nOnly {len(gated)} non-overlapping gated trades -- too few for a bootstrap CI "
              "(need more backfilled history or a shorter horizon).")
        return

    gated_rets = np.array([t.net_return for t in gated])
    mean, lo, hi = bootstrap_mean_ci(gated_rets)
    verdict = "PROFITABLE" if lo > 0 else ("LOSING" if hi < 0 else "not distinguishable from 0")
    print(f"\nGated strategy mean return 95% CI (non-overlapping): [{lo:+.4f}, {hi:+.4f}] -> {verdict}")

    # Paired vs always-buy: isolates "does the gate's buy/sell/skip choice add
    # value" from "is a long straddle profitable at all".
    paired_buy = np.array([buy_by_date[t.trade_date] for t in gated])
    _, lo_b, hi_b = bootstrap_mean_ci(gated_rets - paired_buy)
    buy_verdict = (
        "GATE SIGNIFICANTLY BEATS always-buy" if lo_b > 0
        else ("GATE SIGNIFICANTLY WORSE than always-buy" if hi_b < 0
              else "no significant difference from always-buy")
    )
    print(f"Paired (gated - always-buy) 95% CI: [{lo_b:+.4f}, {hi_b:+.4f}] -> {buy_verdict}")

    # Paired vs always-sell: isolates "does the gate add value over the
    # generic short-vol edge" -- the honest comparison CLAUDE.md §12 demands,
    # since VRP is negative on average (see calibration table) and a book that
    # is short_vol most of the time can look profitable for free.
    paired_sell = np.array([sell_by_date[t.trade_date] for t in gated])
    _, lo_s, hi_s = bootstrap_mean_ci(gated_rets - paired_sell)
    sell_verdict = (
        "GATE SIGNIFICANTLY BEATS always-sell" if lo_s > 0
        else ("GATE SIGNIFICANTLY WORSE than always-sell" if hi_s < 0
              else "no significant difference from always-sell")
    )
    print(f"Paired (gated - always-sell) 95% CI: [{lo_s:+.4f}, {hi_s:+.4f}] -> {sell_verdict}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--horizon", type=int, default=21)
    args = parser.parse_args()
    main(args.db, horizon=args.horizon)
