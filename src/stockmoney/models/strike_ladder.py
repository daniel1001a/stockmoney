"""Snap a continuous strike (Black-Scholes-inverted, from
option_selection.strike_for_delta) to the nearest strike a real broker would
actually list.

Why this exists: option_selection.strike_for_delta() inverts Black-Scholes
delta to get the strike that would carry a target delta *exactly* -- a
mathematically clean number like 996.7139... for NFLX or 234.62... for TSLA.
No listed option chain has a strike like that; real US equity/ETF option
chains trade on a ladder that widens with the underlying's price, and a
strike off that ladder is unrealizable -- it could never actually be bought.
Displaying/pricing one is the "impossible strike" bug (arena Live Board
showing things like "NFLX 996.7 Call").

Ladder chosen (v1, a defensible approximation of CBOE/broker listing
conventions -- not any single broker's exact grid, which varies
security-by-security and isn't public as a clean rule):

    strike <  $25         -> $0.50 increments
    $25  <= strike < $100 -> $1.00 increments
    $100 <= strike < $250 -> $2.50 increments
    $250 <= strike < $500 -> $5.00 increments
    strike >= $500        -> $10.00 increments

This mirrors real-world practice: sub-$25 names (SOXS, low-priced ETFs) list
tight $0.50 strikes; the $25-$100 band (most mid-caps) uses $1; $100-$250
(AAPL/AMZN/JPM territory) commonly uses $2.50; $250-$500 (MSFT/META) uses $5;
and very high-priced names (NFLX, GS, high-priced growth names) use $10. This
is CLAUDE.md section 16 territory (a v1 starting point, not gospel) -- if a
future paid feed (ORATS/CBOE DataShop) supplies the real per-symbol strike
grid, prefer that over this heuristic.
"""
from __future__ import annotations

# (upper_bound_exclusive, increment) pairs, checked in order. The last entry's
# upper bound is unused (anything >= the previous bound falls through to it).
_LADDER: list[tuple[float, float]] = [
    (25.0, 0.5),
    (100.0, 1.0),
    (250.0, 2.5),
    (500.0, 5.0),
]
_TOP_INCREMENT = 10.0


def strike_increment(strike: float) -> float:
    """The listed-strike increment for `strike`'s price tier."""
    if strike <= 0 or strike != strike:  # reject <=0 and NaN (x!=x is the NaN test)
        raise ValueError(f"strike must be a positive finite number, got {strike!r}")
    for upper, increment in _LADDER:
        if strike < upper:
            return increment
    return _TOP_INCREMENT


def snap_strike(strike: float) -> float:
    """Round `strike` to the nearest realistic listed increment for its price
    tier. Idempotent: snap_strike(snap_strike(x)) == snap_strike(x)."""
    increment = strike_increment(strike)
    snapped = round(strike / increment) * increment
    # Guard against float repr noise (e.g. 104.99999999999997) so downstream
    # equality/display never shows a fractional-garbage-looking value.
    return round(snapped, 2)


def is_on_ladder(strike: float, *, tol: float = 1e-6) -> bool:
    """True if `strike` already sits on a valid listed increment for its
    price tier (within float tolerance) -- the check validation.py reuses."""
    if strike <= 0 or strike != strike:
        return False
    return abs(strike - snap_strike(strike)) <= tol
