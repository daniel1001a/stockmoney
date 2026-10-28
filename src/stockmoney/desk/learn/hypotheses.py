"""Weekly hypothesis pipeline: post-mortem symptoms -> recorded candidate hypotheses.

``postmortem.symptom_report`` already decides what is a candidate (a non-S0
symptom in the top two by loss for two consecutive weeks). This module persists
those candidates in ``weekly_hypotheses`` so they can be queried, and renders the
weekly markdown. It only RECORDS: no rule, parameter or setting is changed.
A candidate stays 'candidate' until the owner takes it through the normal path
(backtest -> shadow -> owner approval, docs/desk/EDGE_PLAN.md 2.2).

Runs automatically in the Friday learn job and on demand:
``scripts/desk_learn.py hypotheses [--date YYYY-MM-DD] [--no-record] [--list]``.
Re-running inside the same ISO week updates that week's rows instead of adding more.
"""
from __future__ import annotations

import pandas as pd

from stockmoney.desk import store
from stockmoney.desk.learn import postmortem

SCHEMA = """CREATE TABLE IF NOT EXISTS weekly_hypotheses (
  week TEXT NOT NULL, symptom TEXT NOT NULL, label TEXT, week_end TEXT,
  loss_this REAL, loss_prev REAL, n_this INTEGER, n_prev INTEGER,
  text TEXT, status TEXT DEFAULT 'candidate', created_at TEXT, updated_at TEXT,
  PRIMARY KEY (week, symptom))"""


def ensure_schema(con) -> None:
    con.execute(SCHEMA)


def iso_week(day: str) -> str:
    y, w, _ = pd.Timestamp(day).isocalendar()
    return f"{y}-W{w:02d}"


def build(con, today: str) -> list[dict]:
    """Candidate hypotheses as of ``today`` (pure read)."""
    stats = postmortem.week_stats(con, today, 2)
    out = []
    for code in postmortem.candidate_symptoms(stats)[:postmortem.MAX_HYPOTHESES]:
        out.append({"week": iso_week(today), "symptom": code, "label": postmortem.LABEL[code], "week_end": today,
                    "loss_this": stats[0]["loss"][code], "loss_prev": stats[1]["loss"][code],
                    "n_this": stats[0]["cnt"][code], "n_prev": stats[1]["cnt"][code],
                    "text": postmortem.hypothesis_text(code)})
    return out


def record(con, rows: list[dict]) -> dict:
    """Upsert by (week, symptom); a repeat run keeps created_at and status. Does not commit."""
    ensure_schema(con)
    new = upd = 0
    now = store.now()
    for r in rows:
        old = con.execute("SELECT 1 FROM weekly_hypotheses WHERE week=? AND symptom=?", (r["week"], r["symptom"])).fetchone()
        if old is None:
            con.execute("INSERT INTO weekly_hypotheses (week, symptom, label, week_end, loss_this, loss_prev, n_this, n_prev, "
                        "text, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,'candidate',?,?)",
                        (r["week"], r["symptom"], r["label"], r["week_end"], r["loss_this"], r["loss_prev"],
                         r["n_this"], r["n_prev"], r["text"], now, now))
            new += 1
        else:
            con.execute("UPDATE weekly_hypotheses SET week_end=?, loss_this=?, loss_prev=?, n_this=?, n_prev=?, text=?, "
                        "updated_at=? WHERE week=? AND symptom=?",
                        (r["week_end"], r["loss_this"], r["loss_prev"], r["n_this"], r["n_prev"], r["text"], now,
                         r["week"], r["symptom"]))
            upd += 1
    return {"new": new, "updated": upd}


def history(con, limit: int = 12) -> list[dict]:
    ensure_schema(con)
    return [dict(r) for r in con.execute("SELECT * FROM weekly_hypotheses ORDER BY week DESC, symptom LIMIT ?", (limit,))]


def render(con, today: str, rows: list[dict], include_weekly: bool = True) -> str:
    lines = [f"# 驗屍卡每週假設({iso_week(today)},截至 {today})", ""]
    if include_weekly:
        lines += [postmortem.symptom_report(con, today)[0], ""]
    lines += ["## 本週候選假設", ""]
    if rows:
        lines += [f"- {r['text']} 本週 {r['symptom']} 虧損 ${r['loss_this']:,.0f}({r['n_this']} 筆),"
                  f"上週 ${r['loss_prev']:,.0f}({r['n_prev']} 筆)。" for r in rows]
        lines += ["", "以上只是紀錄:不改任何參數。下一步是回測與影子帳戶驗證,過關後才由你決定是否採用。"]
    else:
        lines.append("沒有候選:沒有任何非 S0 症狀連續兩週排在虧損前兩名。")
    past = [h for h in history(con) if h["week"] != iso_week(today)]
    if past:
        lines += ["", "## 歷史候選", *[f"- {h['week']} {h['symptom']} {h['label']}({h['status']})" for h in past]]
    return "\n".join(lines)


def run(con, today: str, record_rows: bool = True, include_weekly: bool = True) -> str:
    """Build, optionally persist (caller commits), and render the weekly report."""
    rows = build(con, today)
    if record_rows:
        record(con, rows)
    return render(con, today, rows, include_weekly)
