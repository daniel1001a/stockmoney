"""Shadow fill model (OPTIONS_PLAN.md section 5): buys fill at the ASK, sells
at the BID, never at the mid, plus an optional extra slippage fraction and a
per-contract fee. Every setup runs through this whether or not a real order
is sent, so shadow and live results can be compared row by row.
"""
from __future__ import annotations

from math import floor

from stockmoney.desk.options.config import OptionsConfig


def buy_price(ask: float, cfg: OptionsConfig) -> float:
    return ask * (1 + cfg.shadow_slippage_pct)


def sell_price(bid: float, cfg: OptionsConfig) -> float:
    return max(bid * (1 - cfg.shadow_slippage_pct), 0.0)


def contracts_for(price: float, cfg: OptionsConfig) -> int:
    """Whole contracts within the per-trade premium cap (0 = cannot afford
    one). Shadow and live use the same sizing so their dollars compare."""
    if price <= 0:
        return 0
    return floor(cfg.max_premium_per_trade / (price * cfg.contract_multiplier) + 1e-9)


def half_spread_cost(bid: float | None, ask: float | None, qty: float, cfg: OptionsConfig) -> float:
    """Dollar cost versus the mid for crossing half the spread on ``qty``."""
    if bid is None or ask is None:
        return 0.0
    return max(ask - bid, 0.0) / 2 * qty * cfg.contract_multiplier


def round_tick(price: float, tick: float, up: bool) -> float:
    """Round a limit to the tick (up for buys, down for sells), so a buy limit
    at the ask is never accidentally below it."""
    n = round(price / tick, 6)  # 0.64 / 0.01 is 64.00000000000001 in floating point
    n = -(-n // 1) if up else n // 1
    return round(n * tick, 4)


PENNY_ALL_PRICES = ("SPY", "QQQ", "IWM")


def option_tick(underlying: str, price: float) -> float:
    """Minimum price increment: $0.01 under $3, $0.05 from $3 up, except the
    names quoted in pennies at every price. Exchange rules, not a tunable."""
    return 0.01 if price < 3 or underlying in PENNY_ALL_PRICES else 0.05
