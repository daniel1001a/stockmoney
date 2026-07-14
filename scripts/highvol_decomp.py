"""Decompose the high-vol trend/breakout option edge into long-bias vs timing.

Question (foreman follow-up to LINVERSE_EXPERIMENT_REPORT.md): the +7-8% mean
option return of highvol_trend / highvol_breakout "beats long_stock" -- but is
that real UP/DOWN timing skill, or just leveraged long exposure (buying calls)
on hand-picked names that happened to rip?

Test: on the EXACT same trade set (same day, symbol, spot, iv, option structure)
score three ways and compare:
  ACTUAL   -- follow the signal (call on UP-signal, put on DOWN-signal)
  ALL_UP   -- ignore the signal's direction, always buy a call (pure long-bias)
  ALL_DOWN -- always buy a put (mirror sanity check)
plus split ACTUAL into its UP-signal and DOWN-signal subsets.

Read:
- ACTUAL ~= ALL_UP  -> direction signal adds nothing; the edge is long-bias.
- ACTUAL  >  ALL_UP  -> direction timing genuinely contributes.
- DOWN-subset mean > 0 -> the strategy actually times downside (real skill), not
  just riding the up-names.

Leak-safe by construction: reuses highvol_strategies' own leak-tested signal
generation and options_pnl repricing; only the direction fed to the pricer is
overridden.
"""
from __future__ import annotations

import numpy as np

from stockmoney.backtest.highvol_strategies import (
    BREAKOUT_LOOKBACK,
    HIGHVOL_UNIVERSE,
    TREND_ADX_MIN,
    TREND_LOOKBACK,
    VOL_WIN,
    _adx_by_date,
    _load_highvol_closes,
)
from stockmoney.models.backtest_options_pnl import IV_RV_RATIO, build_option_outcomes
from stockmoney.models.feature_matrix import DOWN, UP
from stockmoney.models.metrics import bootstrap_mean_ci
from stockmoney.models.options_pnl import _mid_premium  # noqa: F401  (ensure module import ok)
import math

HORIZON = 5


def _trend_signals(sym, horizon):
    dts, cl, high, low = _load_highvol_closes(sym)
    adx = _adx_by_date(sym)
    n = len(cl)
    start = max(TREND_LOOKBACK, VOL_WIN, 28)
    rows = []
    for i in range(start, n - horizon):
        if adx.get(dts[i], 0.0) < TREND_ADX_MIN:
            continue
        mom = cl[i] / cl[i - TREND_LOOKBACK] - 1.0
        if mom == 0.0:
            continue
        d = UP if mom > 0 else DOWN
        rv = _realized(cl, i)
        if rv is None:
            continue
        rows.append((dts[i], dts[i + horizon], d, float(cl[i + horizon] / cl[i] - 1.0), float(cl[i]), rv))
    return rows


def _breakout_signals(sym, horizon):
    dts, cl, high, low = _load_highvol_closes(sym)
    n = len(cl)
    start = max(BREAKOUT_LOOKBACK, VOL_WIN)
    rows = []
    for i in range(start, n - horizon):
        ph = cl[i - BREAKOUT_LOOKBACK:i].max()
        pl_ = cl[i - BREAKOUT_LOOKBACK:i].min()
        if cl[i] > ph:
            d = UP
        elif cl[i] < pl_:
            d = DOWN
        else:
            continue
        rv = _realized(cl, i)
        if rv is None:
            continue
        rows.append((dts[i], dts[i + horizon], d, float(cl[i + horizon] / cl[i] - 1.0), float(cl[i]), rv))
    return rows


def _realized(cl, i):
    from stockmoney.backtest.highvol_strategies import _realized_vol
    rv = _realized_vol(cl, i, VOL_WIN)
    if not math.isfinite(rv) or rv <= 0:
        return None
    return rv * IV_RV_RATIO


def _score(rows, direction_override=None):
    """rows: (td, led, signal_dir, fwd, spot, iv). direction_override: None=use
    signal_dir, else force UP/DOWN for all. Returns list of option pnl."""
    n = len(rows)
    proba = np.zeros((n, 3))
    for k, r in enumerate(rows):
        d = r[2] if direction_override is None else direction_override
        proba[k, d] = 1.0
    tds = [r[0] for r in rows]
    leds = [r[1] for r in rows]
    fwd = np.array([r[3] for r in rows])
    spot = np.array([r[4] for r in rows])
    iv = np.array([r[5] for r in rows])
    reg = np.zeros(n, dtype=int)
    trades = build_option_outcomes(tds, leds, proba, fwd, spot, iv, reg)
    return [t.pnl_gross for t in trades]


def _line(name, pnls):
    if not pnls:
        print(f"  {name:<24} n=0")
        return
    arr = np.array(pnls)
    _, lo, hi = bootstrap_mean_ci(arr)
    win = float((arr > 0).mean())
    sig = " *" if lo > 0 else ""
    print(f"  {name:<24} n={len(pnls):>5} win={win*100:5.1f}% mean={arr.mean():+.4f} [{lo:+.4f},{hi:+.4f}]{sig}")


def main():
    for label, gen in (("TREND", _trend_signals), ("BREAKOUT", _breakout_signals)):
        rows = []
        for sym in HIGHVOL_UNIVERSE:
            rows.extend(gen(sym, HORIZON))
        n_up = sum(1 for r in rows if r[2] == UP)
        n_dn = sum(1 for r in rows if r[2] == DOWN)
        print(f"\n=== {label}  (horizon={HORIZON}, high-vol COIN/MSTR/IREN) ===")
        print(f"  signal split: UP={n_up} ({n_up/max(len(rows),1)*100:.0f}%)  DOWN={n_dn} ({n_dn/max(len(rows),1)*100:.0f}%)")
        _line("ACTUAL (follow signal)", _score(rows))
        _line("ALL_UP (long-bias only)", _score(rows, direction_override=UP))
        _line("ALL_DOWN (mirror)", _score(rows, direction_override=DOWN))
        up_rows = [r for r in rows if r[2] == UP]
        dn_rows = [r for r in rows if r[2] == DOWN]
        _line("  UP-signal subset", _score(up_rows))
        _line("  DOWN-signal subset", _score(dn_rows))
    print("\n判讀: ACTUAL≈ALL_UP => 方向訊號沒用,edge=長期做多槓桿。")
    print("      DOWN-subset mean<=0 => 做空那批在賠,策略沒有下行擇時能力。")


if __name__ == "__main__":
    main()
