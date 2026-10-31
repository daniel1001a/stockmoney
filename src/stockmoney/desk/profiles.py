"""Risk profiles for the Alpaca book.

``standard`` is the backtested, audited sizing. ``paper_aggressive`` is a
paper-only experiment arm (OPERATING_PLAN section 11): bigger bets and more
slots so the paper month produces enough trades to learn from. The internal
simulated accounts keep the standard sizing, so they are the control group.

Select with ``DESK_PROFILE``. Aggressive is refused unless the Alpaca env is
paper; the live-trading guard in execution/alpaca.py is untouched.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from stockmoney.desk.execution.risk import Limits


@dataclass(frozen=True)
class Profile:
    name: str
    risk_per_trade: float
    max_positions: int
    max_per_sector: int
    max_position_pct: float
    max_order_notional_pct: float
    max_crypto_exposure_pct: float

    def limits(self) -> Limits:
        return Limits(
            max_order_notional_pct=self.max_order_notional_pct,
            max_position_pct=self.max_position_pct,
            max_crypto_exposure_pct=self.max_crypto_exposure_pct,
        )


PROFILES: dict[str, Profile] = {
    "standard": Profile("standard", 0.0075, 8, 2, 0.20, 0.25, 0.30),
    "paper_aggressive": Profile("paper_aggressive", 0.015, 12, 3, 0.25, 0.30, 0.40),
    "paper_wide": Profile("paper_wide", 0.009, 20, 4, 0.25, 0.30, 0.40),
}


def active(env: dict | None = None) -> Profile:
    env = os.environ if env is None else env
    name = (env.get("DESK_PROFILE") or "standard").strip().lower()
    if name not in PROFILES:
        raise ValueError(f"DESK_PROFILE must be one of {sorted(PROFILES)}, got {name!r}")
    if name != "standard" and (env.get("ALPACA_ENV") or "paper").strip().lower() != "paper":
        raise ValueError(f"DESK_PROFILE={name} is paper-only; refusing with ALPACA_ENV != paper")
    return PROFILES[name]
