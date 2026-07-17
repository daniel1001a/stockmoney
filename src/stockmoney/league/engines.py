"""Trader engines: each turns a per-symbol MarketContext into one unified-shape
call (direction / conviction / rationale / invalidation), or a skip. Adding a
new trader philosophy = adding one engine here and registering it in
ENGINE_REGISTRY; no schema change.

Both v1 engines are deliberately read-only over already-computed data and make
NO LLM/API calls (CLAUDE.md: never a paid API; the Analyst's deep reasoning
already ran in the OpenClaw claude-cli cron that wrote catalyst_signals):
- ChartistEngine reads the ProductionPrediction already on the context.
- AnalystEngine reads the latest catalyst_signals row for the symbol.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

import duckdb
import numpy as np

from stockmoney.data.catalyst_signals import get_latest_catalyst_for_symbol
from stockmoney.league.context import MarketContext
from stockmoney.models import production
from stockmoney.models.feature_matrix import DOWN, RANGE, UP

# Pinned method versions -- must match the rows seeded in
# 032_trader_methods.sql so every prediction pins a catalogued version.
CHARTIST_METHOD_VERSION = f"chartist:{production.MODEL_VERSION}"
ANALYST_METHOD_VERSION = "analyst:catalyst-v1"

_DIRECTION_BY_CLASS = {DOWN: "down", RANGE: "range", UP: "up"}

# Analyst mapping knobs (v1 heuristics, tune as catalyst history grows).
ANALYST_SENTIMENT_EPS = 0.15   # |sentiment| below this -> a 'range' (no directional conviction)
ANALYST_MAX_STALE_DAYS = 7     # a catalyst older than this vs the league day is treated as stale -> skip


@dataclass
class EngineCall:
    """A trader's unified-shape opinion for one symbol. Market-side facts
    (entry_price / grade_vol / trade_date / regime) come from the shared
    MarketContext, not from here, so every trader is graded identically."""
    direction: str          # 'up' | 'down' | 'range'
    conviction: float       # 0..1
    rationale: str
    invalidation: str
    method_version: str
    engine_payload: dict
    # Filled by orchestration.py AFTER predict() returns (league/option_bridge.py),
    # never set by an engine itself -- instrument selection is mechanical and
    # must be identical across every trader for the league to stay comparable,
    # not a per-engine judgment call. None for 'range' calls or when no usable
    # entry IV exists that day.
    option_structure: dict | None = None


class TraderEngine(Protocol):
    key: str

    def predict(
        self, conn: duckdb.DuckDBPyConnection, ctx: MarketContext
    ) -> tuple[EngineCall | None, str | None]:
        """(call, None) or (None, skip_reason) -- exactly one is set."""
        ...


class ChartistEngine:
    """Technical trader: module A/B's regime + direction probabilities, already
    computed on the context. Never skips once a context exists (the context
    only exists because production produced a live row)."""

    key = "chartist"

    def predict(
        self, conn: duckdb.DuckDBPyConnection, ctx: MarketContext
    ) -> tuple[EngineCall | None, str | None]:
        proba = np.asarray(ctx.production.proba, dtype=float)
        cls = int(proba.argmax())
        direction = _DIRECTION_BY_CLASS[cls]
        conviction = float(proba[cls])
        fv = ctx.production.feature_values

        rationale = (
            f"Regime {ctx.regime}: module A/B puts P(down/range/up) = "
            f"{proba[DOWN]:.2f}/{proba[RANGE]:.2f}/{proba[UP]:.2f}, favouring '{direction}'. "
            f"Drivers: realized_vol_20d={fv.get('realized_vol_20d', float('nan')):.3f}, "
            f"adx_14={fv.get('adx_14', float('nan')):.1f}, "
            f"xsec_dispersion={fv.get('xsec_dispersion', float('nan')):.3f}."
        )
        invalidation = (
            f"Price exits its {ctx.horizon}-day volatility band, or the market "
            f"re-classifies out of regime {ctx.regime}."
        )
        payload = {
            "proba_down": proba[DOWN], "proba_range": proba[RANGE], "proba_up": proba[UP],
            "regime": ctx.regime, "feature_values": fv,
            "model_version": ctx.production.model_version,
        }
        return EngineCall(direction, conviction, rationale, invalidation,
                          CHARTIST_METHOD_VERSION, payload), None


class AnalystEngine:
    """News-cascade trader: reads the latest catalyst_signals row (the Sonnet
    transmission-chain synthesis written by the OpenClaw cron) for the symbol
    and maps sentiment/novelty/priced_in into a directional bet. Skips a symbol
    with no recent catalyst -- honest: real news history is short, so on most
    days most symbols have nothing, and we don't fabricate a call."""

    key = "analyst"

    def predict(
        self, conn: duckdb.DuckDBPyConnection, ctx: MarketContext
    ) -> tuple[EngineCall | None, str | None]:
        signal = get_latest_catalyst_for_symbol(conn, ctx.symbol)
        if signal is None:
            return None, "no catalyst_signals row for this symbol"
        # Look-ahead guard: only use a catalyst known by the league day, and
        # only if it's still fresh relative to that day.
        if signal.as_of_date > ctx.trade_date:
            return None, "latest catalyst is newer than the league day (look-ahead guard)"
        if (ctx.trade_date - signal.as_of_date).days > ANALYST_MAX_STALE_DAYS:
            return None, f"latest catalyst is stale (>{ANALYST_MAX_STALE_DAYS}d before the league day)"
        if signal.sentiment_score is None:
            return None, "catalyst has no sentiment score to form a direction"

        sentiment = float(signal.sentiment_score)
        novelty = float(signal.novelty_score) if signal.novelty_score is not None else 0.5
        priced_in = float(signal.priced_in_estimate) if signal.priced_in_estimate is not None else 0.5

        if sentiment > ANALYST_SENTIMENT_EPS:
            direction = "up"
        elif sentiment < -ANALYST_SENTIMENT_EPS:
            direction = "down"
        else:
            direction = "range"

        # Conviction = how much fresh, un-priced-in, directionally-strong signal
        # there is. clamp to [0,1]. v1 heuristic.
        conviction = max(0.0, min(1.0, novelty * (1.0 - priced_in) * abs(sentiment)))

        rationale = f"{signal.catalyst_summary} Transmission: {signal.transmission_chain}"
        invalidation = (
            "The catalyst gets fully priced in (sentiment normalizes) or the "
            "transmission thesis reverses on new information."
        )
        payload = {
            "catalyst_summary": signal.catalyst_summary,
            "transmission_chain": signal.transmission_chain,
            "novelty_score": signal.novelty_score,
            "sentiment_score": signal.sentiment_score,
            "priced_in_estimate": signal.priced_in_estimate,
            "source_refs": signal.source_refs,
            "catalyst_as_of": str(signal.as_of_date),
        }
        return EngineCall(direction, conviction, rationale, invalidation,
                          ANALYST_METHOD_VERSION, payload), None


