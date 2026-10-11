"""Random-entry control strategy (also used by research/desk_audit)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from stockmoney.desk.strategies import Context, Signal, Strategy
from stockmoney.desk.universe import STOCKS


class RandomEntry(Strategy):
    """Each day pick Poisson(lam) random eligible stocks. Same stop distance,
    trailing stop, 50-day-line exit and hold cap as breakout, same regime gate
    (no entries in risk_off). Isolates 'which stock, which day' from everything
    else, so the real book is compared against luck under identical rules."""

    name = "random"
    params = {"stop_atr": 2.5, "trail_atr": 3.0, "max_hold_days": 60}

    def __init__(self, seed: int, lam: float = 0.8, trend_filter: bool = True):
        super().__init__()
        self.rng = np.random.default_rng(seed)
        self.lam, self.trend_filter = lam, trend_filter

    def _candidates(self, ctx: Context) -> list[Signal]:
        if ctx.market.regime == "risk_off":
            return []
        elig = []
        for sym in STOCKS:
            if sym not in ctx.ind.close or pd.isna(ctx.v("sma200", sym)) or pd.isna(ctx.v("atr20", sym)):
                continue
            if self.trend_filter and not (ctx.v("close", sym) > ctx.v("sma50", sym) > ctx.v("sma200", sym)):
                continue
            elig.append(sym)
        k = min(len(elig), self.rng.poisson(self.lam))
        out = []
        for sym in (self.rng.choice(elig, size=k, replace=False) if k else []):
            c, a = ctx.v("close", sym), ctx.v("atr20", sym)
            out.append(Signal(self.name, str(sym), ctx.t, c, c - self.params["stop_atr"] * a,
                              float(self.rng.random()), "random", "random"))
        return out
