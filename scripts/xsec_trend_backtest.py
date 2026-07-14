"""Phase 1b -- 橫向順勢(cross-sectional trend)回測.

Phase 1a hit a ceiling: on a single leveraged-bull symbol (SOXL 2018-26), no
trend rule beats "always buy a call" -- because the symbol only ever went up.
Lin's real 順勢 edge isn't "follow SOXL", it's "put money where the strength
is" -- each day rank a basket and trade only the strongest names.

This backtest tests exactly that, leak-free (momentum & realized-vol proxy use
ONLY closes up to the decision date), through the SAME option-P&L engine
(build_option_outcomes: Black-Scholes reprice, theta, bid-ask spread) so numbers
are directly comparable to Phase 1a's baselines.

The decisive comparison is SELECTION vs NO-SELECTION:
  XSEC_topK   -- long calls on the K strongest names each day
  B1_EW       -- long calls on ALL names equally (breadth, no selection)
  B1_SOXL     -- always long SOXL (the Phase-1a leveraged-bull benchmark)
  B2_random   -- random name + random direction
If XSEC_topK beats B1_EW, *selection itself* adds value -- that is the Discovery
pillar's whole premise, measured honestly.

Universe = the 9 single names with full 8y history (SOXL/SOXS excluded from the
cross-section: leveraged derivatives of the semi basket would double-count).

Usage:
    STOCKMONEY_DB=data/stockmoney_live.duckdb .venv/bin/python -m scripts.xsec_trend_backtest
"""
from __future__ import annotations

import math
import os
import statistics
from datetime import date

import numpy as np

from stockmoney.models.backtest_options_pnl import IV_RV_RATIO, build_option_outcomes
from stockmoney.models.metrics import bootstrap_mean_ci

UNIVERSE = ["AAPL", "AMD", "AMZN", "AVGO", "GOOGL", "META", "MSFT", "NVDA", "TSM"]
DOWN, RANGE, UP = 0, 1, 2
LOOKBACK = 20
VOL_WIN = 20
HORIZONS = (2, 3, 5)
TOPKS = (1, 2, 3)
N_RANDOM_SEEDS = 25


def _load_universe_closes(conn) -> tuple[list[date], dict[str, np.ndarray]]:
    """Aligned close series across UNIVERSE on their common trading dates."""
    per: dict[str, dict[date, float]] = {}
    for sym in UNIVERSE:
        rows = conn.execute(
            "SELECT trade_date, close FROM ohlcv_daily WHERE symbol=? AND close IS NOT NULL ORDER BY trade_date",
            [sym],
        ).fetchall()
        per[sym] = {d: float(c) for d, c in rows}
    common = sorted(set.intersection(*[set(m) for m in per.values()]))
    closes = {sym: np.array([per[sym][d] for d in common]) for sym in UNIVERSE}
    return common, closes


def _realized_vol(close: np.ndarray, i: int, win: int) -> float:
    """Annualized realized vol from trailing `win` daily log returns ending at i
    (uses only past closes -> leak-free). Falls back for degenerate windows."""
    if i < win:
        return float("nan")
    seg = close[i - win : i + 1]
    rets = np.diff(np.log(seg))
    sd = float(np.std(rets, ddof=1)) if len(rets) > 1 else 0.0
    return sd * math.sqrt(252.0)


def _assemble(trades: list[tuple], horizon_days_map: dict) -> list:
    """trades: list of (trade_date, label_end_date, direction, fwd_return,
    entry_spot, entry_iv). Route through the shared option engine."""
    if not trades:
        return []
    n = len(trades)
    tds = [t[0] for t in trades]
    leds = [t[1] for t in trades]
    proba = np.zeros((n, 3))
    for k, t in enumerate(trades):
        proba[k, t[2]] = 1.0
    fwd = np.array([t[3] for t in trades])
    spot = np.array([t[4] for t in trades])
    iv = np.array([t[5] for t in trades])
    regime = np.zeros(n, dtype=int)
    return build_option_outcomes(tds, leds, proba, fwd, spot, iv, regime)


def _stats(name: str, pnls: list[float]) -> dict:
    if not pnls:
        return {"name": name, "n": 0}
    arr = np.array(pnls)
    _, lo, hi = bootstrap_mean_ci(arr)
    wins = [p for p in pnls if p > 0]
    return {
        "name": name,
        "n": len(pnls),
        "win": len(wins) / len(pnls),
        "mean": statistics.fmean(pnls),
        "lo": lo,
        "hi": hi,
    }


