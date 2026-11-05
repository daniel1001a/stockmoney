"""Monthly exit check: ``python -m stockmoney.desk.learn.exit_check [--db] [--days 30]``.

EDGE_PLAN 7.4 item 6 (the take-profit cap trap): does the take-profit exit
cap the big winners? Read-only; nothing in risk, veto, execution or the
options engine reads this module, and it changes no parameter.

Scope: EXITED options_positions rows (live and shadow) whose day falls in the
last ``days`` calendar days, exit_reason not in UNKNOWN_PNL (P&L unknown).
Grouped per source x exit_variant x exit_reason: n, mean P&L, total, where
P&L = realized_usd - fees_usd.

Comparison per source x exit_variant: mean winner (P&L > 0) of take-profit
exits (final reason take_profit, or breakeven_stop: variant A's remainder
after the partial take profit) versus trailing / time exits (RUNNER_REASONS, exit reasons from
options/engine.py exit decisions). Flag "停利可能封住大贏家" when the runner
mean winner > CAP_RATIO x the take-profit mean winner and both have
n >= MIN_WINNERS winners.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path("data/desk/desk.sqlite")
UNKNOWN_PNL = ("broker_flat_unreconciled", "manual_resolved")  # options/report.py UNKNOWN_PNL
# take_profit sells only part (take_profit_fraction); in variant A the rest then
# stops at cost, so a take-profit position usually ends as breakeven_stop, which
# options/engine.py _exit_decision only returns after tp_done
TAKE_PROFIT_REASONS = ("take_profit", "breakeven_stop")
RUNNER_REASONS = ("atr_trail", "time_stop", "force_flat")  # trailing / ATR / time exits
CAP_RATIO = 1.5
MIN_WINNERS = 10
CAP_FLAG = "停利可能封住大贏家"


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def rows(con, days: int = 30, today: str | None = None) -> dict:
    """{"groups": [...per source/variant/reason...], "compare": [...per source/variant...]}."""
    out: dict = {"groups": [], "compare": []}
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='options_positions'").fetchone() is None:
        return out
    end = date.fromisoformat(today) if today else date.today()
    since = (end - timedelta(days=days)).isoformat()
    marks = ",".join("?" * len(UNKNOWN_PNL))
    got = con.execute(
        "SELECT source, COALESCE(exit_variant, '?'), COALESCE(exit_reason, '?'), "
        "COALESCE(realized_usd, 0) - COALESCE(fees_usd, 0) FROM options_positions "
        f"WHERE state='EXITED' AND COALESCE(exit_reason,'') NOT IN ({marks}) AND day > ? AND day <= ?",
        (*UNKNOWN_PNL, since, end.isoformat())).fetchall()
    groups: dict[tuple, list[float]] = {}
    for src, ev, reason, pnl in got:
        groups.setdefault((src, ev, reason), []).append(float(pnl))
    for (src, ev, reason), ps in sorted(groups.items()):
        out["groups"].append({"source": src, "exit_variant": ev, "exit_reason": reason, "n": len(ps),
                              "mean": _mean(ps), "total": sum(ps)})
    for src, ev in sorted({(s, e) for s, e, _ in groups}):
        tp = [p for (s, e, r), ps in groups.items() if (s, e) == (src, ev) and r in TAKE_PROFIT_REASONS
              for p in ps if p > 0]
        run = [p for (s, e, r), ps in groups.items() if (s, e) == (src, ev) and r in RUNNER_REASONS
               for p in ps if p > 0]
        tp_m, run_m = _mean(tp), _mean(run)
        flag = (len(tp) >= MIN_WINNERS and len(run) >= MIN_WINNERS and tp_m is not None and run_m is not None
                and tp_m > 0 and run_m > CAP_RATIO * tp_m)
        out["compare"].append({"source": src, "exit_variant": ev, "tp_n": len(tp), "tp_mean_win": tp_m,
                               "run_n": len(run), "run_mean_win": run_m, "flag": flag})
    return out


def _f(x) -> str:
    return "n/a" if x is None else f"{x:+.0f}"


def report(con, days: int = 30, today: str | None = None) -> str:
    r = rows(con, days, today)
    lines = [f"## 出場檢查(最近 {days} 天,唯讀)", "",
             "單筆損益 = realized - fees,live 與影子都算,損益未知的出場不算。比較停利出場與追蹤/時間出場"
             f"({', '.join(RUNNER_REASONS)})的平均獲利筆;後者平均贏 > {CAP_RATIO} 倍停利平均贏且兩邊各 "
             f">= {MIN_WINNERS} 筆贏單時標示「{CAP_FLAG}」。只是報告,不改任何參數。", ""]
    if not r["groups"]:
        return "\n".join(lines + ["(期間內沒有已出場部位)"])
    lines += ["| 來源 | 出場版本 | 出場原因 | n | 平均 | 合計 |", "|---|---|---|---|---|---|"]
    lines += [f"| {g['source']} | {g['exit_variant']} | {g['exit_reason']} | {g['n']} | {_f(g['mean'])} "
              f"| {_f(g['total'])} |" for g in r["groups"]]
    lines += ["", "| 來源 | 出場版本 | 停利贏單 n | 停利平均贏 | 追蹤/時間贏單 n | 追蹤/時間平均贏 | 判定 |",
              "|---|---|---|---|---|---|---|"]
    for c in r["compare"]:
        verdict = CAP_FLAG if c["flag"] else ("樣本不足" if min(c["tp_n"], c["run_n"]) < MIN_WINNERS else "正常")
        lines.append(f"| {c['source']} | {c['exit_variant']} | {c['tp_n']} | {_f(c['tp_mean_win'])} | {c['run_n']} "
                     f"| {_f(c['run_mean_win'])} | {verdict} |")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="exit_check", description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--days", type=int, default=30)
    a = ap.parse_args(argv[1:])
    con = sqlite3.connect(f"file:{Path(a.db).resolve()}?mode=ro", uri=True)
    try:
        print(report(con, a.days))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
