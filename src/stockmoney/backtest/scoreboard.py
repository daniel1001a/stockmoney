"""The project's standing judge: one permanent, tested "daily win-rate
scoreboard" harness that every future strategy plugs into.

This consolidates three ad-hoc scripts (scripts/timemachine_report.py,
scripts/xsec_trend_backtest.py, scripts/premium_selling_backtest.py) into a
single reusable registry + runner. It does NOT reimplement pricing or
walk-forward -- every number here is produced by the same leak-safe modules
those scripts already used:

    - stockmoney.models.walk_forward.run_walk_forward   (purge/embargo OOS)
    - stockmoney.models.regime.KMeansGMMTrack            (regime track)
    - stockmoney.models.direction.LogisticDirectionModel (per-regime direction)
    - stockmoney.models.backtest_options_pnl.build_option_outcomes
      (Black-Scholes reprice, theta, bid-ask spread -- the long-option engine)
    - stockmoney.models.options_pnl.entry_premium/exit_premium
      (the short-put engine, reused directly for premium-selling)
    - stockmoney.models.metrics.bootstrap_mean_ci

What IS new here (pure data-assembly glue, previously inlined ad-hoc in each
script, now given one home): leak-free momentum/realized-vol/SMA feature
computation (`_realized_vol`, `_sma`, `_trend_proba`), and the sell-put
strike/gate assembly (`_sell_put_trades`). Each is a straight port of the
algorithm already validated by the three scripts' own printed output (see
tests/backtest/test_scoreboard.py's smoke test, which checks this module's
numbers against those scripts' known-good runs).

Design so new strategies plug in WITHOUT touching this file's core:
`run_scoreboard` only depends on the `Strategy` protocol (a `.name`, a
`.baseline` name-or-None, and a `.run(ctx, horizon) -> list[TradeResult]`
method). Anyone can define a new class satisfying that protocol and pass a
custom `strategies=[...]` list to `run_scoreboard` -- nothing in this module
needs to change.

CLAUDE.md section 12 discipline preserved throughout: everything is
walk-forward OOS, net of costs, and a merged single win rate is never
reported when a per-regime breakdown is available -- `StrategyReport.per_regime`
carries the regime split whenever the underlying strategy has regime
information (only the SOXL-anchored strategies do; cross-sectional and
premium-selling strategies pool across a multi-symbol universe with no single
regime axis, so their `per_regime` is empty by construction, not omitted by
oversight).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Protocol

import duckdb
import numpy as np

from stockmoney.models.backtest_options_pnl import IV_RV_RATIO, build_option_outcomes
from stockmoney.models.direction import LogisticDirectionModel
from stockmoney.models.feature_matrix import DOWN, RANGE, UP, build_feature_matrix, to_dataset
from stockmoney.models.feature_matrix import _load_closes as _soxl_load_closes
from stockmoney.models.metrics import bootstrap_mean_ci
from stockmoney.models.options_pnl import (
    DEFAULT_SPREAD_PCT,
    OptionEntry,
    OptionExit,
    entry_premium,
    exit_premium,
)
from stockmoney.models.regime import KMeansGMMTrack
from stockmoney.models.walk_forward import run_walk_forward

DEFAULT_HORIZONS: tuple[int, ...] = (2, 3, 5)
N_FOLDS = 5

# --- SOXL-anchored family (old direction model / trend / B1 / B2) ----------
TREND_LOOKBACK = 20
TREND_ADX_MIN = 20.0  # a priori canonical config from timemachine_report.py's CANON
B2_N_SEEDS = 25

# --- cross-sectional family (xsec top-K) ------------------------------------
XSEC_UNIVERSE = ["AAPL", "AMD", "AMZN", "AVGO", "GOOGL", "META", "MSFT", "NVDA", "TSM"]
XSEC_LOOKBACK = 20
XSEC_VOL_WIN = 20

# --- premium-selling family (sell-put) --------------------------------------
SELLPUT_UNIVERSE = ["AAPL", "AMD", "AMZN", "AVGO", "GOOGL", "META", "MSFT", "NVDA", "TSM", "SOXL"]
SELLPUT_VOL_WIN = 20
SELLPUT_SMA_WIN = 50
SELLPUT_VOL_LOOKBACK = 126
SELLPUT_VOL_PCTL = 80


# =============================================================================
# Trade result + Strategy protocol
# =============================================================================


@dataclass(frozen=True)
class TradeResult:
    """One realized trade in the strategy's own natural units.

    `pnl` is a fractional return (on premium for long options, on collateral
    for short puts, on price for a plain long-stock baseline) -- NOT
    cross-strategy-comparable by magnitude, but each strategy's own win_rate/
    mean_ret/CI are computed consistently from this shape.

    `win` is carried explicitly rather than derived from `pnl > 0` in the
    aggregator: for a long option this coincides with pnl>0, but for a short
    put the natural "win" is "expired out-of-the-money" (struck-price test),
    which is NOT always identical to pnl>0 once bid-ask spread is netted in
    (a barely-ITM assignment can still net a hair of positive P&L). Getting
    this right matters -- see the module docstring's reference to the smoke
    test, which pins the sell-put win rate to the OTM-struck definition.

    `regime` is None when the strategy has no single regime axis to report
    against (cross-sectional / premium-selling universes).
    """

    trade_date: date
    label_end_date: date
    pnl: float
    win: bool
    regime: int | None = None


class Strategy(Protocol):
    name: str
    baseline: str | None  # name of a registered baseline strategy, or None

    def run(self, ctx: "ScoreboardContext", horizon: int) -> list[TradeResult]: ...


# =============================================================================
# Shared, leak-free glue (data assembly only -- no pricing/walk-forward logic
# is reimplemented here; see module docstring)
# =============================================================================


def _realized_vol(close: np.ndarray, i: int, win: int) -> float:
    """Annualized realized vol from the `win` daily log returns trailing AND
    including index i. Uses only close[i-win : i+1] -- nothing at index > i."""
    if i < win:
        return float("nan")
    seg = close[i - win : i + 1]
    rets = np.diff(np.log(seg))
    sd = float(np.std(rets, ddof=1)) if len(rets) > 1 else 0.0
    return sd * math.sqrt(252.0)


def _sma(cl: np.ndarray, i: int, w: int) -> float:
    """Simple moving average of the w closes trailing AND including index i."""
    return float(cl[i - w + 1 : i + 1].mean()) if i >= w - 1 else float("nan")


def _trend_proba(
    trade_dates: list[date],
    closes: list[tuple[date, float]],
    adx_by_date: dict[date, float],
    lookback: int,
    adx_min: float | None,
) -> np.ndarray:
    """順勢 (trend) signal, leak-free: at each trade_date use only closes up to
    and including that date. Direction = sign of `lookback`-day momentum;
    gated to no-trade (RANGE) when adx_14 < adx_min. Emits a synthetic (n,3)
    proba so it flows through the same build_option_outcomes machinery as
    every other strategy here (ported verbatim from
    scripts/timemachine_report.py's `_trend_proba`)."""
    vals = [c for _, c in closes]
    idx = {d: i for i, (d, _) in enumerate(closes)}
    p = np.zeros((len(trade_dates), 3))
    for k, td in enumerate(trade_dates):
        i = idx.get(td)
        if i is None or i < lookback:
            p[k, RANGE] = 1.0
            continue
        if adx_min is not None and adx_by_date.get(td, 0.0) < adx_min:
            p[k, RANGE] = 1.0  # chop regime -> no directional option
            continue
        mom = vals[i] / vals[i - lookback] - 1.0
        p[k, UP if mom > 0 else (DOWN if mom < 0 else RANGE)] = 1.0
    return p


def _assemble_direction_trades(
    trades: list[tuple[date, date, int, float, float, float]],
) -> list[TradeResult]:
    """trades: (trade_date, label_end_date, direction, fwd_return, entry_spot,
    entry_iv). Routes through build_option_outcomes (the shared long-option
    engine) with a placeholder regime axis (no single regime applies across a
    multi-symbol universe), then maps to TradeResult with regime=None."""
    if not trades:
        return []
    n = len(trades)
    tds = [t[0] for t in trades]
    leds = [t[1] for t in trades]
    proba = np.zeros((n, 3))
    for i, t in enumerate(trades):
        proba[i, t[2]] = 1.0
    fwd = np.array([t[3] for t in trades])
    spot = np.array([t[4] for t in trades])
    iv = np.array([t[5] for t in trades])
    regime = np.zeros(n, dtype=int)
    outcomes = build_option_outcomes(tds, leds, proba, fwd, spot, iv, regime)
    return [
        TradeResult(o.trade_date, o.label_end_date, o.pnl_gross, o.pnl_gross > 0, None)
        for o in outcomes
    ]


def _sell_put_trades(
    dts: list[date], cl: np.ndarray, otm: float, hold_td: int, gate: bool
) -> list[tuple[date, date, float, bool]]:
    """Cash-secured OTM put, held to expiry. Returns (trade_date,
    label_end_date, collateral_return, won) per decision date. Ported
    verbatim from scripts/premium_selling_backtest.py's `_sell_put_trades`.

    Leak-free: entry sizing (spot/strike/iv, and the gate's SMA50/vol-
    percentile) uses only cl[:i+1]; the only reference to cl[i+1:] is the
    resolution at exit (cl[i+hold_td]), which is the label, not a feature --
    exactly analogous to feature_matrix's fwd_return. See
    tests/backtest/test_scoreboard.py's leak-safety test."""
    n = len(cl)
    out: list[tuple[date, date, float, bool]] = []
    rvs = np.array([_realized_vol(cl, j, SELLPUT_VOL_WIN) for j in range(n)])
    start = max(SELLPUT_VOL_WIN, SELLPUT_SMA_WIN)
    for i in range(start, n - hold_td):
        rv = _realized_vol(cl, i, SELLPUT_VOL_WIN)
        if not math.isfinite(rv) or rv <= 0:
            continue
        if gate:
            sma = _sma(cl, i, SELLPUT_SMA_WIN)
            past_rv = rvs[max(0, i - SELLPUT_VOL_LOOKBACK) : i]
            past_rv = past_rv[np.isfinite(past_rv)]
            vol_hi = (
                np.percentile(past_rv, SELLPUT_VOL_PCTL) if len(past_rv) > 20 else np.inf
            )
            if (math.isfinite(sma) and cl[i] < sma) or rv > vol_hi:
                continue  # downtrend or panic -> sit out
        spot = float(cl[i])
        strike = spot * (1.0 - otm)
        days_cal = max((dts[i + hold_td] - dts[i]).days, 1)
        entry = OptionEntry(
            spot=spot, strike=strike, is_call=False, iv=rv * IV_RV_RATIO,
            t_years=days_cal / 365.0, side="short",
        )
        p_in = entry_premium(entry)
        if p_in <= 0:
            continue
        exit_spot = float(cl[i + hold_td])
        # held to expiry: exit premium = intrinsic (t_exit -> 0)
        p_out = exit_premium(entry, OptionExit(spot=exit_spot, iv=None, days_held=days_cal))
        half = DEFAULT_SPREAD_PCT / 2.0
        proceeds_in = p_in * (1.0 - half)
        cost_out = p_out * (1.0 + half)
        pnl_per_share = proceeds_in - cost_out
        collateral_return = pnl_per_share / strike
        won = exit_spot >= strike  # put expired OTM -> kept premium
        out.append((dts[i], dts[i + hold_td], collateral_return, won))
    return out


# =============================================================================
# Shared context: caches expensive per-symbol/per-horizon computation so
# multiple strategies (e.g. MODEL/TREND/B1/B2 all sharing one SOXL
# walk-forward) never redo the same fit or DB query.
# =============================================================================


@dataclass
class _SoxlWalkForward:
    trade_dates: list[date]
    label_end_dates: list[date]
    proba: np.ndarray
    fwd_return: np.ndarray
    regime: np.ndarray
    entry_spot: np.ndarray
    entry_iv: np.ndarray
    closes: list[tuple[date, float]]
    adx_by_date: dict[date, float]


def _build_soxl_walk_forward(conn: duckdb.DuckDBPyConnection, horizon: int) -> _SoxlWalkForward:
    matrix = build_feature_matrix(conn, target_symbol="SOXL", sector="semiconductor", horizon=horizon)
    if matrix.height == 0:
        raise ValueError(
            "empty SOXL feature matrix -- wrong DB? point STOCKMONEY_DB at stockmoney_live.duckdb"
        )
    dataset = to_dataset(matrix)

    wf = run_walk_forward(
        dataset,
        lambda: KMeansGMMTrack(method="gmm", n_regimes=3, seed=0),
        lambda: LogisticDirectionModel(seed=0, calibrate="sigmoid"),
        n_folds=N_FOLDS,
    )

    rv_by_date = {d: float(x[0]) for d, x in zip(dataset.trade_dates, dataset.X)}  # realized_vol_20d = col 0
    adx_by_date = {d: float(x[1]) for d, x in zip(dataset.trade_dates, dataset.X)}  # adx_14 = col 1
    closes = _soxl_load_closes(conn, "SOXL")
    close_by_date = dict(closes)
    entry_spot = np.array([close_by_date[d] for d in wf.trade_dates])
    entry_iv = np.array([rv_by_date[d] * IV_RV_RATIO for d in wf.trade_dates])

    return _SoxlWalkForward(
        trade_dates=wf.trade_dates,
        label_end_dates=wf.label_end_dates,
        proba=wf.proba,
        fwd_return=wf.fwd_return,
        regime=wf.regime,
        entry_spot=entry_spot,
        entry_iv=entry_iv,
        closes=closes,
        adx_by_date=adx_by_date,
    )


@dataclass
class ScoreboardContext:
    """Per-run cache shared by every Strategy.run() call. Not part of the
    Strategy protocol's contract -- a new Strategy is free to ignore it and
    query `conn` directly; it exists purely so the standard registry doesn't
    redo the same expensive walk-forward fit or DB read once per strategy."""

    conn: duckdb.DuckDBPyConnection
    _soxl_cache: dict[int, _SoxlWalkForward] = field(default_factory=dict)
    _closes_cache: dict[str, tuple[list[date], np.ndarray]] = field(default_factory=dict)
    _xsec_cache: tuple[list[date], dict[str, np.ndarray]] | None = field(default=None, repr=False)

    def soxl_walk_forward(self, horizon: int) -> _SoxlWalkForward:
        if horizon not in self._soxl_cache:
            self._soxl_cache[horizon] = _build_soxl_walk_forward(self.conn, horizon)
        return self._soxl_cache[horizon]

    def closes(self, symbol: str) -> tuple[list[date], np.ndarray]:
        if symbol not in self._closes_cache:
            rows = self.conn.execute(
                "SELECT trade_date, close FROM ohlcv_daily WHERE symbol=? AND close IS NOT NULL "
                "ORDER BY trade_date",
                [symbol],
            ).fetchall()
            dts = [r[0] for r in rows]
            arr = np.array([float(r[1]) for r in rows])
            self._closes_cache[symbol] = (dts, arr)
        return self._closes_cache[symbol]

    def xsec_universe(self) -> tuple[list[date], dict[str, np.ndarray]]:
        if self._xsec_cache is None:
            per: dict[str, dict[date, float]] = {}
            for sym in XSEC_UNIVERSE:
                dts, arr = self.closes(sym)
                per[sym] = dict(zip(dts, arr.tolist()))
            common = sorted(set.intersection(*[set(m) for m in per.values()]))
            closes = {sym: np.array([per[sym][d] for d in common]) for sym in XSEC_UNIVERSE}
            self._xsec_cache = (common, closes)
        return self._xsec_cache


# =============================================================================
# Registered strategies
# =============================================================================


class OldDirectionModelStrategy:
    """The GMM-regime + per-regime logistic direction model (module A v1)."""

    name = "old_direction_model"
    baseline = "B2_random"  # does the model beat random on EV-after-costs?

    def run(self, ctx: ScoreboardContext, horizon: int) -> list[TradeResult]:
        wf = ctx.soxl_walk_forward(horizon)
        outcomes = build_option_outcomes(
            wf.trade_dates, wf.label_end_dates, wf.proba, wf.fwd_return,
            wf.entry_spot, wf.entry_iv, wf.regime,
        )
        return [
            TradeResult(o.trade_date, o.label_end_date, o.pnl_gross, o.pnl_gross > 0, o.regime)
            for o in outcomes
        ]


class TrendCanonStrategy:
    """順勢 canonical config (L20 momentum, ADX>=20 gate), pre-committed a
    priori in scripts/timemachine_report.py -- never tuned on the OOS set."""

    name = f"trend_canon_L{TREND_LOOKBACK}_adx{int(TREND_ADX_MIN)}"
    baseline = "B1_always_long"  # does the trend heartbeat beat naked-long?

    def run(self, ctx: ScoreboardContext, horizon: int) -> list[TradeResult]:
        wf = ctx.soxl_walk_forward(horizon)
        proba = _trend_proba(wf.trade_dates, wf.closes, wf.adx_by_date, TREND_LOOKBACK, TREND_ADX_MIN)
        outcomes = build_option_outcomes(
            wf.trade_dates, wf.label_end_dates, proba, wf.fwd_return,
            wf.entry_spot, wf.entry_iv, wf.regime,
        )
        return [
            TradeResult(o.trade_date, o.label_end_date, o.pnl_gross, o.pnl_gross > 0, o.regime)
            for o in outcomes
        ]


class B1AlwaysLongStrategy:
    """Baseline: buy a call every OOS day (the market's upward-drift floor)."""

    name = "B1_always_long"
    baseline = None

    def run(self, ctx: ScoreboardContext, horizon: int) -> list[TradeResult]:
        wf = ctx.soxl_walk_forward(horizon)
        n = len(wf.trade_dates)
        proba = np.zeros((n, 3))
        proba[:, UP] = 1.0
        outcomes = build_option_outcomes(
            wf.trade_dates, wf.label_end_dates, proba, wf.fwd_return,
            wf.entry_spot, wf.entry_iv, wf.regime,
        )
        return [
            TradeResult(o.trade_date, o.label_end_date, o.pnl_gross, o.pnl_gross > 0, o.regime)
            for o in outcomes
        ]


class B2RandomStrategy:
    """Baseline: buy a call-or-put at random each day, pooled over many seeds
    (the "average person's random short-dated option")."""

    name = "B2_random"
    baseline = None

    def run(self, ctx: ScoreboardContext, horizon: int) -> list[TradeResult]:
        wf = ctx.soxl_walk_forward(horizon)
        n = len(wf.trade_dates)
        out: list[TradeResult] = []
        for s in range(B2_N_SEEDS):
            rng = np.random.default_rng(1000 + s)
            proba = np.zeros((n, 3))
            picks = rng.choice([DOWN, UP], size=n)
            proba[np.arange(n), picks] = 1.0
            outcomes = build_option_outcomes(
                wf.trade_dates, wf.label_end_dates, proba, wf.fwd_return,
                wf.entry_spot, wf.entry_iv, wf.regime,
            )
            out.extend(
                TradeResult(o.trade_date, o.label_end_date, o.pnl_gross, o.pnl_gross > 0, o.regime)
                for o in outcomes
            )
        return out


class XsecTopKStrategy:
    """Cross-sectional 順勢: long calls on the K strongest names (by L20
    momentum) each day, across the 9-name single-stock universe."""

    def __init__(self, k: int):
        self.k = k
        self.name = f"XSEC_top{k}"
        self.baseline = "B1_EW_all"  # does selection itself beat plain breadth?

    def run(self, ctx: ScoreboardContext, horizon: int) -> list[TradeResult]:
        common, closes = ctx.xsec_universe()
        n = len(common)
        start = max(XSEC_LOOKBACK, XSEC_VOL_WIN)
        idxs = list(range(start, n - horizon))
        if not idxs:
            return []

        mom = {s: np.array([closes[s][i] / closes[s][i - XSEC_LOOKBACK] - 1.0 for i in idxs]) for s in XSEC_UNIVERSE}
        iv = {s: np.array([_realized_vol(closes[s], i, XSEC_VOL_WIN) * IV_RV_RATIO for i in idxs]) for s in XSEC_UNIVERSE}
        fwd = {s: np.array([closes[s][i + horizon] / closes[s][i] - 1.0 for i in idxs]) for s in XSEC_UNIVERSE}
        spot = {s: np.array([closes[s][i] for i in idxs]) for s in XSEC_UNIVERSE}
        dts = [common[i] for i in idxs]
        leds = [common[i + horizon] for i in idxs]

        raw: list[tuple[date, date, int, float, float, float]] = []
        for k_i in range(len(idxs)):
            ranked = sorted(XSEC_UNIVERSE, key=lambda s: mom[s][k_i], reverse=True)
            for s in ranked[: self.k]:
                if not math.isfinite(iv[s][k_i]) or iv[s][k_i] <= 0:
                    continue
                raw.append((dts[k_i], leds[k_i], UP, float(fwd[s][k_i]), float(spot[s][k_i]), float(iv[s][k_i])))
        return _assemble_direction_trades(raw)


class B1EWAllStrategy:
    """Baseline: long a call on EVERY name every day (breadth, no selection)."""

    name = "B1_EW_all"
    baseline = None

    def run(self, ctx: ScoreboardContext, horizon: int) -> list[TradeResult]:
        common, closes = ctx.xsec_universe()
        n = len(common)
        start = max(XSEC_LOOKBACK, XSEC_VOL_WIN)
        raw: list[tuple[date, date, int, float, float, float]] = []
        for i in range(start, n - horizon):
            for s in XSEC_UNIVERSE:
                rv = _realized_vol(closes[s], i, XSEC_VOL_WIN) * IV_RV_RATIO
                if not math.isfinite(rv) or rv <= 0:
                    continue
                raw.append(
                    (common[i], common[i + horizon], UP,
                     float(closes[s][i + horizon] / closes[s][i] - 1.0),
                     float(closes[s][i]), float(rv))
                )
        return _assemble_direction_trades(raw)


class SellPutStrategy:
    """Cash-secured OTM put, naive or gated (Lin's 情緒差空手 discipline:
    skip downtrend / vol-panic names using only past closes)."""

    def __init__(self, otm: float, gated: bool):
        self.otm = otm
        self.gated = gated
        tag = "gated" if gated else "naive"
        self.name = f"sellput_otm{int(round(otm * 100))}_{tag}"
        self.baseline = "long_stock" if not gated else f"sellput_otm{int(round(otm * 100))}_naive"

    def run(self, ctx: ScoreboardContext, horizon: int) -> list[TradeResult]:
        out: list[TradeResult] = []
        for sym in SELLPUT_UNIVERSE:
            dts, cl = ctx.closes(sym)
            for td, led, collateral_return, won in _sell_put_trades(dts, cl, self.otm, horizon, self.gated):
                out.append(TradeResult(td, led, collateral_return, won, None))
        return out


class LongStockStrategy:
    """Baseline for premium-selling: plain buy-and-hold over the same
    universe/horizon, on the same capital basis discussion as the sell-put's
    return-on-collateral (see premium_selling_backtest.py's docstring)."""

    name = "long_stock"
    baseline = None

    def run(self, ctx: ScoreboardContext, horizon: int) -> list[TradeResult]:
        out: list[TradeResult] = []
        start = max(SELLPUT_VOL_WIN, SELLPUT_SMA_WIN)
        for sym in SELLPUT_UNIVERSE:
            dts, cl = ctx.closes(sym)
            n = len(cl)
            for i in range(start, n - horizon):
                ret = float(cl[i + horizon] / cl[i] - 1.0)
                out.append(TradeResult(dts[i], dts[i + horizon], ret, ret > 0, None))
        return out


def default_strategies() -> list[Strategy]:
    """The standard registry. New strategies do not need to be added here --
    pass a custom `strategies=[...]` list to `run_scoreboard` instead."""
    return [
        B1AlwaysLongStrategy(),
        B2RandomStrategy(),
        OldDirectionModelStrategy(),
        TrendCanonStrategy(),
        B1EWAllStrategy(),
        XsecTopKStrategy(1),
        XsecTopKStrategy(2),
        XsecTopKStrategy(3),
        LongStockStrategy(),
        SellPutStrategy(0.03, False),
        SellPutStrategy(0.03, True),
        SellPutStrategy(0.05, False),
        SellPutStrategy(0.05, True),
        SellPutStrategy(0.07, False),
        SellPutStrategy(0.07, True),
    ]


# =============================================================================
# Aggregation + reporting
# =============================================================================


@dataclass(frozen=True)
class RegimeSlice:
    regime: int
    n: int
    win_rate: float
    mean_ret: float
    ci_lo: float
    ci_hi: float


@dataclass(frozen=True)
class StrategyReport:
    strategy: str
    horizon: int
    n: int
    win_rate: float
    mean_ret: float
    ci_lo: float
    ci_hi: float
    baseline: str | None
    baseline_mean_ret: float | None
    beats_baseline: bool | None
    per_regime: tuple[RegimeSlice, ...]


@dataclass(frozen=True)
class ScoreboardResult:
    horizons: tuple[int, ...]
    reports: list[StrategyReport]

    def get(self, strategy: str, horizon: int) -> StrategyReport | None:
        for r in self.reports:
            if r.strategy == strategy and r.horizon == horizon:
                return r
        return None


def _regime_slices(trades: list[TradeResult]) -> tuple[RegimeSlice, ...]:
    regimes = sorted({t.regime for t in trades if t.regime is not None})
    if not regimes:
        return ()
    slices = []
    for r in regimes:
        sub = [t for t in trades if t.regime == r]
        pnls = np.array([t.pnl for t in sub])
        wins = np.array([t.win for t in sub])
        mean_ret, lo, hi = bootstrap_mean_ci(pnls)
        slices.append(
            RegimeSlice(
                regime=r, n=len(sub), win_rate=float(wins.mean()),
                mean_ret=mean_ret, ci_lo=lo, ci_hi=hi,
            )
        )
    return tuple(slices)


def run_scoreboard(
    conn: duckdb.DuckDBPyConnection,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    strategies: list[Strategy] | None = None,
) -> ScoreboardResult:
    """Run every registered strategy over every horizon, walk-forward OOS, net
    of costs. Per CLAUDE.md section 12: a per-regime breakdown is always
    reported alongside the pooled number when regime information exists --
    never merged into a single win rate.
    """
    strategies = strategies if strategies is not None else default_strategies()
    ctx = ScoreboardContext(conn=conn)

    built: list[StrategyReport] = []
    mean_by_key: dict[tuple[str, int], float] = {}

    for horizon in horizons:
        for strat in strategies:
            trades = strat.run(ctx, horizon)
            n = len(trades)
            pnls = np.array([t.pnl for t in trades]) if n else np.array([])
            wins = np.array([t.win for t in trades]) if n else np.array([])
            win_rate = float(wins.mean()) if n else 0.0
            mean_ret, ci_lo, ci_hi = bootstrap_mean_ci(pnls) if n else (0.0, 0.0, 0.0)
            report = StrategyReport(
                strategy=strat.name,
                horizon=horizon,
                n=n,
                win_rate=win_rate,
                mean_ret=mean_ret,
                ci_lo=ci_lo,
                ci_hi=ci_hi,
                baseline=getattr(strat, "baseline", None),
                baseline_mean_ret=None,
                beats_baseline=None,
                per_regime=_regime_slices(trades),
            )
            built.append(report)
            mean_by_key[(strat.name, horizon)] = mean_ret

    final: list[StrategyReport] = []
    for r in built:
        if r.baseline is not None and (r.baseline, r.horizon) in mean_by_key:
            bm = mean_by_key[(r.baseline, r.horizon)]
            r = replace(r, baseline_mean_ret=bm, beats_baseline=r.mean_ret > bm)
        final.append(r)

    return ScoreboardResult(horizons=tuple(horizons), reports=final)


def format_table(result: ScoreboardResult) -> str:
    lines: list[str] = []
    for h in result.horizons:
        lines.append(f"\n{'=' * 92}\nHORIZON = {h}  (walk-forward OOS, net of theta/spread costs)\n{'=' * 92}")
        lines.append(
            f"{'strategy':<28}{'n':>7}{'win%':>8}{'mean_ret':>11}{'ci_lo':>9}{'ci_hi':>9}{'vs baseline':>16}"
        )
        for r in result.reports:
            if r.horizon != h:
                continue
            if r.beats_baseline is None:
                verdict = "-"
            else:
                verdict = f"{'WIN ' if r.beats_baseline else 'lose'} vs {r.baseline}"
            sig = " *" if r.ci_lo > 0 else ""
            lines.append(
                f"{r.strategy:<28}{r.n:>7}{r.win_rate * 100:>7.1f}%{r.mean_ret:>+11.4f}"
                f"{r.ci_lo:>+9.4f}{r.ci_hi:>+9.4f}  {verdict}{sig}"
            )
            for rs in r.per_regime:
                lines.append(
                    f"    regime {rs.regime}: n={rs.n:<6} win={rs.win_rate * 100:5.1f}% "
                    f"mean_ret={rs.mean_ret:+.4f} [{rs.ci_lo:+.4f},{rs.ci_hi:+.4f}]"
                )
    return "\n".join(lines)
