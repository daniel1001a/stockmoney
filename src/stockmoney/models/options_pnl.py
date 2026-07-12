"""Option-level P&L engine (Part 1A of ~/.claude/plans/buzzing-yawning-squid.md).

Module A predicts *underlying* direction; the thing actually traded is an
*option*, whose P&L is nonlinear — dominated by time decay (theta) and IV
changes (vega), not just the underlying move. A signal that is mildly right on
direction can still lose money on the option (theta bleed / IV crush) and vice
versa, so a Sharpe measured on the underlying (see metrics.py's toy long/flat/
short strategy) says almost nothing about whether the option trade makes money.
This module reprices a hypothetical option forward via Black-Scholes so the
options-P&L backtest (backtest_options_pnl.py) can measure the real thing.

Return convention: `option_return` returns a *fractional return on premium*
(+0.8 == +80% of the premium put up). That is deliberately the same shape
ev_gate/kelly already consume as `pnl_gross`, so the option's own asymmetric
win/loss distribution — a long's loss is floored near -1 (you can only lose the
premium), a short's loss is not — flows straight into the EV gate and Kelly
sizing (Part 1C) unchanged.

Look-ahead discipline (this module is pure math with no clock of its own; the
caller owns point-in-time correctness, but the contract it must honour is
documented here):
- `OptionEntry.iv` must be the IV *available at entry* (iv_surface_daily as of
  the entry date, available_at <= entry). Never a future IV.
- `OptionExit.iv` is the IV assumption at exit. Passing `None` holds IV constant
  at the entry level — the honest default that isolates the directional + theta
  effect and needs no future data. Passing a realized exit IV (a vega scenario)
  requires that IV to have been observed at the exit date; the backtest reports
  both and never feeds a future IV in as if known at entry.

Pricing is European Black-Scholes (`options_math.bs_price`), a v1 approximation
on the same footing as the rest of the self-estimated options layer (CLAUDE.md
section 2): it ignores the small early-exercise premium of US equity/ETF
options and assumes a flat IV per option rather than a full surface.
"""
from __future__ import annotations

from dataclasses import dataclass

from stockmoney.data.options_math import bs_price, is_valid_option

DEFAULT_RATE = 0.045          # short rate; caller can pass a FRED DGS value instead
DEFAULT_SPREAD_PCT = 0.05     # v1 bid-ask spread as a fraction of mid premium (CLAUDE.md section 16: to calibrate)
DAYS_PER_YEAR = 365.0


@dataclass(frozen=True)
class OptionEntry:
    spot: float
    strike: float
    is_call: bool
    iv: float                 # IV available at entry (never a future value)
    t_years: float            # time to expiry at entry, in years
    side: str = "long"        # 'long' (buyer) | 'short' (seller)

    def __post_init__(self) -> None:
        if self.side not in ("long", "short"):
            raise ValueError(f"side must be 'long' or 'short', got {self.side!r}")


@dataclass(frozen=True)
class OptionExit:
    spot: float
    iv: float | None          # None -> hold IV constant at entry (isolates direction+theta)
    days_held: int


def _mid_premium(spot: float, strike: float, is_call: bool, iv: float, t_years: float, r: float) -> float:
    """Mid Black-Scholes premium per share, floored at intrinsic once expired
    (t<=0) or on any junk input so a degenerate row can't produce a negative or
    NaN premium."""
    if t_years <= 0 or not is_valid_option(spot, strike, t_years, iv):
        return max(spot - strike, 0.0) if is_call else max(strike - spot, 0.0)
    return bs_price(spot, strike, t_years, iv, r, is_call=is_call)


def entry_premium(entry: OptionEntry, *, r: float = DEFAULT_RATE) -> float:
    """Mid premium paid/collected at entry (per share), before spread."""
    return _mid_premium(entry.spot, entry.strike, entry.is_call, entry.iv, entry.t_years, r)


def exit_premium(entry: OptionEntry, exit: OptionExit, *, r: float = DEFAULT_RATE) -> float:
    """Mid premium at exit (per share), before spread. Reprices the *same*
    contract (strike/right unchanged) at the exit spot, the exit IV (or the
    entry IV if `exit.iv is None`), and the reduced time to expiry."""
    t_exit = entry.t_years - exit.days_held / DAYS_PER_YEAR
    iv = entry.iv if exit.iv is None else exit.iv
    return _mid_premium(exit.spot, entry.strike, entry.is_call, iv, t_exit, r)


def option_return(
    entry: OptionEntry,
    exit: OptionExit,
    *,
    r: float = DEFAULT_RATE,
    spread_pct: float = DEFAULT_SPREAD_PCT,
) -> float:
    """Net fractional return on premium for holding `entry` to `exit`.

    Bid-ask cost: the mid premium is crossed by half the spread on each leg — a
    buyer pays mid*(1+h) and later sells at mid*(1-h); a seller is the mirror.
    This round-trip cost on both legs is exactly where "a signal with positive
    underlying Sharpe still loses money as options" tends to show up, which is
    the whole reason this backtest exists.
    """
    p_in = entry_premium(entry, r=r)
    if p_in <= 0:
        return 0.0  # no premium to put up (junk/degenerate row) -> no trade, no P&L
    p_out = exit_premium(entry, exit, r=r)
    half = spread_pct / 2.0

    if entry.side == "long":
        cost_in = p_in * (1.0 + half)          # buy at ask
        proceeds_out = p_out * (1.0 - half)    # sell at bid
        return (proceeds_out - cost_in) / cost_in
    else:  # short: collect premium at bid, buy back at ask
        proceeds_in = p_in * (1.0 - half)      # sell at bid
        cost_out = p_out * (1.0 + half)        # buy back at ask
        return (proceeds_in - cost_out) / proceeds_in
