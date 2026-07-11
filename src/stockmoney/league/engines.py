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


ENGINE_REGISTRY: dict[str, TraderEngine] = {
    "chartist": ChartistEngine(),
    "analyst": AnalystEngine(),
}
