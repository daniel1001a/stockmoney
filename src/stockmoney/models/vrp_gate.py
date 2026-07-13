"""VRP trading gate (IMPROVEMENT_PLAN.md §S2), the direct analogue of
ev_gate.py: turn today's *predicted* VRP into a directional-vol bias, gated on
whether the prediction sits in an extreme percentile of its own recent history.

Unlike ev_gate's single "tradeable" boolean (EV either clears the bar or it
doesn't), VRP is naturally two-sided: a strongly positive prediction favours
buying premium (straddle/directional long), a strongly negative one favours
selling it (credit spread / short premium) — see vrp.py's module docstring for
the VRP(d) = forward_realized_vol - entry_iv definition and why market-level
(VIX vs SPY) is what's backtestable today.

**The "prediction head" here is deliberately the simplest defensible one**:
the expanding (point-in-time) mean of every VRP observation already resolved
before the decision date. This is not a stand-in for "we'll build a real model
later" — VRP has a well-documented persistent positive long-run mean (IV
richness) with strong autocorrelation, so a running historical mean is a
legitimate first forecast, the same spirit as ev_gate's own trailing P(win)/
avg_win/avg_loss estimate. CLAUDE.md §16 explicitly endorses starting from a
simple estimator and calibrating later.

Point-in-time discipline, same shape as ev_gate.py: a VRP observation at
trade_date d only "resolves" (forward_realized_vol becomes knowable) once its
`label_end_date` has passed. `predict_vrp_expanding_mean(d)` and the rolling
percentile window both only ever look at observations with label_end_date < d.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import polars as pl

MIN_RESOLVED_OBS = 10
DEFAULT_WINDOW = 90         # mirrors ev_gate.py's v1 starting point (CLAUDE.md §16, to calibrate)
DEFAULT_QUANTILE = 0.75     # two-sided: >= 75th pct -> long vol, <= 25th pct -> short vol


@dataclass
class VrpObservation:
    trade_date: date
    label_end_date: date
    vrp: float
    entry_iv: float
    entry_spot: float
    fwd_return: float | None


def to_vrp_observations(matrix: pl.DataFrame) -> list[VrpObservation]:
    """Adapt vrp.build_vrp_matrix's polars output to the plain dataclass this
    module works with -- same decoupling ev_gate.derive_trade_outcomes gives
    WalkForwardResult, so this module has no polars/duckdb dependency."""
    return [
        VrpObservation(
            trade_date=r["trade_date"], label_end_date=r["label_end_date"],
            vrp=r["vrp"], entry_iv=r["entry_iv"], entry_spot=r["entry_spot"],
            fwd_return=r["fwd_return"],
        )
        for r in matrix.sort("trade_date").to_dicts()
    ]


def predict_vrp_expanding_mean(
    observations: list[VrpObservation], asof_date: date, *, min_obs: int = MIN_RESOLVED_OBS
) -> float | None:
    """Point-in-time VRP forecast for `asof_date`: mean VRP of every
    observation resolved strictly before it. None if fewer than `min_obs`
    resolved observations exist yet (insufficient sample -> caller must treat
    as "no prediction", never assume an edge)."""
    resolved = [o.vrp for o in observations if o.label_end_date < asof_date]
    if len(resolved) < min_obs:
        return None
    return sum(resolved) / len(resolved)


@dataclass
class VrpGateResult:
    trade_date: date
    predicted_vrp: float | None    # None if too few resolved observations to predict yet
    upper: float | None            # None until the rolling reference window is full
    lower: float | None
    bias: str                      # 'long_vol' | 'short_vol' | 'no_trade'


def run_vrp_gate(
    observations: list[VrpObservation],
    *,
    window: int = DEFAULT_WINDOW,
    quantile: float = DEFAULT_QUANTILE,
    min_obs: int = MIN_RESOLVED_OBS,
) -> list[VrpGateResult]:
    """Walk observations in date order, computing predicted_vrp[d] from
    expanding point-in-time history, then gating on whether it sits at/above
    the `quantile` percentile (long_vol) or at/below the `1-quantile`
    percentile (short_vol) of the most recent `window` *previously predicted*
    values (never including today's own prediction). Requires a full window
    of prior predictions before gating turns on -- early observations default
    to bias='no_trade' rather than being let through for free, exactly like
    ev_gate.run_ev_gate's `threshold is None` early period.
    """
    ordered = sorted(observations, key=lambda o: o.trade_date)
    pred_history: list[float] = []
    results: list[VrpGateResult] = []

    for o in ordered:
        predicted = predict_vrp_expanding_mean(ordered, o.trade_date, min_obs=min_obs)

        upper: float | None = None
        lower: float | None = None
        bias = "no_trade"
        if predicted is not None and len(pred_history) >= window:
            recent = pred_history[-window:]
            upper = float(np.percentile(recent, quantile * 100))
            lower = float(np.percentile(recent, (1.0 - quantile) * 100))
            if predicted >= upper:
                bias = "long_vol"
            elif predicted <= lower:
                bias = "short_vol"

        results.append(
            VrpGateResult(
                trade_date=o.trade_date, predicted_vrp=predicted, upper=upper, lower=lower, bias=bias,
            )
        )
        if predicted is not None:
            pred_history.append(predicted)

    return results