# --- v2 factions (migration 039) --------------------------------------------
# Three more philosophies. Like the v1 pair they are read-only over
# already-computed data and make NO LLM/API calls -- they read features
# as-of the league day from feature_store / alt_social_hourly, look-ahead-
# guarded the same way AnalystEngine guards catalyst_signals (feature_date and
# available_at both on/before the context's anchoring instant). v1 heuristic
# thresholds are module constants below, tunable as history accrues.

REVERSION_METHOD_VERSION = "reversion:rsi-meanrev-v1"
FLOW_METHOD_VERSION = "flow:positioning-v1"
SENTIMENT_METHOD_VERSION = "sentiment:heat-accel-v1"

# Reversion knobs
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0
# Flow knobs
FLOW_PCR_NEUTRAL = 1.0          # put/call above this leans bearish
FLOW_SCORE_EPS = 0.34           # |mean vote| below this -> 'range' (no clear pressure)
# Sentiment knobs
SENTIMENT_WINDOW_DAYS = 3       # trailing social window ending on the league day
SENTIMENT_TONE_LOOKBACK_DAYS = 5   # compare market news tone against this many days back
SENTIMENT_TONE_SCALE = 8.0     # GDELT avgtone swings ~[-8,0]; scale a tone *change* into ~[-1,1]
SENTIMENT_EPS = 0.05           # |net heat| below this -> 'range'
MARKET_KEY = "__MARKET__"      # feature_store key for market-wide (non-per-ticker) features


