"""Phase 0 -- 時光機回測「生死閘門」報告卡.

Reuses the *existing, leak-safe* harness (walk_forward + option-P&L repricing)
and adds the missing piece the keep/scrap decision needs: honest baselines run
through the SAME option machinery on the SAME OOS dates, so the only thing that
differs is the signal.

Strategies compared (all long options, net of theta + bid-ask spread):
  MODEL  -- current GMM-logistic direction model (skips RANGE rows)
  B1     -- always-long: buy a call every OOS day (market's upward-drift floor)
  B2     -- random: buy a call-or-put at random each day, averaged over many
            seeds (the "average person's random short-dated option" -- the most
            faithful reading of 市場平均交易勝率)

Decision rule (REBUILD_PLAN.md section 8): the MODEL must beat B2 on
EV-after-costs (mean option return) and beat B1 on directional accuracy, judged
risk-adjusted. Nothing here is cherry-picked -- every number is walk-forward OOS
and reported per regime, never a merged single win rate (CLAUDE.md section 12).

Usage:
    STOCKMONEY_DB=data/stockmoney_live.duckdb .venv/bin/python -m scripts.timemachine_report
"""
from __future__ import annotations

import os
import statistics
from dataclasses import dataclass

import numpy as np

from stockmoney.models.backtest_options_pnl import IV_RV_RATIO, build_option_outcomes
from stockmoney.models.direction import LogisticDirectionModel
from stockmoney.models.feature_matrix import build_feature_matrix, to_dataset
from stockmoney.models.metrics import bootstrap_mean_ci
from stockmoney.models.regime import KMeansGMMTrack
from stockmoney.models.walk_forward import run_walk_forward

N_FOLDS = 5
N_RANDOM_SEEDS = 25
DOWN, RANGE, UP = 0, 1, 2

# Trend engine configs, declared A PRIORI (never tuned on the OOS test set).
# The decision is "does the FAMILY of simple trend rules beat B1/B2 robustly",
# not "does the single best-tuned config" -- that would be the cardinal
# look-ahead sin. `CANON` is the one pre-committed config used in the verdict.
TREND_LOOKBACKS = (10, 20, 40)   # trading-day momentum lookback
TREND_ADX_GATES = (None, 20.0)   # None = ungated; 20 = only trade when adx_14>=20 (trending)
CANON = ("TREND_L20_adx20", 20, 20.0)


@dataclass
class StratResult:
    name: str
    n: int
    win_rate: float
    mean_ret: float
    ci_lo: float
    ci_hi: float
    avg_win: float
    avg_loss: float
    per_regime: dict[int, tuple[int, float, float]]  # regime -> (n, win_rate, mean_ret)


def _summarize(name: str, pnls: list[float], regimes: list[int]) -> StratResult:
    arr = np.array(pnls) if pnls else np.array([0.0])
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    _, lo, hi = bootstrap_mean_ci(arr)
    per: dict[int, tuple[int, float, float]] = {}
    for reg in sorted(set(regimes)):
        rp = [p for p, r in zip(pnls, regimes) if r == reg]
        if rp:
            per[reg] = (len(rp), sum(1 for p in rp if p > 0) / len(rp), statistics.fmean(rp))
    return StratResult(
        name=name,
        n=len(pnls),
        win_rate=len(wins) / len(pnls) if pnls else 0.0,
        mean_ret=statistics.fmean(pnls) if pnls else 0.0,
        ci_lo=lo,
        ci_hi=hi,
        avg_win=statistics.fmean(wins) if wins else 0.0,
        avg_loss=statistics.fmean(losses) if losses else 0.0,
        per_regime=per,
    )


def _proba_for(kind: str, n: int, rng: np.random.Generator | None) -> np.ndarray:
    """Synthetic (n,3) proba so build_option_outcomes' argmax picks the intended
    direction. B1 = always UP; B2 = random UP/DOWN (never RANGE, so it always
    trades, matching 'randomly buy an option')."""
    p = np.zeros((n, 3))
    if kind == "B1":
        p[:, UP] = 1.0
    elif kind == "B2":
        assert rng is not None
        picks = rng.choice([DOWN, UP], size=n)
        p[np.arange(n), picks] = 1.0
    return p


