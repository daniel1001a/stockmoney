"""EngineCall -> concrete option instrument (IMPROVEMENT_PLAN.md §S3), applied
UNIFORMLY to every trader by orchestration.py after an engine emits its call --
not inside each engine. Engines decide market view (direction/conviction/
rationale); instrument selection is mechanical and must be identical across
traders for the league to stay a fair comparison, exactly the same
"discretion vs mechanics" split option_selection.py itself documents.

Reuses option_selection.select_option verbatim (the same up->call/down->put/
range->no-trade bridge the module-A backtest already uses) rather than
re-deriving strike selection here -- this module's only job is sourcing the
entry IV (data.options_iv, real-snapshot-or-proxy) and shaping the result into
the JSON-serializable dict trader_predictions.option_structure stores.

**Directional-long only, and why**: §S3 also mentions mapping a seller
strategy to a credit spread, but neither of the two v1 traders (Chartist,
Analyst) ever emits a "sell premium" view -- both only ever say up/down/range.
A credit-spread structure needs an engine that actually decides to sell (e.g.
a future VRP-driven trader, models/vrp_gate.py's short_vol bias, which is not
wired into any trader yet -- that's explicitly out of Wave D's scope). Adding
a seller mapping today with no caller that ever produces "sell" would be
unverifiable dead code, so this bridge stays long-only until a seller trader
exists.
"""
from __future__ import annotations

import duckdb

from stockmoney.data.options_iv import entry_iv_for_symbol
from stockmoney.league.context import MarketContext
from stockmoney.league.engines import EngineCall
from stockmoney.models.feature_matrix import DOWN, RANGE, UP
from stockmoney.models.option_selection import SelectionParams, select_option

_DIRECTION_TO_CLASS = {"down": DOWN, "range": RANGE, "up": UP}


def build_option_structure(
    conn: duckdb.DuckDBPyConnection, ctx: MarketContext, call: EngineCall,
    *, params: SelectionParams = SelectionParams(),
) -> dict | None:
    """None for a 'range' call (no directional edge -> no trade, same
    option_selection convention) or when no usable entry_iv exists at all
    (e.g. grade_vol itself is missing/non-positive -- can't even proxy)."""
    direction_class = _DIRECTION_TO_CLASS[call.direction]
    if direction_class == RANGE:
        return None
    if ctx.grade_vol is None or ctx.grade_vol <= 0:
        return None

    entry_iv, iv_source = entry_iv_for_symbol(
        conn, ctx.symbol, ctx.trade_date, realized_vol_20d=ctx.grade_vol,
    )
    entry = select_option(direction_class, ctx.entry_price, entry_iv, params=params)
    if entry is None:
        return None

    return {
        "spot": entry.spot,
        "strike": entry.strike,
        "is_call": entry.is_call,
        "iv": entry.iv,
        "iv_source": iv_source,       # 'real' (iv_surface_daily) | 'proxy' (realized_vol_20d * ratio)
        "t_years": entry.t_years,
        "dte_days": params.dte_days,
        "side": entry.side,
    }