def _feature_asof(
    conn: duckdb.DuckDBPyConnection, symbol: str, feature_name: str, *, as_of, available_by
) -> float | None:
    """Latest `feature_store` value for (symbol, feature_name) that was known by
    the league day: `feature_date` on/before `as_of` AND `available_at` on/before
    `available_by` (the context's anchoring instant) -- no look-ahead. Returns a
    float, or None when missing/NaN (DuckDB's IS NOT NULL doesn't catch NaN, so
    filter it explicitly)."""
    row = conn.execute(
        """
        SELECT feature_value FROM feature_store
        WHERE symbol = ? AND feature_name = ?
          AND feature_date <= ? AND available_at <= ?
        ORDER BY feature_date DESC, available_at DESC
        LIMIT 1
        """,
        [symbol.upper(), feature_name, as_of, available_by],
    ).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        v = float(row[0])
    except (TypeError, ValueError):
        return None
    return None if np.isnan(v) else v


class ReversionEngine:
    """Mean-reversion trader (反轉派): fades RSI-14 extremes as-of the league
    day. Skips a symbol with no RSI that day -- honest, no fabricated call."""

    key = "reversion"

    def predict(
        self, conn: duckdb.DuckDBPyConnection, ctx: MarketContext
    ) -> tuple[EngineCall | None, str | None]:
        rsi = _feature_asof(conn, ctx.symbol, "rsi_14", as_of=ctx.trade_date, available_by=ctx.available_at)
        if rsi is None:
            return None, "no rsi_14 in feature_store for this symbol as-of the league day"
        if rsi >= RSI_OVERBOUGHT:
            direction, why = "down", "overbought -> fade the rip"
        elif rsi <= RSI_OVERSOLD:
            direction, why = "up", "oversold -> fade the flush"
        else:
            direction, why = "range", "mid-range -> no mean-reversion edge"
        conviction = max(0.0, min(1.0, abs(rsi - 50.0) / 50.0))
        rationale = f"RSI-14={rsi:.1f} as-of {ctx.trade_date}: {why}."
        invalidation = (
            "RSI normalises back toward 50, or price breaks its band in the "
            "extreme's direction (a trend, which mean-reversion should not fight)."
        )
        payload = {"rsi_14": rsi, "overbought": RSI_OVERBOUGHT, "oversold": RSI_OVERSOLD}
        return EngineCall(direction, conviction, rationale, invalidation,
                          REVERSION_METHOD_VERSION, payload), None


class FlowEngine:
    """Positioning/flow trader (資金流派): infers option-hedging pressure from
    dealer gamma (GEX), 25-delta skew change and put/call ratio as-of the league
    day. Each present signal casts a bearish/bullish vote; the mean vote is the
    directional pressure. Skips when none of the inputs exist that day."""

    key = "flow"

    def predict(
        self, conn: duckdb.DuckDBPyConnection, ctx: MarketContext
    ) -> tuple[EngineCall | None, str | None]:
        gex = _feature_asof(conn, ctx.symbol, "gex_estimate", as_of=ctx.trade_date, available_by=ctx.available_at)
        skew_chg = _feature_asof(conn, ctx.symbol, "skew_25delta_chg_1d", as_of=ctx.trade_date, available_by=ctx.available_at)
        pcr = _feature_asof(conn, ctx.symbol, "put_call_ratio", as_of=ctx.trade_date, available_by=ctx.available_at)
        if gex is None and skew_chg is None and pcr is None:
            return None, "no positioning features (gex/skew/put-call) for this symbol as-of the league day"
        votes = []
        if gex is not None:
            votes.append(-1.0 if gex < 0 else 1.0)        # negative dealer gamma amplifies moves -> down bias
        if skew_chg is not None:
            votes.append(-1.0 if skew_chg > 0 else 1.0)   # downside skew steepening = put demand -> down
        if pcr is not None:
            votes.append(-1.0 if pcr > FLOW_PCR_NEUTRAL else 1.0)  # more puts than calls -> down
        score = sum(votes) / len(votes)
        if score <= -FLOW_SCORE_EPS:
            direction = "down"
        elif score >= FLOW_SCORE_EPS:
            direction = "up"
        else:
            direction = "range"
        conviction = min(1.0, abs(score))
        rationale = (
            f"Positioning as-of {ctx.trade_date}: gex={_fmt(gex)}, "
            f"skew_chg_1d={_fmt(skew_chg)}, put/call={_fmt(pcr)} -> "
            f"pressure score {score:+.2f} favouring '{direction}'."
        )
        invalidation = "Dealer gamma flips sign, skew/put-call mean-revert, or a catalyst overrides positioning mechanics."
        payload = {"gex_estimate": gex, "skew_25delta_chg_1d": skew_chg,
                   "put_call_ratio": pcr, "pressure_score": score}
        return EngineCall(direction, conviction, rationale, invalidation,
                          FLOW_METHOD_VERSION, payload), None