def _trend_proba(
    trade_dates: list,
    closes: list[tuple],
    adx_by_date: dict,
    lookback: int,
    adx_min: float | None,
) -> np.ndarray:
    """順勢 signal, leak-free: at each trade_date use ONLY closes up to and
    including that date. Direction = sign of `lookback`-day momentum; gated to
    no-trade (RANGE) when adx_14 < adx_min (sit out chop). Emits synthetic
    (n,3) proba so it flows through the SAME option machinery as MODEL/B1/B2."""
    vals = [c for _, c in closes]
    idx = {d: i for i, (d, _) in enumerate(closes)}
    p = np.zeros((len(trade_dates), 3))
    for k, td in enumerate(trade_dates):
        i = idx.get(td)
        if i is None or i < lookback:
            p[k, RANGE] = 1.0
            continue
        if adx_min is not None and adx_by_date.get(td, 0.0) < adx_min:
            p[k, RANGE] = 1.0  # chop regime -> no directional option
            continue
        mom = vals[i] / vals[i - lookback] - 1.0
        p[k, UP if mom > 0 else (DOWN if mom < 0 else RANGE)] = 1.0
    return p


def run_horizon(conn, horizon: int) -> dict[str, StratResult]:
    matrix = build_feature_matrix(conn, target_symbol="SOXL", sector="semiconductor", horizon=horizon)
    if matrix.height == 0:
        raise SystemExit("empty feature matrix -- wrong DB? point STOCKMONEY_DB at stockmoney_live.duckdb")
    dataset = to_dataset(matrix)

    wf = run_walk_forward(
        dataset,
        lambda: KMeansGMMTrack(method="gmm", n_regimes=3, seed=0),
        lambda: LogisticDirectionModel(seed=0, calibrate="sigmoid"),
        n_folds=N_FOLDS,
    )

    from stockmoney.models.backtest_options_pnl import _closes_by_date

    rv_by_date = {d: float(x[0]) for d, x in zip(dataset.trade_dates, dataset.X)}  # realized_vol_20d = col 0
    close_by_date = _closes_by_date(conn)
    entry_spot = np.array([close_by_date[d] for d in wf.trade_dates])
    entry_iv = np.array([rv_by_date[d] * IV_RV_RATIO for d in wf.trade_dates])
    n = len(wf.trade_dates)

    def trades_from(proba: np.ndarray):
        return build_option_outcomes(
            wf.trade_dates, wf.label_end_dates, proba, wf.fwd_return, entry_spot, entry_iv, wf.regime
        )

    results: dict[str, StratResult] = {}

    model_trades = trades_from(wf.proba)
    results["MODEL"] = _summarize("MODEL", [t.pnl_gross for t in model_trades], [t.regime for t in model_trades])

    b1 = trades_from(_proba_for("B1", n, None))
    results["B1_always_long"] = _summarize("B1_always_long", [t.pnl_gross for t in b1], [t.regime for t in b1])

    # B2: pool trades across many seeds so the CI reflects real dispersion.
    b2_pnls: list[float] = []
    b2_regs: list[int] = []
    for s in range(N_RANDOM_SEEDS):
        rng = np.random.default_rng(1000 + s)
        tr = trades_from(_proba_for("B2", n, rng))
        b2_pnls += [t.pnl_gross for t in tr]
        b2_regs += [t.regime for t in tr]
    results["B2_random"] = _summarize("B2_random", b2_pnls, b2_regs)

    # 順勢 trend engine -- a priori robustness panel (never tuned on OOS).
    from stockmoney.models.feature_matrix import _load_closes

    closes = _load_closes(conn, "SOXL")
    adx_by_date = {d: float(x[1]) for d, x in zip(dataset.trade_dates, dataset.X)}  # adx_14 = col 1
    for lb in TREND_LOOKBACKS:
        for gate in TREND_ADX_GATES:
            name = f"TREND_L{lb}_adx{int(gate) if gate else 'none'}"
            proba = _trend_proba(wf.trade_dates, closes, adx_by_date, lb, gate)
            tr = trades_from(proba)
            results[name] = _summarize(name, [t.pnl_gross for t in tr], [t.regime for t in tr])
            # directional accuracy over the rows it actually traded (vs y_true)
            traded = proba.argmax(axis=1) != RANGE
            if traded.any():
                results[name].__dict__["dir_acc"] = float(
                    np.mean(proba[traded].argmax(axis=1) == wf.y_true[traded])
                )

    # Directional accuracy (own terms): P(argmax) vs realized y, model vs 50%.
    y = wf.y_true
    model_dir_acc = float(np.mean(wf.proba.argmax(axis=1) == y))
    # B1 accuracy = fraction of days the underlying actually went UP.
    b1_dir_acc = float(np.mean(y == UP))
    results["MODEL"].__dict__["dir_acc"] = model_dir_acc
    results["B1_always_long"].__dict__["dir_acc"] = b1_dir_acc
    results["B2_random"].__dict__["dir_acc"] = 0.5
    return results


