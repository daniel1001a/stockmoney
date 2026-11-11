"""Read-only daily gate report (EVOLUTION_PLAN section 6 item 2f; Python
version of the reference query /tmp/sm_daily_gate.sql).

Per source/day: trade count, net P&L, the running cumulative P&L, that
cumulative total with the source's single best day excluded (a crude
"how dependent on one lucky day is this" check), and the count of
first-10-minute "invalidation" exits. All of it comes from the exact same
scope learn.setup_gate's 5.1 gate uses (``setup_gate.scope_rows``: EXITED
positions with known P&L, the live exit variant only, the registered-date
exclusion, live/shadow-twin dedup) -- this script does not re-derive or
loosen that scope, it only regroups it by day instead of by source. The
per-source lower bound reuses ``setup_gate.metrics()`` verbatim (same
bootstrap, same Bonferroni/Holm-adjusted alpha, same K from
``learn.variants.family_size()``) so the two reports can never disagree.

Read-only: the DB is opened with ``mode=ro`` (SQLite raises on any write
attempt against such a connection), and this module performs no INSERT /
UPDATE / DELETE / schema change of its own. Not imported by anything in
options/, execution/, risk/, bridge/policy.py, run.py, veto.py, engine.py,
live.py, or intraday.py -- it is a reporting-only leaf.

Usage:
    uv run python scripts/desk_daily_gate_report.py [--db PATH]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from stockmoney.desk.learn import setup_gate as sg
from stockmoney.desk.learn import variants


def _f(x: float | None) -> str:
    return "-" if x is None else f"{x:+.2f}"


def _is_first10_invalidation(row) -> bool:
    """True if this row's exit was an 'invalidation' within 10 minutes of
    entry -- the one derived signal the reference SQL adds beyond the gate's
    own scope. Missing/unparseable timestamps never count (fail closed).
    ``row`` is a sqlite3.Row (from setup_gate.scope_rows), not a dict."""
    if row["exit_reason"] != "invalidation":
        return False
    et, xt = row["entry_time"], row["exit_time"]
    if not et or not xt:
        return False
    try:
        e, x = datetime.fromisoformat(et), datetime.fromisoformat(xt)
    except ValueError:
        return False
    return (x - e).total_seconds() <= 600.0


def daily_rows(con: sqlite3.Connection, cfg=None) -> list[dict]:
    """One row per (day, source) in setup_gate's scope, sorted by source then
    day: n, daily_net_pnl, cumulative_pnl (running sum within the source),
    total_excluding_best_day (cumulative less the source's best single day
    seen so far), first_10m_invalidation_count."""
    cfg = cfg or sg.OptionsConfig()
    per_day: dict[tuple[str, str], dict] = {}
    for r in sg.scope_rows(con, cfg):
        key = (r["day"], r["source"])
        d = per_day.setdefault(key, {"day": r["day"], "source": r["source"], "n": 0, "pnl": 0.0, "inval10": 0})
        d["n"] += 1
        d["pnl"] += (r["realized_usd"] or 0.0) - (r["fees_usd"] or 0.0)
        if _is_first10_invalidation(r):
            d["inval10"] += 1

    out: list[dict] = []
    running: dict[str, float] = {}
    best: dict[str, float] = {}
    for key in sorted(per_day, key=lambda k: (k[1], k[0])):
        d = per_day[key]
        source = d["source"]
        running[source] = running.get(source, 0.0) + d["pnl"]
        best[source] = max(best.get(source, d["pnl"]), d["pnl"])
        out.append({
            "day": d["day"], "source": source, "n": d["n"],
            "daily_net_pnl": round(d["pnl"], 2),
            "cumulative_pnl": round(running[source], 2),
            "total_excluding_best_day": round(running[source] - best[source], 2),
            "first_10m_invalidation_count": d["inval10"],
        })
    return out


def report(con: sqlite3.Connection) -> str:
    cfg = sg.OptionsConfig()
    k = variants.family_size()
    lines = [
        "## 每日逐策略關卡報告(唯讀)",
        "",
        "範圍與 learn.setup_gate 的 5.1 關卡完全相同(EXITED、已知損益、live exit 變體、"
        "登記日排除、live/影子雙胞胎去重),只是改成逐日而非只看來源加總。",
        "",
        "| 日期 | 來源 | 當日筆數 | 當日淨損益 | 累計損益 | 排除最佳單日後累計 | 10分內失效次數 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in daily_rows(con, cfg):
        lines.append(
            f"| {r['day']} | {r['source']} | {r['n']} | {_f(r['daily_net_pnl'])} | "
            f"{_f(r['cumulative_pnl'])} | {_f(r['total_excluding_best_day'])} | "
            f"{r['first_10m_invalidation_count']} |"
        )
    lines += [
        "",
        f"來源加總(沿用 setup_gate.metrics(),K = {k},下界已依 Bonferroni/Holm 校正):",
        "",
        "| 來源 | n | 下界 | 總損益 | 判定 |",
        "|---|---|---|---|---|",
    ]
    for r in sg.rows(con, False, cfg):
        lines.append(f"| {r['source']} | {r['n']} | {sg._f(r['lb'])} | {sg._f(r['total'])} | {r['verdict']} |")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="desk_daily_gate_report",
        description="Read-only per-day/source breakdown of the setup_gate scope (never writes to the DB).",
    )
    ap.add_argument("--db", default=str(sg.DB_PATH), help=f"SQLite path (default: {sg.DB_PATH})")
    args = ap.parse_args(argv[1:])

    con = sqlite3.connect(f"file:{Path(args.db).resolve()}?mode=ro", uri=True)
    try:
        print(report(con))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