class SentimentEngine:
    """Sentiment-heat trader (情緒派): rides the ACCELERATION of attention, not
    its level (distinct from Analyst's transmission reasoning). News tone is
    chronically negative, so its *level* isn't directional -- the signal is
    whether tone is IMPROVING or DETERIORATING. Reads the market-wide GDELT tone
    change (feature_store is per-ticker for price features but market-wide for
    GDELT, keyed __MARKET__) plus per-symbol social acceleration when it exists.
    Skips when neither signal is available as-of the league day."""

    key = "sentiment"

    def predict(
        self, conn: duckdb.DuckDBPyConnection, ctx: MarketContext
    ) -> tuple[EngineCall | None, str | None]:
        # Per-symbol social acceleration over the trailing window ending on the
        # league day (look-ahead: hour on/before trade_date AND ingested by the
        # context instant). Usually absent today -- kept so the faction sharpens
        # per-symbol once social ingestion fills in.
        row = conn.execute(
            """
            SELECT avg(sentiment_accel)
            FROM alt_social_hourly
            WHERE symbol = ?
              AND CAST(hour_bucket AS DATE) <= ?
              AND CAST(hour_bucket AS DATE) >= ?
              AND ingested_at <= ?
            """,
            [ctx.symbol.upper(), ctx.trade_date,
             ctx.trade_date - timedelta(days=SENTIMENT_WINDOW_DAYS), ctx.available_at],
        ).fetchone()
        accel = row[0] if row and row[0] is not None and not np.isnan(row[0]) else None
        # Market-wide news-tone *change* vs a few days back, scaled into ~[-1,1].
        tone_now = _feature_asof(conn, MARKET_KEY, "gdelt_avgtone_1d", as_of=ctx.trade_date, available_by=ctx.available_at)
        tone_prev = _feature_asof(
            conn, MARKET_KEY, "gdelt_avgtone_1d",
            as_of=ctx.trade_date - timedelta(days=SENTIMENT_TONE_LOOKBACK_DAYS), available_by=ctx.available_at,
        )
        tone_heat = (tone_now - tone_prev) / SENTIMENT_TONE_SCALE if (tone_now is not None and tone_prev is not None) else None
        signals = [x for x in (accel, tone_heat) if x is not None]
        if not signals:
            return None, "no social/news sentiment signal (social accel or market tone change) as-of the league day"
        net = sum(signals) / len(signals)
        if net >= SENTIMENT_EPS:
            direction = "up"
        elif net <= -SENTIMENT_EPS:
            direction = "down"
        else:
            direction = "range"
        conviction = max(0.0, min(1.0, abs(net)))
        rationale = (
            f"Sentiment heat as-of {ctx.trade_date}: social accel={_fmt(accel)}, "
            f"market tone change={_fmt(tone_heat)} (now {_fmt(tone_now)} vs "
            f"{_fmt(tone_prev)}) -> net {net:+.2f} favouring '{direction}'."
        )
        invalidation = "The heat decelerates (attention fades) or reverses sign on fresh news."
        payload = {"social_sentiment_accel": accel, "market_tone_now": tone_now,
                   "market_tone_prev": tone_prev, "tone_heat": tone_heat, "net_heat": net}
        return EngineCall(direction, conviction, rationale, invalidation,
                          SENTIMENT_METHOD_VERSION, payload), None


def _fmt(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.3f}"


ENGINE_REGISTRY: dict[str, TraderEngine] = {
    "chartist": ChartistEngine(),
    "analyst": AnalystEngine(),
    "reversion": ReversionEngine(),
    "flow": FlowEngine(),
    "sentiment": SentimentEngine(),
}
