"""Phase 1c -- premium-selling(賣方,cash-secured OTM put)回測.

Phases 0/1a/1b showed the *buy-side* 1-3 day option game is structurally hard:
long options bleed theta and nothing beat simply being long. Premium-selling is
the structural opposite -- you COLLECT theta and win whenever the underlying
doesn't fall through your strike. That is exactly the "穩定高勝率" the user
wants, at the cost of occasional fat-tail losses. This backtest measures whether
that structure is net-positive after costs AND fat tails, on the same 8y data.

Leak-free: strike/IV use only closes up to the decision date. Repricing reuses
options_pnl (Black-Scholes, side="short", theta, bid-ask spread), held to expiry.

Honest accounting: buy-side backtests reported return-on-premium, which is not
comparable across instruments. Here the headline is **return on collateral**
(P&L / strike = the cash a cash-secured put ties up), directly comparable to the
long-stock baseline's return on the same capital. Win-rate and the single worst
trade are reported alongside, because a high win-rate with an unbounded tail is
the classic way premium-selling blows up -- we surface it, not hide it.

Universe = 9 single names (8y) + SOXL (to see the leveraged case).

Usage:
    STOCKMONEY_DB=data/stockmoney_live.duckdb .venv/bin/python -m scripts.premium_selling_backtest
"""
from __future__ import annotations

import math
import os
import statistics
from datetime import date

import numpy as np

from stockmoney.models.metrics import bootstrap_mean_ci
from stockmoney.models.options_pnl import (
    DEFAULT_RATE,
    DEFAULT_SPREAD_PCT,
    OptionEntry,
    OptionExit,
    entry_premium,
    exit_premium,
)

UNIVERSE = ["AAPL", "AMD", "AMZN", "AVGO", "GOOGL", "META", "MSFT", "NVDA", "TSM", "SOXL"]
IV_RV_RATIO = 1.1
VOL_WIN = 20
OTM_LEVELS = (0.03, 0.05, 0.07)   # sell put this far below spot
HOLD_TDS = (5, 10)                # trading days held to expiry (~7 / ~14 calendar)


def _load_closes(conn, sym) -> tuple[list[date], np.ndarray]:
    rows = conn.execute(
        "SELECT trade_date, close FROM ohlcv_daily WHERE symbol=? AND close IS NOT NULL ORDER BY trade_date",
        [sym],
    ).fetchall()
    return [d for d, _ in rows], np.array([float(c) for _, c in rows])


def _realized_vol(close: np.ndarray, i: int, win: int) -> float:
    if i < win:
        return float("nan")
    rets = np.diff(np.log(close[i - win : i + 1]))
    sd = float(np.std(rets, ddof=1)) if len(rets) > 1 else 0.0
    return sd * math.sqrt(252.0)


def _sma(cl, i, w):
    return float(cl[i - w + 1 : i + 1].mean()) if i >= w - 1 else float("nan")


