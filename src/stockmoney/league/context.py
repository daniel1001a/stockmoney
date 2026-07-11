"""Shared per-symbol market context for a league day.

The whole point of the league is that traders are *comparable*. That only
works if, for a given (symbol, day), every trader is anchored to the SAME
window and graded on the SAME ruler: same trade_date, same entry_price, same
grade_vol (realized_vol_20d), same label_end_date, same regime. This module
builds that context once per symbol so no engine can drift off its own private
anchor.

It reuses `production.predict_latest` (the single source of "what does the
model see as of today") for the anchoring facts -- trade_date, regime, and the
realized_vol_20d used for the grading band -- plus `latest_underlying_price`
for entry_price. If production can't produce a live row for the symbol (no
unresolved feature row yet, or an unsupported sector), there is no consistent
league window to anchor to, so the symbol is skipped for every trader that day
(honest: we don't invent a window).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import duckdb

from stockmoney.data.daily_predictions import _add_trading_days
from stockmoney.data.positions import latest_underlying_price
from stockmoney.models import production
from stockmoney.models.production import ProductionPrediction

DEFAULT_BAND_K = 0.5


@dataclass
class MarketContext:
    symbol: str
    sector: str
    trade_date: date
    horizon: int
    label_end_date: date
    entry_price: float
    grade_vol: float          # realized_vol_20d -> the market fact every trader grades on
    band_k: float
    regime: int               # market regime as-of, shared by all traders for this symbol/day
    available_at: datetime
    production: ProductionPrediction  # kept so the Chartist engine reads its proba without re-fitting


def build_context(
    conn: duckdb.DuckDBPyConnection,
    *,
    symbol: str,
    sector: str,
    horizon: int = production.DEFAULT_HORIZON,
    band_k: float = DEFAULT_BAND_K,
) -> tuple[MarketContext | None, str | None]:
    """Anchor the league window for one symbol. Returns (context, None) or
    (None, skip_reason) -- exactly one is set. A skip is an expected outcome
    (unsupported sector / feature_store not refreshed today), not an error."""
    pred = production.predict_latest(conn, target_symbol=symbol, sector=sector, horizon=horizon)
    if pred is None:
        return None, "no unresolved feature row, or missing sector feature/insufficient history"

    latest = latest_underlying_price(conn, symbol)
    if latest is None:
        return None, "no ohlcv_daily price available"

    grade_vol = pred.feature_values.get("realized_vol_20d")
    if grade_vol is None:
        return None, "no realized_vol_20d to anchor the grading band"

    ctx = MarketContext(
        symbol=symbol.upper(),
        sector=sector,
        trade_date=pred.as_of_date,
        horizon=horizon,
        label_end_date=_add_trading_days(pred.as_of_date, horizon),
        entry_price=latest[1],
        grade_vol=float(grade_vol),
        band_k=band_k,
        regime=pred.regime,
        available_at=datetime.now(tz=None).astimezone(),
        production=pred,
    )
    return ctx, None
