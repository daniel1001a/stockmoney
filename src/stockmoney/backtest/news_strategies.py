"""Experiment 3 (news-as-signal) scoreboard plug-ins.

Two `Strategy`-protocol implementations (see `stockmoney.backtest.scoreboard`'s
module docstring: "Anyone can define a new class satisfying [the `Strategy`
protocol] and pass a custom `strategies=[...]` list to `run_scoreboard` --
nothing in this module needs to change"). This file does NOT modify
`scoreboard.py`'s core; it only imports its shared, already-leak-tested glue
(`_realized_vol`, `_assemble_direction_trades`, universe/window constants) and
adds two new registrants:

1. `sellput_otm5_newsgated` -- risk gate. REBUILD_PLAN.md sec.4.10/4.12: the
   naive sell-put strategy is the project's only strategy so far with a
   significant positive OOS mean return, but its tail is brutal (-46%/-63%)
   and a PRICE-based gate (close<SMA50 or rv>80th pctile) kills the tail but
   also kills the return to insignificance -- because that positive return IS
   the volatility-risk premium collected specifically during panics; a price
   gate can't cheaply separate "collect the premium" from "avoid the crash"
   since they're the same regime. This strategy tests whether a NEWS-based
   gate (sit out on a negative tone spike or a volume surge accompanied by
   negative tone -- i.e. "something bad is actively being reported about this
   name today", not "price already fell") can do better: cut the tail WITHOUT
   flattening the return, because it conditions on a different (and, in
   theory, more leading) signal than price itself.
   Baseline: `sellput_otm5_naive` (already registered in
   `scoreboard.default_strategies()`).

2. `news_catalyst_dir` -- directional catalyst. Buys a call on a strong
   positive tone+volume spike, a put on a strong negative tone+volume spike,
   sits out otherwise. Tests REBUILD_PLAN.md sec.4.11's reframed question:
   not "does GDELT predict direction on average" (already tested not
   significant, see phase0-timemachine-verdict / commit 0d2aa8b) but "does a
   STRONG, CORROBORATED (tone AND volume both spiking) news event predict
   short-horizon direction better than doing nothing in particular."
   Two baselines are registered for the SAME underlying trade generation
   (spec requirement): `B2_random` (beats noise?) and `long_stock` (beats
   passive holding?) -- both already registered in
   `scoreboard.default_strategies()`.

Universe: the 9 tickers `backfill_news_real.TICKER_ORG_PATTERNS` actually
covers (== `scoreboard.XSEC_UNIVERSE`, reused directly here rather than
re-declared, so the two lists can never silently drift apart). SOXL (also in
`SELLPUT_UNIVERSE`) has no GDELT organization-name pattern (it's an ETF, not
a company) -- `news_features.news_features_for_symbol` returns an all-empty
feature series for it by design, so the gated strategy below simply never
gates SOXL's sell-put leg (falls through as an ungated trade, identical to
the naive strategy for that one symbol) rather than raising. Documented, not
silently wrong: flagged again in NEWS_EXPERIMENT_REPORT.md.

Thresholds below (`NEWS_TONE_Z_THRESH`, `NEWS_VOL_SURGE_Z`, `CATALYST_TONE_Z`,
`CATALYST_VOL_Z`) are v1 starting points, NOT grid-searched -- same discipline
as CLAUDE.md section 16's other un-optimized v1 risk parameters. A real
promotion decision would need a walk-forward grid search over these; this
experiment's job is only to establish whether the SIGN of the effect exists
at all with a reasonable a priori choice.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import numpy as np

from stockmoney.backtest.news_features import (
    NewsFeatureRow,
    news_features_for_symbol,
)
from stockmoney.backtest.scoreboard import (
    SELLPUT_SMA_WIN,
    SELLPUT_UNIVERSE,
    SELLPUT_VOL_WIN,
    XSEC_UNIVERSE,
    XSEC_VOL_WIN,
    ScoreboardContext,
    TradeResult,
    _assemble_direction_trades,
    _realized_vol,
)
from stockmoney.models.backtest_options_pnl import IV_RV_RATIO
from stockmoney.models.feature_matrix import DOWN, UP
from stockmoney.models.options_pnl import (
    DEFAULT_SPREAD_PCT,
    OptionEntry,
    OptionExit,
    entry_premium,
    exit_premium,
)

# Tickers with real GDELT GKG coverage (see NEWS_BACKFILL_REPORT.md) -- this
# IS scoreboard.XSEC_UNIVERSE (the same 9-name single-stock universe), kept
# as a separate, self-documenting alias rather than a re-declared literal so
# a future universe change can't make the two lists drift silently.
NEWS_SYMBOLS: frozenset[str] = frozenset(XSEC_UNIVERSE)

# --- sellput_otm5_newsgated parameters (v1, not grid-searched) -------------
SELLPUT_NEWSGATE_OTM = 0.05  # matches the naive baseline's OTM% exactly
NEWS_TONE_Z_THRESH = -1.0    # sit out if today's tone z-score <= this
NEWS_VOL_SURGE_Z = 1.5       # "volume surge" threshold (article-count z-score)

# --- news_catalyst_dir parameters (v1, not grid-searched) -------------------
CATALYST_TONE_Z = 1.5   # |tone z-score| must clear this to count as a "spike"
CATALYST_VOL_Z = 1.0    # article-count z-score must also clear this (corroboration)


# =============================================================================
# 1) sellput_otm5_newsgated
# =============================================================================


def _sell_put_trades_news_gated(
    dts: list[date],
    cl: np.ndarray,
    news_feats: list[NewsFeatureRow] | None,
    otm: float,
    hold_td: int,
    *,
    tone_z_thresh: float = NEWS_TONE_Z_THRESH,
    vol_surge_z: float = NEWS_VOL_SURGE_Z,
) -> list[tuple[date, date, float, bool]]:
    """Same option pricing/assembly as `scoreboard._sell_put_trades` (ported,
    not imported, because the gate PREDICATE itself changes -- price/SMA/RV
    vs. news z-scores), but the sit-out condition is news-based instead of
    price-based: skip the trade if today has a negative tone z-score spike,
    OR an article-volume surge that is ALSO net-negative in tone (a volume
    surge alone -- e.g. a positive earnings beat -- is not a reason to avoid
    selling a put).

    `news_feats` must be index-aligned with `dts`/`cl` (i.e. produced by
    `news_features_for_symbol(sym, dts)` on the SAME `dts` passed in here --
    `news_features.compute_news_features` guarantees `feats[i].trade_date ==
    dts[i]`). `news_feats=None` (a symbol with no GDELT coverage, e.g. SOXL)
    degrades to an UNGATED trade (identical to the naive strategy for that
    symbol) -- see module docstring.

    Leak-safety: entry sizing (`spot`/`strike`/`iv`) and the gate both use
    only `cl[:i+1]` / `news_feats[i]` (itself already leak-safe by
    `news_features.compute_news_features`'s own invariant); the only
    reference to information at `i+hold_td` is the exit resolution, exactly
    mirroring `_sell_put_trades`'s own documented leak-safety shape.
    """
    n = len(cl)
    out: list[tuple[date, date, float, bool]] = []
    start = max(SELLPUT_VOL_WIN, SELLPUT_SMA_WIN)
    for i in range(start, n - hold_td):
        rv = _realized_vol(cl, i, SELLPUT_VOL_WIN)
        if not math.isfinite(rv) or rv <= 0:
            continue
        if news_feats is not None:
            f = news_feats[i]
            assert f.trade_date == dts[i], "news_feats must be index-aligned with dts"
            neg_tone_spike = math.isfinite(f.tone_zscore) and f.tone_zscore <= tone_z_thresh
            vol_surge_negative = (
                math.isfinite(f.volume_zscore)
                and f.volume_zscore >= vol_surge_z
                and math.isfinite(f.tone_mean)
                and f.tone_mean < 0
            )
            if neg_tone_spike or vol_surge_negative:
                continue  # negative-tone spike / bad-news volume surge -> sit out
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
        p_out = exit_premium(entry, OptionExit(spot=exit_spot, iv=None, days_held=days_cal))
        half = DEFAULT_SPREAD_PCT / 2.0
        proceeds_in = p_in * (1.0 - half)
        cost_out = p_out * (1.0 + half)
        pnl_per_share = proceeds_in - cost_out
        collateral_return = pnl_per_share / strike
        won = exit_spot >= strike
        out.append((dts[i], dts[i + hold_td], collateral_return, won))
    return out


class SellPutNewsGatedStrategy:
    """Cash-secured OTM put, sits out on a news-based (not price-based) risk
    signal. See module docstring section 1."""

    name = "sellput_otm5_newsgated"
    baseline = "sellput_otm5_naive"

    def run(self, ctx: ScoreboardContext, horizon: int) -> list[TradeResult]:
        out: list[TradeResult] = []
        for sym in SELLPUT_UNIVERSE:
            dts, cl = ctx.closes(sym)
            feats = news_features_for_symbol(sym, dts) if sym in NEWS_SYMBOLS else None
            for td, led, collateral_return, won in _sell_put_trades_news_gated(
                dts, cl, feats, SELLPUT_NEWSGATE_OTM, horizon
            ):
                out.append(TradeResult(td, led, collateral_return, won, None))
        return out


# =============================================================================
# 2) news_catalyst_dir
# =============================================================================


def _news_catalyst_trades(
    dts: list[date],
    cl: np.ndarray,
    news_feats: list[NewsFeatureRow],
    horizon: int,
    *,
    tone_z: float = CATALYST_TONE_Z,
    vol_z: float = CATALYST_VOL_Z,
) -> list[tuple[date, date, int, float, float, float]]:
    """One symbol's directional catalyst trades: a strong, corroborated
    (tone AND volume both spiking) news day triggers a directional option;
    every other day produces no trade. Returns raw tuples in
    `_assemble_direction_trades`'s expected shape
    (trade_date, label_end_date, direction, fwd_return, entry_spot, entry_iv).

    Leak-safety: `news_feats[i]` is itself leak-safe (see news_features.py);
    `rv`/`spot` use only `cl[:i+1]`; `fwd` is the label (uses `cl[i+horizon]`),
    exactly mirroring every other direction-trade assembler in this project
    (`scoreboard.XsecTopKStrategy`/`B1EWAllStrategy`).
    """
    n = len(cl)
    start = XSEC_VOL_WIN
    raw: list[tuple[date, date, int, float, float, float]] = []
    for i in range(start, n - horizon):
        f = news_feats[i]
        assert f.trade_date == dts[i], "news_feats must be index-aligned with dts"
        if not (math.isfinite(f.tone_zscore) and math.isfinite(f.volume_zscore)):
            continue
        if f.volume_zscore < vol_z:
            continue  # no corroborating volume surge -> not a "catalyst" day
        if f.tone_zscore >= tone_z:
            direction = UP
        elif f.tone_zscore <= -tone_z:
            direction = DOWN
        else:
            continue  # volume spiked but tone isn't extreme enough either way
        rv = _realized_vol(cl, i, XSEC_VOL_WIN)
        if not math.isfinite(rv) or rv <= 0:
            continue
        spot = float(cl[i])
        fwd = float(cl[i + horizon] / cl[i] - 1.0)
        raw.append((dts[i], dts[i + horizon], direction, fwd, spot, rv * IV_RV_RATIO))
    return raw


class NewsCatalystDirStrategy:
    """Directional option on a strong, corroborated (tone+volume) news
    catalyst; no trade otherwise. See module docstring section 2. Same trade
    generation registered twice under two different `.baseline`s (spec
    requires comparing against BOTH B2_random and long_stock; the `Strategy`
    protocol only carries one `.baseline` slot, so two instances is the
    straightforward way to get both comparisons out of `run_scoreboard`
    without touching scoreboard.py's core)."""

    def __init__(self, baseline: str):
        self.name = "news_catalyst_dir"
        self.baseline = baseline

    def run(self, ctx: ScoreboardContext, horizon: int) -> list[TradeResult]:
        raw: list[tuple[date, date, int, float, float, float]] = []
        for sym in XSEC_UNIVERSE:
            dts, cl = ctx.closes(sym)
            feats = news_features_for_symbol(sym, dts)
            raw.extend(_news_catalyst_trades(dts, cl, feats, horizon))
        return _assemble_direction_trades(raw)


def news_strategies() -> list:
    """The two experiment-3 plug-ins, registered against BOTH baselines the
    spec asks for. Pass as (part of) `strategies=` to
    `stockmoney.backtest.scoreboard.run_scoreboard` alongside
    `default_strategies()` so the baselines (`sellput_otm5_naive`,
    `B2_random`, `long_stock`) are actually computed in the same run."""
    return [
        SellPutNewsGatedStrategy(),
        NewsCatalystDirStrategy("B2_random"),
        NewsCatalystDirStrategy("long_stock"),
    ]
