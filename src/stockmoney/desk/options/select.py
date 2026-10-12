"""Contract selection (OPTIONS_PLAN.md section 5).

For one trigger the shadow book records every variant (DTE bucket x delta
band); live trading uses exactly one. Within a variant: the nearest expiry in
the bucket, the right matching the direction (long = call, short = put),
contracts that pass the liquidity filters, then the |delta| closest to the
band center (ties: tighter spread). Entry is a buy at the ask side.

Delta source, in order (recorded with every choice):
  1. ``provider``  the broker's greek;
  2. ``bs_iv``     Black-Scholes delta from the provider's IV;
  3. ``bs_mid``    Black-Scholes delta from the IV implied by the quote mid
                   (rate and dividends taken as 0: for <= 3 day options the
                   error is far below the width of a delta band).
A contract none of these can price is skipped, never guessed.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from stockmoney.desk.calendar import is_trading_day, next_trading_day
from stockmoney.desk.options._iface import OptionContract
from stockmoney.desk.options.config import DeltaBand, DteBucket, OptionsConfig

ET = ZoneInfo("America/New_York")
YEAR_S = 365.0 * 24 * 3600


# ------------------------------------------------------------------ Black-Scholes (r = q = 0)

def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def years_to_expiry(expiry: date, now: datetime) -> float:
    close = datetime.combine(expiry, time(16, 0), ET)
    return max((close - now).total_seconds(), 60.0) / YEAR_S


def bs_price(spot: float, strike: float, t: float, iv: float, right: str) -> float:
    if iv <= 0 or t <= 0:
        return max(spot - strike, 0.0) if right == "C" else max(strike - spot, 0.0)
    sd = iv * math.sqrt(t)
    d1 = (math.log(spot / strike) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    if right == "C":
        return spot * _ncdf(d1) - strike * _ncdf(d2)
    return strike * _ncdf(-d2) - spot * _ncdf(-d1)


def bs_delta(spot: float, strike: float, t: float, iv: float, right: str) -> float:
    """Signed delta (calls 0..1, puts -1..0)."""
    sd = iv * math.sqrt(t)
    d1 = (math.log(spot / strike) + 0.5 * sd * sd) / sd
    return _ncdf(d1) if right == "C" else _ncdf(d1) - 1


def implied_vol(price: float, spot: float, strike: float, t: float, right: str) -> float | None:
    """Bisection on [0.01, 5]. None when the price is outside the model's
    range (below intrinsic or above the 500% vol price)."""
    lo, hi = 0.01, 5.0
    if not (bs_price(spot, strike, t, lo, right) <= price <= bs_price(spot, strike, t, hi, right)):
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if bs_price(spot, strike, t, mid, right) < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def mid(c: OptionContract) -> float | None:
    if c.bid is None or c.ask is None or c.ask <= 0 or c.bid < 0 or c.ask < c.bid:
        return None
    return (c.bid + c.ask) / 2


def contract_delta(c: OptionContract, spot: float, now: datetime) -> tuple[float | None, str]:
    """(|delta|, source). See the module docstring for the order."""
    if c.delta is not None and math.isfinite(c.delta) and c.delta != 0:
        return abs(c.delta), "provider"
    t = years_to_expiry(c.expiry, now)
    if c.iv is not None and math.isfinite(c.iv) and c.iv > 0:
        return abs(bs_delta(spot, c.strike, t, c.iv, c.right)), "bs_iv"
    m = mid(c)
    if m and spot > 0:
        iv = implied_vol(m, spot, c.strike, t, c.right)
        if iv is not None:
            return abs(bs_delta(spot, c.strike, t, iv, c.right)), "bs_mid"
    return None, "none"


# ------------------------------------------------------------------ expiry buckets

def trading_day_offset(day: date, n: int) -> date:
    d = day if is_trading_day(day) else next_trading_day(day)
    for _ in range(n):
        d = next_trading_day(d)
    return d


def bucket_range(day: date, bucket: DteBucket) -> tuple[date, date]:
    return trading_day_offset(day, bucket.min_days), trading_day_offset(day, bucket.max_days)


# ------------------------------------------------------------------ liquidity + choice

def liquidity_reason(c: OptionContract, now: datetime, cfg: OptionsConfig) -> str | None:
    """None when the contract passes, else the first failing rule."""
    m = mid(c)
    if m is None or not c.bid:
        return "no_two_sided_quote"
    if c.as_of is None or (now - c.as_of).total_seconds() > cfg.max_quote_age_s:
        return "stale_quote"
    if m < cfg.min_mid:
        return "mid_below_min"
    if (c.ask - c.bid) / m > cfg.max_spread_pct:
        return "spread_too_wide"
    if c.open_interest is not None and c.open_interest < cfg.min_open_interest:
        return "open_interest_low"
    if c.volume is not None and c.volume < cfg.min_volume:
        return "volume_low"
    return None


@dataclass(frozen=True)
class Choice:
    contract: OptionContract
    delta: float
    delta_source: str
    dte_label: str
    band_label: str
    spread_pct: float

    def quote_dict(self) -> dict:
        c = self.contract
        return {"occ": c.occ, "bid": c.bid, "ask": c.ask, "last": c.last, "volume": c.volume,
                "open_interest": c.open_interest, "delta": c.delta, "iv": c.iv,
                "as_of": c.as_of.isoformat() if c.as_of else None, "delta_used": round(self.delta, 4),
                "delta_source": self.delta_source, "spread_pct": round(self.spread_pct, 4)}


def choose(chain: list[OptionContract], spot: float, now: datetime, direction: str, bucket: DteBucket,
           band: DeltaBand, cfg: OptionsConfig) -> tuple[Choice | None, str, dict]:
    """(choice, reason, detail). ``reason`` is "" on success, else why this
    variant has no contract (logged as a filtered-out opportunity)."""
    right = "C" if direction == "long" else "P"
    day = now.astimezone(ET).date()
    lo, hi = bucket_range(day, bucket)
    cands = [c for c in chain if c.right == right and lo <= c.expiry <= hi]
    if not cands:
        return None, "no_expiry_in_bucket", {"from": str(lo), "to": str(hi)}
    expiry = min(c.expiry for c in cands)
    cands = [c for c in cands if c.expiry == expiry]
    fails: Counter = Counter()
    best: tuple | None = None
    for c in cands:
        why = liquidity_reason(c, now, cfg)
        if why:
            fails[why] += 1
            continue
        d, src = contract_delta(c, spot, now)
        if d is None:
            fails["no_delta"] += 1
            continue
        if not (band.lo <= d <= band.hi):
            fails["delta_out_of_band"] += 1
            continue
        m = mid(c)
        sp = (c.ask - c.bid) / m
        key = (abs(d - band.center), sp)
        if best is None or key < best[0]:
            best = (key, Choice(c, d, src, bucket.label, band.label, sp))
    if best is None:
        detail = {"expiry": str(expiry), "contracts": len(cands), **dict(fails)}
        liquid_fail = sum(v for k, v in fails.items() if k not in ("delta_out_of_band", "no_delta"))
        reason = "no_liquid_contract" if liquid_fail == len(cands) else "no_contract_in_band"
        return None, reason, detail
    return best[1], "", {}
