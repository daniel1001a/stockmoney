"""Weekly process vs outcome lines per playbook (EDGE_PLAN 7.4 item 7).

Read-only, appended to the weekly post-mortem report (postmortem.symptom_report).
Process and outcome are judged separately: a winning week that broke the
rules is a process error.

Window: options_positions EXITED with day in (today - 7 days, today]. Only the
live exit variant (cfg.live_exit) is counted, live and shadow, and a shadow
row whose setup_id and dte_label match a live row is dropped (same event,
as in learn/setup_gate.py).

流程 (process), per source, counts:
  - unknown: exit_reason in UNKNOWN_PNL (the sleeve never saw the true P&L)
  - non-rule exits: exit_reason in NON_RULE (leftover / recovery market sells)
  - errors: options_events at level error or critical in the window whose
    message names one of the source's position_ids that week
"照規則" when all three are zero.
結果 (outcome): n and P&L = realized_usd - fees_usd (unknown exits left out).
"""
from __future__ import annotations

from datetime import date, timedelta

from stockmoney.desk.options.config import OptionsConfig

UNKNOWN_PNL = ("broker_flat_unreconciled", "manual_resolved")  # options/report.py UNKNOWN_PNL
NON_RULE = ("carry_over", "recover_flat")
ERROR_LEVELS = ("error", "critical")


def week_rows(con, today: str, cfg: OptionsConfig | None = None) -> list[dict]:
    cfg = cfg or OptionsConfig()
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='options_positions'").fetchone() is None:
        return []
    lo = (date.fromisoformat(today) - timedelta(days=7)).isoformat()
    got = con.execute(
        "SELECT position_id, setup_id, dte_label, mode, source, exit_reason, "
        "COALESCE(realized_usd, 0) - COALESCE(fees_usd, 0) FROM options_positions WHERE state='EXITED' "
        "AND day > ? AND day <= ? AND COALESCE(exit_variant, ?) = ? ORDER BY source",
        (lo, today, cfg.live_exit, cfg.live_exit)).fetchall()
    live_keys = {(r[1], r[2]) for r in got if r[3] == "live" and r[1]}
    got = [r for r in got if not (r[3] != "live" and (r[1], r[2]) in live_keys)]
    events: list[str] = []
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='options_events'").fetchone():
        marks = ",".join("?" * len(ERROR_LEVELS))
        events = [m or "" for (m,) in con.execute(
            f"SELECT message FROM options_events WHERE level IN ({marks}) AND day > ? AND day <= ?",
            (*ERROR_LEVELS, lo, today))]
    out: dict[str, dict] = {}
    for pid, _, _, _, src, reason, pnl in got:
        o = out.setdefault(src, {"source": src, "unknown": 0, "non_rule": 0, "errors": 0, "n": 0, "pnl": 0.0,
                                 "_pids": []})
        o["_pids"].append(pid)
        if reason in UNKNOWN_PNL:
            o["unknown"] += 1
            continue
        o["non_rule"] += reason in NON_RULE
        o["n"] += 1
        o["pnl"] += float(pnl)
    for o in out.values():
        pids = o.pop("_pids")
        o["errors"] = sum(any(p and p in m for p in pids) for m in events)
    return [out[k] for k in sorted(out)]


def lines(con, today: str) -> list[str]:
    rs = week_rows(con, today)
    if not rs:
        return []
    out = ["", f"### 期權劇本流程與結果(截至 {today} 的 7 天,唯讀)",
           "流程與結果分開記:賺錢但違規算流程錯。"]
    for r in rs:
        bad = r["unknown"] + r["non_rule"] + r["errors"]
        proc = ("照規則" if not bad else
                f"未照規則:損益未知 {r['unknown']}、非規則出場 {r['non_rule']}、錯誤事件 {r['errors']}")
        out += [f"- {r['source']}", f"  - 流程:{proc}", f"  - 結果:n={r['n']},損益 {r['pnl']:+.0f}",
                "  - 一件要改的事:(週五覆盤填寫)"]
    return out