def _sell_put_trades(dts, cl, otm: float, hold_td: int, *, gate: bool = False):
    """Yield (collateral_return, won, long_stock_return) per decision date.

    gate=True applies Lin's 「情緒差空手」discipline, using only past closes:
    skip selling the put when the name is in a downtrend (close < SMA50) OR
    realized vol is in its own elevated zone (> rolling 6-month 80th pct) --
    i.e. don't sell puts into falling / panicking names, where the fat tail lives."""
    n = len(cl)
    out = []
    # precompute a trailing realized-vol series for the vol-percentile gate
    rvs = np.array([_realized_vol(cl, j, VOL_WIN) for j in range(n)])
    for i in range(max(VOL_WIN, 50), n - hold_td):
        rv = _realized_vol(cl, i, VOL_WIN)
        if not math.isfinite(rv) or rv <= 0:
            continue
        if gate:
            sma50 = _sma(cl, i, 50)
            past_rv = rvs[max(0, i - 126):i]
            past_rv = past_rv[np.isfinite(past_rv)]
            vol_hi = np.percentile(past_rv, 80) if len(past_rv) > 20 else np.inf
            if (math.isfinite(sma50) and cl[i] < sma50) or rv > vol_hi:
                continue  # downtrend or panic -> sit out
        spot = float(cl[i])
        strike = spot * (1.0 - otm)
        days_cal = max((dts[i + hold_td] - dts[i]).days, 1)
        entry = OptionEntry(spot=spot, strike=strike, is_call=False, iv=rv * IV_RV_RATIO,
                            t_years=days_cal / 365.0, side="short")
        p_in = entry_premium(entry)
        if p_in <= 0:
            continue
        exit_spot = float(cl[i + hold_td])
        # held to expiry: exit premium = intrinsic (t_exit -> 0)
        p_out = exit_premium(entry, OptionExit(spot=exit_spot, iv=None, days_held=hold_td_calendar(days_cal)))
        half = DEFAULT_SPREAD_PCT / 2.0
        proceeds_in = p_in * (1.0 - half)         # sell put at bid
        cost_out = p_out * (1.0 + half)            # buy back / assignment at ask
        pnl_per_share = proceeds_in - cost_out
        collateral_return = pnl_per_share / strike  # return on cash-secured collateral
        won = exit_spot >= strike                    # put expired OTM -> kept premium
        long_ret = exit_spot / spot - 1.0
        out.append((collateral_return, won, long_ret))
    return out


def hold_td_calendar(days_cal: int) -> int:
    # held to expiry: days_held == full life so t_exit collapses to 0 (intrinsic)
    return days_cal


def _summ(name, rows):
    if not rows:
        return None
    cr = np.array([r[0] for r in rows])
    _, lo, hi = bootstrap_mean_ci(cr)
    win = statistics.fmean([1.0 if r[1] else 0.0 for r in rows])
    long_mean = statistics.fmean([r[2] for r in rows])
    return {
        "name": name, "n": len(rows), "win": win,
        "mean": float(cr.mean()), "lo": lo, "hi": hi,
        "worst": float(cr.min()), "long_mean": long_mean,
    }


def main() -> None:
    from stockmoney.data.db import DEFAULT_DB_PATH, get_connection

    db = os.environ.get("STOCKMONEY_DB", DEFAULT_DB_PATH)
    print(f"DB = {db}\nUNIVERSE = {UNIVERSE}\n")
    conn = get_connection(db)
    closes = {s: _load_closes(conn, s) for s in UNIVERSE}

    for hold in HOLD_TDS:
        for otm in OTM_LEVELS:
            for gate in (False, True):
                pooled = []
                for s in UNIVERSE:
                    dts, cl = closes[s]
                    pooled += _sell_put_trades(dts, cl, otm, hold, gate=gate)
                tag = "GATED" if gate else "naive"
                r = _summ(f"sellput_otm{int(otm*100)}_hold{hold}td_{tag}", pooled)
                if r is None:
                    continue
                sig = " *" if r["lo"] > 0 else ""
                print(f"{r['name']:<32} n={r['n']:>5} win={r['win']*100:5.1f}%  "
                      f"ret/collat={r['mean']:+.4f} [{r['lo']:+.4f},{r['hi']:+.4f}]{sig}  "
                      f"worst={r['worst']:+.3f}  |  做多={r['long_mean']:+.4f}")
            print()

    print("\n判讀:賣 put 的 ret/collat 是『對綁住資金的報酬』,跟『同期做多均報』同一資金基礎可比。")
    print("      期待樣態:win% 很高(70-90%)、報酬穩定為正(CI下界>0=*)、但看 worst 尾端有多痛。")
    print("      若穩定為正且尾端可控 -> premium-selling 是『穩定高勝率』心臟的候選,進工人階段。")
    conn.close()


if __name__ == "__main__":
    main()
