"""Fractional Kelly position sizing (CLAUDE.md section 9):

    Kelly% = (P(win) x avg_win/avg_loss - P(loss)) / (avg_win/avg_loss)
    position = Kelly% x conservative_coeff x confidence_multiplier

`conservative_coeff` (v1 default 0.5, the upper/half-Kelly end of CLAUDE.md's
0.25-0.5 range) and `confidence_multiplier` (v1 default 1.0) are both listed in
section 16 as parameters to calibrate later, not fixed values — kept as
explicit keyword arguments so a later discretion-layer confidence input
(section 7) can be threaded in without touching this function's math.
"""
from __future__ import annotations

DEFAULT_CONSERVATIVE_COEFF = 0.5


def kelly_fraction(p_win: float, avg_win: float, avg_loss: float) -> float:
    """Full Kelly fraction, clipped to 0 when there's no positive edge (never
    suggests a reverse/short-the-edge position)."""
    if avg_loss <= 0 or avg_win <= 0:
        return 0.0
    b = avg_win / avg_loss
    p_loss = 1.0 - p_win
    kelly = (p_win * b - p_loss) / b
    return max(kelly, 0.0)


def position_size(
    p_win: float,
    avg_win: float,
    avg_loss: float,
    *,
    conservative_coeff: float = DEFAULT_CONSERVATIVE_COEFF,
    confidence_multiplier: float = 1.0,
) -> float:
    kelly = kelly_fraction(p_win, avg_win, avg_loss)
    return kelly * conservative_coeff * confidence_multiplier