def run_horizon(common, closes, horizon: int, seed_universe_gate: bool) -> list[dict]:
    n = len(common)
    start = max(LOOKBACK, VOL_WIN)
    # valid decision indices: enough history behind, and a resolvable exit ahead
    idxs = [i for i in range(start, n - horizon)]

    # Precompute per-symbol momentum, vol, fwd_return at each valid i
    mom = {s: np.array([closes[s][i] / closes[s][i - LOOKBACK] - 1.0 for i in idxs]) for s in UNIVERSE}
    iv = {s: np.array([_realized_vol(closes[s], i, VOL_WIN) * IV_RV_RATIO for i in idxs]) for s in UNIVERSE}
    fwd = {s: np.array([closes[s][i + horizon] / closes[s][i] - 1.0 for i in idxs]) for s in UNIVERSE}
    spot = {s: np.array([closes[s][i] for i in idxs]) for s in UNIVERSE}
    dts = [common[i] for i in idxs]
    leds = [common[i + horizon] for i in idxs]

    results: list[dict] = []

    def build(pick_fn, name, rng=None):
        trades = []
        for k in range(len(idxs)):
            picks = pick_fn(k, rng)
            for s, direction in picks:
                if not math.isfinite(iv[s][k]) or iv[s][k] <= 0:
                    continue
                trades.append((dts[k], leds[k], direction, float(fwd[s][k]), float(spot[s][k]), float(iv[s][k])))
        ot = _assemble(trades, {})
        return _stats(name, [t.pnl_gross for t in ot])

    # XSEC top-K: long the K strongest by momentum (optionally require mom>0)
    def make_topk(K, gate):
        def f(k, rng):
            ranked = sorted(UNIVERSE, key=lambda s: mom[s][k], reverse=True)
            out = []
            for s in ranked[:K]:
                if gate and mom[s][k] <= 0:
                    continue
                out.append((s, UP))
            return out
        return f

    for K in TOPKS:
        results.append(build(make_topk(K, False), f"XSEC_top{K}"))
        results.append(build(make_topk(K, True), f"XSEC_top{K}_gate"))

    # B1_EW: long a call on every name every day (breadth, no selection)
    results.append(build(lambda k, rng: [(s, UP) for s in UNIVERSE], "B1_EW_all"))

    # B2: random name + random direction, pooled over seeds
    b2_pnls = []
    for sd in range(N_RANDOM_SEEDS):
        rng = np.random.default_rng(7000 + sd)
        trades = []
        for k in range(len(idxs)):
            s = UNIVERSE[rng.integers(len(UNIVERSE))]
            d = UP if rng.random() < 0.5 else DOWN
            if math.isfinite(iv[s][k]) and iv[s][k] > 0:
                trades.append((dts[k], leds[k], d, float(fwd[s][k]), float(spot[s][k]), float(iv[s][k])))
        ot = _assemble(trades, {})
        b2_pnls += [t.pnl_gross for t in ot]
    results.append(_stats("B2_random", b2_pnls))

    return results


def _b1_soxl(conn, horizon: int) -> dict:
    rows = conn.execute(
        "SELECT trade_date, close FROM ohlcv_daily WHERE symbol='SOXL' AND close IS NOT NULL ORDER BY trade_date",
        [],
    ).fetchall()
    dts = [d for d, _ in rows]
    cl = np.array([float(c) for _, c in rows])
    n = len(cl)
    start = max(LOOKBACK, VOL_WIN)
    trades = []
    for i in range(start, n - horizon):
        rv = _realized_vol(cl, i, VOL_WIN) * IV_RV_RATIO
        if not math.isfinite(rv) or rv <= 0:
            continue
        trades.append((dts[i], dts[i + horizon], UP, float(cl[i + horizon] / cl[i] - 1.0), float(cl[i]), float(rv)))
    ot = _assemble(trades, {})
    return _stats("B1_SOXL", [t.pnl_gross for t in ot])


def main() -> None:
    from stockmoney.data.db import DEFAULT_DB_PATH, get_connection

    db = os.environ.get("STOCKMONEY_DB", DEFAULT_DB_PATH)
    print(f"DB = {db}\nUNIVERSE(8y) = {UNIVERSE}")
    conn = get_connection(db)
    common, closes = _load_universe_closes(conn)
    print(f"common trading dates: {len(common)}  {common[0]} -> {common[-1]}")

    for h in HORIZONS:
        rows = run_horizon(common, closes, h, seed_universe_gate=False)
        rows.append(_b1_soxl(conn, h))
        print(f"\n{'='*80}\nHORIZON={h}天  (long calls, net theta+spread, leak-free momentum L{LOOKBACK})\n{'='*80}")
        print(f"{'strategy':<18}{'n':>7}{'win%':>8}{'mean_ret':>11}{'ci_lo':>9}{'ci_hi':>9}")
        order = sorted(rows, key=lambda r: (not r["name"].startswith("XSEC"), r["name"]))
        for r in order:
            if r["n"] == 0:
                continue
            sig = " *" if r["lo"] > 0 else ""
            print(f"{r['name']:<18}{r['n']:>7}{r['win']*100:>7.1f}%{r['mean']:>+11.4f}{r['lo']:>+9.4f}{r['hi']:>+9.4f}{sig}")

    print("\n判讀:XSEC_topK 若 mean_ret 打贏 B1_EW_all -> 『選最強』本身加值(Discovery 支柱前提成立)。")
    print("      若 CI 下界>0(*) -> 這顆心臟顯著獲利,可進入工人階段包 Discovery+劇本。")
    conn.close()


if __name__ == "__main__":
    main()
