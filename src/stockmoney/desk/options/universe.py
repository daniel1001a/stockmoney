"""Daily underlying universe (OPTIONS_PLAN.md section 4).

Order of priority, capped at ``daily_universe_cap`` (25):
  1. underlyings named in today's Grok plan (its explicit picks);
  2. the starting list (index ETFs, mega caps, high-vol names);
  3. scan additions: ``scan_candidates`` whose premarket gap and premarket
     dollar volume clear the thresholds.
Every name that does not make it is logged with the reason
(options_filtered, stage 'universe'). Liquidity is then filtered per
trigger, where it can actually be measured: the underlying's bid/ask spread
(engine) and each option contract's spread / mid / OI / volume (select.py),
each failure logged too. A name whose options never pass simply never
trades, which is the "auto-removed" rule of section 4.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from stockmoney.desk.options.config import OptionsConfig


@dataclass(frozen=True)
class Member:
    symbol: str
    uclass: str
    origin: str  # plan | start | scan


def scan_hit(pm_bars: pd.DataFrame, prev_close: float | None, cfg: OptionsConfig) -> tuple[bool, str, dict]:
    """Premarket gap / dollar-volume test for one scan candidate."""
    if pm_bars is None or pm_bars.empty or not prev_close:
        return False, "no_premarket_data", {}
    gap = float(pm_bars["close"].iloc[-1]) / prev_close - 1
    dv = float((pm_bars["close"] * pm_bars["volume"]).sum())
    detail = {"gap": round(gap, 4), "premarket_dollar_vol": round(dv)}
    if abs(gap) < cfg.scan_min_gap_pct:
        return False, "scan_gap_small", detail
    if dv < cfg.scan_min_premarket_dollar_vol:
        return False, "scan_volume_low", detail
    return True, "", detail


def build(cfg: OptionsConfig, plan_symbols: list[str], scan_hits: list[str]) -> tuple[list[Member], list[tuple[str, str]]]:
    """(members, dropped[(symbol, reason)])."""
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()
    for origin, syms in (("plan", plan_symbols), ("start", cfg.starting_universe()), ("scan", scan_hits)):
        for s in syms:
            if s not in seen:
                seen.add(s)
                ordered.append((s, origin))
    members = [Member(s, cfg.underlying_class(s), o) for s, o in ordered[:cfg.daily_universe_cap]]
    dropped = [(s, "daily_cap") for s, _ in ordered[cfg.daily_universe_cap:]]
    return members, dropped
