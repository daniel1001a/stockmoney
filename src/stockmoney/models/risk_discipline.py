"""Risk-control discipline: the system's *default* rules for how much to risk
and when to STOP (CLAUDE.md sections 9, 10, 16). This is the risk-management
track the whole project exists to serve -- it is NOT personalized investment
advice and NOT a directive to trade any particular instrument; it is a set of
configurable guardrails, every threshold documented and overridable.

Why these defaults (grounded, not plucked):

* Account 10k-50k, long short-dated options (CLAUDE.md section 0). A long option
  can lose ~100% of its premium, so "risk per trade" is premium-at-risk, and a
  handful of full-premium losses in a row can wreck a small account -- the
  sizing here is deliberately tighter than the Arena contest's 20%/position.

* The live signal is WEAK (build_live.py: OOS directional accuracy ~0.35-0.48,
  barely above the ~1/3 three-class baseline). Kelly sizing is acutely sensitive
  to estimation error in a thin edge; over-betting a noisy edge is the classic
  ruin path. So the fractional-Kelly coefficient defaults to the LOW end of
  CLAUDE.md section 9's 0.25-0.50 band (0.25), not the high end kelly.py uses for
  its contest simulation.

* Circuit breakers exist to stop behavioural failure modes (revenge-trading a
  red day, forcing trades in a regime the model reads badly), which is where
  small accounts actually die -- not from any single position.

Every function here is pure and unit-testable; nothing places or blocks a real
order (hard boundary, CLAUDE.md section 0) -- callers surface these as prompts.
"""
from __future__ import annotations

from dataclasses import dataclass

from stockmoney.models.kelly import kelly_fraction

# --- default guardrails (all overridable) -----------------------------------

# Fractional-Kelly coefficient: low end of CLAUDE.md section 9's band, because
# the live edge is thin and Kelly punishes over-betting a mis-estimated edge.
DEFAULT_KELLY_COEFF = 0.25
# Hard cap on premium-at-risk per single option position, as a fraction of
# account equity. Tighter than the Arena's 20% because a long option's loss is
# floored at -100% of premium, not a fraction of notional.
DEFAULT_MAX_POSITION_FRAC = 0.06
# Stop OPENING new positions once the day's realized+marked loss reaches this
# fraction of start-of-day equity (CLAUDE.md section 16's undecided daily-loss
# breaker -- proposed default, calibrate to the owner's tolerance).
DEFAULT_DAILY_LOSS_LIMIT = 0.04
# After this many consecutive losing trades, pause and re-evaluate the thesis /
# regime before the next entry (the model may be in a regime it reads poorly).
DEFAULT_LOSS_STREAK_COOLDOWN = 3


@dataclass(frozen=True)
class RiskLimits:
    kelly_coeff: float = DEFAULT_KELLY_COEFF
    max_position_frac: float = DEFAULT_MAX_POSITION_FRAC
    daily_loss_limit: float = DEFAULT_DAILY_LOSS_LIMIT
    loss_streak_cooldown: int = DEFAULT_LOSS_STREAK_COOLDOWN


DEFAULT_LIMITS = RiskLimits()


def recommended_position_fraction(
    p_win: float,
    avg_win: float,
    avg_loss: float,
    *,
    limits: RiskLimits = DEFAULT_LIMITS,
    confidence_multiplier: float = 1.0,
) -> float:
    """Fraction of account equity to put at risk on one trade: fractional Kelly
    from the trade's own win/loss profile, scaled by the discretion-layer
    confidence multiplier (section 7), then HARD-CAPPED at
    ``limits.max_position_frac``. Returns 0.0 when there's no positive edge --
    a thin/negative edge means "don't trade", never "size down and hope"."""
    kelly = kelly_fraction(p_win, avg_win, avg_loss)
    sized = kelly * limits.kelly_coeff * max(confidence_multiplier, 0.0)
    return min(sized, limits.max_position_frac)


def daily_loss_breached(
    realized_day_pnl: float, start_of_day_equity: float, *, limits: RiskLimits = DEFAULT_LIMITS
) -> bool:
    """True once the day's loss reaches the circuit-breaker threshold -> the UI
    should prompt "stop opening new positions today". Never blocks an order
    itself (hard boundary); a gain (positive pnl) never trips it."""
    if start_of_day_equity <= 0:
        return False
    return realized_day_pnl <= -abs(limits.daily_loss_limit) * start_of_day_equity


def loss_streak_cooldown(
    recent_outcomes: list[str], *, limits: RiskLimits = DEFAULT_LIMITS
) -> bool:
    """True when the most recent trades are an unbroken run of >= cooldown
    losses (outcomes newest-last or newest-first both work, since we only read
    the trailing run). Prompts a pause-and-re-evaluate, not an auto-halt."""
    streak = 0
    for outcome in reversed(recent_outcomes):
        if outcome == "loss":
            streak += 1
        else:
            break
    return streak >= limits.loss_streak_cooldown
