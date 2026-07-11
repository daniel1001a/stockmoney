"""Production inference: fit module A/B strictly on resolved history, then
score the single most recent not-yet-resolved feature row. This is the
walk-forward harness's final, still-open fold made explicit as its own path
-- `walk_forward.py` is backtest-only (every fold there eventually resolves
so it can score itself against y_true); nothing before this module has ever
answered "what does the model say about today."

Look-ahead discipline: `build_feature_matrix(..., horizon)` already excludes
any row without a fully-elapsed horizon-day forward window from training
(see `feature_matrix.latest_unresolved_feature_rows`'s docstring for the
disjointness guarantee between the two). This module fits on exactly that
resolved set -- nothing newer is added, nothing is fit on partial/estimated
future closes -- then scores the one row `build_feature_matrix` is still
excluding today. Training and inference are separated by construction, not
by an extra date filter bolted on here.

v1 scope: only symbols/sectors with a computed `xsec_dispersion` feature
work today -- as of 2026-07-10 that's the semiconductor sector group only
(SOXL/SOXS/NVDA/AVGO/AMD/TSM). big_tech has no sector-level dispersion
feature yet (a data-engineering gap, not a modeling one); callers must
handle `predict_latest` returning None for those symbols.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import duckdb
import numpy as np

from stockmoney.models.direction import LogisticDirectionModel
from stockmoney.models.ev_gate import compute_ev, derive_trade_outcomes, expanding_stats_asof
from stockmoney.models.feature_matrix import (
    FEATURE_COLUMNS,
    REGIME_COLUMNS,
    build_feature_matrix,
    latest_unresolved_feature_rows,
    to_dataset,
)
from stockmoney.models.regime import KMeansGMMTrack
from stockmoney.models.walk_forward import run_walk_forward

DEFAULT_HORIZON = 5
DEFAULT_COST_BPS = 5.0
# Bump whenever fit/inference logic changes meaningfully -- stored alongside
# every prediction so old rows can be told apart from a future revision.
# v1 -> v2 (2026-07-10): regime clustering used to see all 6 FEATURE_COLUMNS;
# fixed to observe only the 3 CLAUDE.md section 4 columns (REGIME_COLUMNS).
# Same historical dates can now get different regime assignments, so old and
# new rows must not be silently pooled in the daily_predictions win-rate ledger.
# v2 -> v3 (2026-07-11): yield_curve_10y2y (raw, T-1 publication lag) swapped
# for yield_curve_10y2y_ffill (forward-filled level) in FEATURE_COLUMNS -- the
# league's shared trade_date anchor (league/context.py) was lagging the latest
# price day by the Treasury series' publication delay, which spuriously
# blocked the Analyst trader's same-day catalyst signals via the look-ahead
# guard. Regime clustering is unaffected (yield curve isn't a REGIME_COLUMN).
MODEL_VERSION = "gmm-logistic-v3"


@dataclass
class ProductionPrediction:
    as_of_date: date
    symbol: str
    sector: str
    horizon: int
    regime: int
    proba: np.ndarray  # (3,) P(down/range/up), index order matches feature_matrix.{DOWN,RANGE,UP}
    feature_values: dict[str, float]  # the "why" behind the call
    model_version: str


def fit_production_model(
    conn: duckdb.DuckDBPyConnection,
    *,
    target_symbol: str = "SOXL",
    sector: str = "semiconductor",
    horizon: int = DEFAULT_HORIZON,
    seed: int = 0,
) -> tuple[KMeansGMMTrack, LogisticDirectionModel] | None:
    """Fit GMM regime + per-regime logistic direction model on ALL resolved
    history -- no walk-forward split, since there's no OOS test needed here;
    the only job is to score one live row, and more training data (not a
    held-out slice) is strictly better for that.

    GMM over HMM: HANDOFF's backtest found no statistically significant OOS
    Brier difference between the two tracks, and HMM has a known degenerate-
    regime failure mode -- GMM is the safer default for an unattended
    production path. Returns None if there isn't enough resolved history to
    fit (empty feature matrix -- e.g. an unsupported symbol/sector).
    """
    matrix = build_feature_matrix(conn, target_symbol=target_symbol, sector=sector, horizon=horizon)
    if matrix.height == 0:
        return None
    dataset = to_dataset(matrix)

    track = KMeansGMMTrack(method="gmm", n_regimes=3, seed=seed)
    track.fit(dataset.regime_X)
    regimes = track.label(dataset.regime_X)

    model = LogisticDirectionModel(seed=seed, calibrate="sigmoid")
    model.fit(dataset.X, dataset.y, regimes)
    return track, model


def predict_latest(
    conn: duckdb.DuckDBPyConnection,
    *,
    target_symbol: str = "SOXL",
    sector: str = "semiconductor",
    horizon: int = DEFAULT_HORIZON,
    seed: int = 0,
) -> ProductionPrediction | None:
    """Fit on resolved history, score the single most recent unresolved row.
    Returns None if there's no unresolved row yet (feature_store not
    refreshed today) or not enough history to fit (see fit_production_model).
    """
    fitted = fit_production_model(conn, target_symbol=target_symbol, sector=sector, horizon=horizon, seed=seed)
    if fitted is None:
        return None
    track, model = fitted

    latest = latest_unresolved_feature_rows(
        conn, target_symbol=target_symbol, sector=sector, horizon=horizon, n=1
    )
    if latest.height == 0:
        return None

    X = latest.select(FEATURE_COLUMNS).to_numpy()
    regime_X = latest.select(REGIME_COLUMNS).to_numpy()
    regime = int(track.label(regime_X)[0])
    proba = model.predict_proba(X, np.array([regime]))[0]

    return ProductionPrediction(
        as_of_date=latest["trade_date"][0],
        symbol=target_symbol,
        sector=sector,
        horizon=horizon,
        regime=regime,
        proba=proba,
        feature_values={c: float(latest[c][0]) for c in FEATURE_COLUMNS},
        model_version=MODEL_VERSION,
    )


def current_ev_of_continuing(
    conn: duckdb.DuckDBPyConnection,
    *,
    target_symbol: str = "SOXL",
    sector: str = "semiconductor",
    horizon: int = DEFAULT_HORIZON,
    asof_date: date | None = None,
    cost_bps: float = DEFAULT_COST_BPS,
) -> float | None:
    """EV estimate for options_risk.MarketSnapshot.ev_of_continuing.

    Trade-outcome source: module A/B's own walk-forward backtest over the
    same symbol/sector/horizon -- the only TradeOutcome history that exists
    today (there is no live ledger of model-driven trades). This answers
    "would the model's own historical directional calls have continued to
    look good," not anything about the specific option contract a human is
    holding -- callers surfacing this value should say so.
    expanding_stats_asof is already point-in-time safe (only counts trades
    with label_end_date < asof_date), so calling it with today's date here
    is safe.
    """
    matrix = build_feature_matrix(conn, target_symbol=target_symbol, sector=sector, horizon=horizon)
    if matrix.height == 0:
        return None
    dataset = to_dataset(matrix)

    wf_result = run_walk_forward(
        dataset,
        lambda: KMeansGMMTrack(method="gmm", n_regimes=3, seed=0),
        lambda: LogisticDirectionModel(seed=0),
    )
    outcomes = derive_trade_outcomes(wf_result)
    stats = expanding_stats_asof(outcomes, asof_date or date.today())
    if stats is None:
        return None
    return compute_ev(stats, cost_bps=cost_bps)