def _print_horizon(horizon: int, res: dict[str, StratResult]) -> None:
    print(f"\n{'='*78}\nHORIZON = {horizon} 天  (SOXL, 8y walk-forward OOS, net theta+spread)\n{'='*78}")
    print(f"{'strategy':<20}{'n':>6}{'win%':>8}{'mean_ret':>11}{'ci_lo':>9}{'ci_hi':>9}{'dir_acc':>9}")
    order = ["MODEL", "B1_always_long", "B2_random"] + [k for k in res if k.startswith("TREND")]
    for key in order:
        r = res[key]
        da = getattr(r, "dir_acc", float("nan"))
        sig = " *" if r.ci_lo > 0 else ""  # profitable at 95%
        print(f"{r.name:<20}{r.n:>6}{r.win_rate*100:>7.1f}%{r.mean_ret:>+11.4f}"
              f"{r.ci_lo:>+9.4f}{r.ci_hi:>+9.4f}{da:>9.3f}{sig}")
    print("  per-regime mean option return (n, win%, mean_ret) -- MODEL vs canonical trend:")
    for key in ("MODEL", CANON[0]):
        if key not in res:
            continue
        r = res[key]
        cells = "  ".join(f"R{reg}:{v[0]}/{v[1]*100:.0f}%/{v[2]:+.3f}" for reg, v in sorted(r.per_regime.items()))
        print(f"    {r.name:<20}{cells}")


def _verdict(all_res: dict[int, dict[str, StratResult]]) -> None:
    print(f"\n{'='*82}\nPhase 1a 判決 -- 順勢 trend 心臟 vs 舊模型 vs baseline(canonical={CANON[0]})\n{'='*82}")
    for h in sorted(all_res):
        res = all_res[h]
        m, b1, b2 = res["MODEL"], res["B1_always_long"], res["B2_random"]
        t = res.get(CANON[0])
        print(f"\n  horizon={h}天  mean option return(扣成本):")
        print(f"    舊MODEL {m.mean_ret:+.4f} [{m.ci_lo:+.4f},{m.ci_hi:+.4f}]  |  "
              f"B1裸多 {b1.mean_ret:+.4f}  |  B2隨機 {b2.mean_ret:+.4f}")
        if t:
            beats_b1 = t.mean_ret > b1.mean_ret
            beats_b2 = t.mean_ret > b2.mean_ret
            sig = t.ci_lo > 0
            print(f"    順勢TREND {t.mean_ret:+.4f} [{t.ci_lo:+.4f},{t.ci_hi:+.4f}] (n={t.n}, win {t.win_rate*100:.0f}%)  "
                  f"-> vs B1 {'WIN' if beats_b1 else 'lose'} / vs B2 {'WIN' if beats_b2 else 'lose'} / "
                  f"顯著獲利 {'YES' if sig else 'no'}")
    print(f"\n  解讀準則:順勢心臟要在目標 1-3 天(h=2,3)上,mean_ret 打贏 B1 且 CI 下界>0 才算「有心跳」。")
    print(f"  任何單一 config 贏不算數 -- 看上方 robustness 面板整族 TREND 是否一致優於 baseline。")


def main() -> None:
    from stockmoney.data.db import DEFAULT_DB_PATH, get_connection

    db = os.environ.get("STOCKMONEY_DB", DEFAULT_DB_PATH)
    print(f"DB = {db}")
    conn = get_connection(db)
    all_res = {}
    for h in (5, 3, 2):
        all_res[h] = run_horizon(conn, h)
        _print_horizon(h, all_res[h])
    _verdict(all_res)
    conn.close()


if __name__ == "__main__":
    main()
