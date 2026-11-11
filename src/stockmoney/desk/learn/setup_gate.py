"""Per-setup live gate: ``python -m stockmoney.desk.learn.setup_gate [--db] [--by-dte]``.

Read-only analysis (owner decision 2026-11-04). orb15, vwap_reclaim and
gap_fade went back to paper live orders with caps unchanged; whether a setup
keeps its live slot is judged from its sample here, not from a story. Nothing
in risk, veto, bridge policy, the options engine or execution code reads this
module: a verdict never opens, blocks or sizes anything. Acting on it is the
owner's decision.

Scope: EXITED options_positions rows of both modes (live and shadow), per
source and optionally per source x dte_label. Only the live exit variant
(cfg.live_exit) is counted, and a shadow row whose setup_id and
dte_label match a live row is dropped (same event, Grok Build review
2026-11-05): every shadow entry is booked once per exit
variant, so counting A and B would count the same entry twice. Exits with
unknown P&L (options/report.py UNKNOWN_PNL) are left out.

Trade P&L = realized_usd - fees_usd (realized already pays the ask and sells
at the bid). Lower bound = the more conservative of a trade bootstrap and a
day-block bootstrap (resample whole sessions), each the 5th percentile of
5000 resampled means with a fixed seed: the 90% two-sided CI lower bound.

PASS needs all of: n >= 100, lower bound > 0, distinct days >= 15, and the
best single day <= 50% of total P&L (n/a when total <= 0, which fails).

Variant registry (EDGE_PLAN 7.4 item 5, learn/variants.py): a source's
trades dated before its registered date are excluded (they are not its
sample); an unregistered source keeps all its rows. The report prints the
family size K and the out-of-sample mean (trades on the last third of the
source's distinct trading days).

Multiple-comparison correction (owner decision 2026-11-10, EVOLUTION_PLAN
section 6 item 9 / docs/desk/EDGE_PLAN.md section 8): with K variants
running at once, about K x 5% of them pass an UNCORRECTED 90% one-sided
lower bound by luck. ``rows()``/``report()`` therefore bootstrap the
conservative lower bound at a Bonferroni/Holm-adjusted alpha = ALPHA / K
(``variants.family_size()``, the one place K is counted) instead of the raw
ALPHA, so the PASS threshold itself gets stricter as more variants run.
``metrics()`` defaults to K=1 (alpha unchanged) for callers that pass their
own synthetic trades without a family context; callers that go through the
real gate (``rows()``) always pass K explicitly.

Scope (``scope_rows``): EXITED rows, the live exit variant only, excluding
unknown-P&L exits and a shadow row whose setup_id and dte_label match a
live row (same event, counted once). Shared with learn/v_variants.py so its
derived shadow filters (V03/V05/V06, EVOLUTION_PLAN P1) see the same event
set as the main gate.

Stressed-stop sizing (report only): stressed cost per contract = premium-stop
loss + half the entry spread + fees for both legs; stressed qty = floor($1,000
/ stressed cost). Hypothetical P&L = trade P&L per contract x stressed qty.
The engine stores these in flags_json["stressed"]; older rows are recomputed
from their columns.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

from stockmoney.desk.learn import variants
from stockmoney.desk.options import spread_variant as sv
from stockmoney.desk.options.config import OptionsConfig
from stockmoney.desk.options.shadow import stressed_risk

DB_PATH = Path("data/desk/desk.sqlite")
UNKNOWN_PNL = ("broker_flat_unreconciled", "manual_resolved")  # options/report.py UNKNOWN_PNL
MIN_N = 100
MIN_DAYS = 15
MAX_DAY_SHARE = 0.5
RESAMPLES = 5000
SEED = 20261104
ALPHA = 0.05  # one tail of a 90% two-sided interval
STRESS_BUDGET = 1000.0


def _stop_pct(variant: str | None, cfg: OptionsConfig) -> float:
    return cfg.premium_stop_pct_b if variant == "B" else cfg.premium_stop_pct


def _lower(values: np.ndarray, rng: np.random.Generator, alpha: float = ALPHA) -> float:
    idx = rng.integers(0, len(values), size=(RESAMPLES, len(values)))
    return float(np.quantile(values[idx].mean(axis=1), alpha))


def _day_block_lower(pnl: np.ndarray, days: list[str], rng: np.random.Generator, alpha: float = ALPHA) -> float:
    """Resample whole sessions with replacement; the statistic is the mean
    per trade (sum of picked days' P&L / number of their trades)."""
    keys = sorted(set(days))
    sums = np.array([pnl[[d == k for d in days]].sum() for k in keys])
    counts = np.array([sum(d == k for d in days) for k in keys])
    idx = rng.integers(0, len(keys), size=(RESAMPLES, len(keys)))
    return float(np.quantile(sums[idx].sum(axis=1) / counts[idx].sum(axis=1), alpha))


def metrics(trades: list[dict], k: int = 1) -> dict:
    """``trades``: dicts with pnl, day, mode and optionally pnl_stressed.
    ``k``: family size (variants.family_size()); the conservative lower
    bound is bootstrapped at the Bonferroni/Holm-adjusted alpha = ALPHA / k
    (k=1, the default, leaves alpha unchanged for direct callers that are
    not checking a registered variant against the rest of the family)."""
    n = len(trades)
    k = max(int(k), 1)
    alpha = ALPHA / k
    out = {"n": n, "days": len({t["day"] for t in trades}), "k": k, "alpha_adj": alpha,
           "live": sum(t["mode"] == "live" for t in trades), "shadow": sum(t["mode"] != "live" for t in trades),
           "mean": None, "lb_trade": None, "lb_day": None, "lb": None, "total": 0.0, "day_share": None,
           "stressed_total": None, "oos_mean": None, "verdict": "FAIL"}
    if not n:
        return out
    pnl = np.array([float(t["pnl"]) for t in trades])
    days = [t["day"] for t in trades]
    rng = np.random.default_rng(SEED)
    out["mean"] = float(pnl.mean())
    out["lb_trade"] = _lower(pnl, rng, alpha)
    out["lb_day"] = _day_block_lower(pnl, days, rng, alpha)
    out["lb"] = min(out["lb_trade"], out["lb_day"])
    out["total"] = float(pnl.sum())
    if out["total"] > 0:
        per_day: dict[str, float] = {}
        for t in trades:
            per_day[t["day"]] = per_day.get(t["day"], 0.0) + float(t["pnl"])
        out["day_share"] = max(per_day.values()) / out["total"]
    keys = sorted(set(days))
    tail = set(keys[len(keys) - (len(keys) + 2) // 3:])  # last third of distinct days, rounded up
    oos = [float(t["pnl"]) for t in trades if t["day"] in tail]
    out["oos_mean"] = float(np.mean(oos)) if oos else None
    st = [t["pnl_stressed"] for t in trades if t.get("pnl_stressed") is not None]
    out["stressed_total"] = float(sum(st)) if st else None
    ok = (n >= MIN_N and out["lb"] > 0 and out["days"] >= MIN_DAYS
          and out["day_share"] is not None and out["day_share"] <= MAX_DAY_SHARE)
    out["verdict"] = "PASS" if ok else "FAIL"
    return out


def event_pnl(r, cfg: OptionsConfig) -> dict:
    """One scope_rows() row -> a ``metrics()`` trade dict (pnl, day, mode,
    pnl_stressed). Public: learn/v_variants.py's derived filters reuse this
    so every variant prices an event the same way the main gate does."""
    pnl = (r["realized_usd"] or 0.0) - (r["fees_usd"] or 0.0)
    try:
        flags = json.loads(r["flags_json"] or "{}")
    except (TypeError, ValueError):
        flags = {}
    st = flags.get("stressed") if isinstance(flags, dict) else None
    if not isinstance(st, dict) or "qty" not in st:
        st = (stressed_risk(r["entry_price"], r["entry_bid"], r["entry_ask"], _stop_pct(r["exit_variant"], cfg), cfg,
                            STRESS_BUDGET)
              if r["entry_price"] else None)
    qty = r["qty_initial"] or 0
    ps = pnl / qty * st["qty"] if st and qty else None
    return {"pnl": pnl, "day": r["day"], "mode": r["mode"], "pnl_stressed": ps}


SCOPE_COLUMNS = ("setup_id", "source", "dte_label", "mode", "day", "realized_usd", "fees_usd", "entry_price",
                 "entry_bid", "entry_ask", "qty_initial", "exit_variant", "flags_json", "uclass", "entry_time",
                 "exit_time", "exit_reason")


def scope_rows(con, cfg: OptionsConfig | None = None) -> list[sqlite3.Row]:
    """Rows in the 5.1 gate's scope: EXITED, known P&L, the live exit variant
    only, registered-date exclusion (variants.registered_since) and the
    live/shadow-twin dedup (a live fill and its live-exit shadow twin are one
    event; keep the live row). Shared by ``rows()`` and learn/v_variants.py so
    both see the same event set with the same column set."""
    cfg = cfg or OptionsConfig()
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='options_positions'").fetchone() is None:
        return []  # options schema not created yet (no options session has run)
    marks = ",".join("?" * len(UNKNOWN_PNL))
    prev = con.row_factory
    con.row_factory = sqlite3.Row
    try:
        got = con.execute(
            f"SELECT {', '.join(SCOPE_COLUMNS)} FROM options_positions WHERE state='EXITED' "
            f"AND COALESCE(exit_reason,'') NOT IN ({marks}) AND COALESCE(exit_variant, ?) = ? "
            "ORDER BY source, dte_label, day", (*UNKNOWN_PNL, cfg.live_exit, cfg.live_exit)).fetchall()
    finally:
        con.row_factory = prev
    live_keys = {(r["setup_id"], r["dte_label"]) for r in got if r["mode"] == "live" and r["setup_id"]}
    out = []
    for r in got:
        since = variants.registered_since(r["source"])
        if since and (r["day"] or "") < since:
            continue  # before the variant was registered: not its sample
        if r["mode"] != "live" and (r["setup_id"], r["dte_label"]) in live_keys:
            continue
        out.append(r)
    return out


def rows(con, by_dte: bool = False, cfg: OptionsConfig | None = None) -> list[dict]:
    """One dict per source (or source x dte_label) with the gate metrics."""
    cfg = cfg or OptionsConfig()
    k = variants.family_size()
    groups: dict[tuple, list[dict]] = {}
    for r in scope_rows(con, cfg):
        key = (r["source"], r["dte_label"] or "?") if by_dte else (r["source"], None)
        groups.setdefault(key, []).append(event_pnl(r, cfg))
    return [{"source": s, "dte_label": d, **metrics(ts, k=k)} for (s, d), ts in sorted(groups.items())]


def _f(x, fmt="{:+.0f}") -> str:
    return "n/a" if x is None else fmt.format(x)


def report(con, by_dte: bool = False) -> str:
    rs = rows(con, by_dte)
    k = variants.family_size()
    adj_alpha = ALPHA / max(k, 1)
    lines = ["## 逐策略實盤關卡(per-setup live gate,唯讀)", "",
             f"變體家族大小 K = {k}(learn/variants.py)。同時跑 K 個變體,未校正時約 K x 5% = {k * 0.05:.1f} 個"
             "會靠運氣通過 90% 單邊下界,所以本報告的下界改用 Bonferroni/Holm 校正後的 "
             f"alpha = {ALPHA:.2f} / {k} = {adj_alpha:.4f}(逐筆與逐日區塊各自取這個分位數,PASS 門檻因此更嚴,"
             "owner 2026-11-10)。PASS 之後單日占比與樣本外段(最後 1/3 交易日)平均仍須為正。"
             "登記日之前的資料不算該變體的樣本。", "",
             f"PASS 需同時滿足:n >= {MIN_N}、校正後 bootstrap 下界(逐筆與逐日區塊取較保守者)> 0、"
             f"交易日 >= {MIN_DAYS}、單日占總損益 <= {MAX_DAY_SHARE:.0%}。單筆損益 = realized - fees,"
             "只計 live exit 變體,live 與影子都算。壓力部位:以 $1,000 風險預算與權利金停損反推口數(僅報告)。", ""]
    if not rs:
        return "\n".join(lines + ["(尚無已出場部位)"] + _spread_lines(con))
    lines += ["| 來源 | DTE | n | 天數 | live/影子 | 平均 | 下界(逐筆/逐日) | 總損益 | 單日占比 | 後段(最後 1/3 交易日)平均 | 壓力部位損益 | 判定 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rs:
        lines.append(f"| {r['source']} | {r['dte_label'] or '全部'} | {r['n']} | {r['days']} | {r['live']}/{r['shadow']} "
                     f"| {_f(r['mean'])} | {_f(r['lb'])} ({_f(r['lb_trade'])}/{_f(r['lb_day'])}) | {_f(r['total'])} "
                     f"| {_f(r['day_share'], '{:.0%}')} | {_f(r['oos_mean'])} | {_f(r['stressed_total'])} | {r['verdict']} |")
    return "\n".join(lines + _spread_lines(con))


def _spread_lines(con) -> list[str]:
    """vwap_reclaim debit-spread shadow variant (options/spread_variant.py), P&L after fees."""
    try:
        rs = sv.summary(con)
    except sqlite3.OperationalError:  # table not created yet (older database)
        return []
    if not rs:
        return []
    return ["", f"{sv.VARIANT}(影子,扣費後):" + ";".join(
        f"{r['dte_label']} n={r['n']} 天數={r['days']} 總損益 {r['pnl']:+.0f}" for r in rs)]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="setup_gate", description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--by-dte", action="store_true", help="split each source by dte_label")
    a = ap.parse_args(argv[1:])
    con = sqlite3.connect(f"file:{Path(a.db).resolve()}?mode=ro", uri=True)
    try:
        print(report(con, a.by_dte))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
