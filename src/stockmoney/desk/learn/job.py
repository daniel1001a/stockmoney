"""The scheduled learning job: ``python -m stockmoney.desk.learn.job``.

Runs after postclose (17:15 ET). Reads the ledger and price cache, writes only
learning tables and a report file. Own lock, own failure handling: a crash
here is logged and never touches an order or the trading lock.

Steps (each isolated, a failure in one does not stop the others):
  1. post-mortem cards for closed trades
  2. advance shadow books
  3. score due event chains, judge due invalidation conditions, label
     Muse/Grok source independence
  4. allocator recommendation (Fridays or when none logged this week)
  5. report -> data/desk/learn/<date>.md and learn_reports; Friday push summary
"""
from __future__ import annotations

import fcntl
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from stockmoney.desk import data, notify, store
from stockmoney.desk.indicators import compute
from stockmoney.desk.learn import (allocator, chains, corroboration, earnings, invalidation, postmortem, shadow,
                                   tables)

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
LOCK = Path("data/desk/learn.lock")
OUT_DIR = Path("data/desk/learn")


def _through(bars: data.Bars, now: datetime) -> pd.Timestamp:
    """Last COMPLETE session: today's bar only counts after 16:05 ET."""
    et = now.astimezone(ET)
    d = bars.dates
    done = d[d < pd.Timestamp(et.date())]
    if len(d) and d[-1] == pd.Timestamp(et.date()) and (et.hour, et.minute) >= (16, 5):
        return d[-1]
    return done[-1]


def _earnings_watch(con, day) -> str:
    """Held positions and already-planned buys that report within 7 days.
    Read-only: this only informs, it never changes a plan."""
    held = [r["symbol"] for r in con.execute("SELECT symbol FROM sleeve_positions").fetchall()] \
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='sleeve_positions'").fetchone() else []
    planned = [r["symbol"] for r in con.execute(
        "SELECT symbol FROM planned_orders WHERE side='buy' AND status IN ('planned','submitted')").fetchall()] \
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='planned_orders'").fetchone() else []
    up = earnings.upcoming(con, held + planned, day, 7)
    if not up:
        return "## 財報提醒\n\n持倉與已規劃的買單,7 天內沒有已知的財報日(資料不足的日子不會被當成沒有)。"
    lines = ["## 財報提醒(7 天內)", ""]
    for s, d in sorted(up.items(), key=lambda x: x[1]):
        kind = "持倉" if norm_in(s, held) else "已規劃買進"
        lines.append(f"- {s}:{d}({kind})")
    return "\n".join(lines)


def norm_in(sym: str, group: list[str]) -> bool:
    return sym in {earnings.norm(g) for g in group}


def run(con, now: datetime, bars: data.Bars | None = None, shadow_start: str | None = None,
        with_allocator: bool | None = None) -> str:
    tables.ensure_schema(con)
    bars = bars or data.to_bars(data.load())
    through = _through(bars, now)
    bars = bars.upto(through)
    ind = compute(bars)
    day = str(through.date())
    parts, problems = [f"# Desk 學習日報 {day}", ""], []

    def guarded(name: str, fn):
        try:
            return fn()
        except Exception as e:  # one step failing must not hide the others
            log.exception("learn step %s failed", name)
            problems.append(f"{name} 失敗:{type(e).__name__}: {e}")
            con.rollback()
            return None

    er = guarded("earnings", lambda: earnings.refresh(con, through.date()))
    con.commit()
    if er is not None:
        parts.append(f"財報日:掃描 {er['scanned']} 天,失敗 {er['failed']} 天,略過 {er['skipped']} 天。")
    pm = guarded("postmortem", lambda: postmortem.update_cards(con, bars, ind))
    con.commit()
    if pm is not None:
        parts.append(f"驗屍卡:新增 {pm['new']},更新 {pm['updated']}。")
    rep, hyps = guarded("symptom_report", lambda: postmortem.symptom_report(con, day)) or ("", [])
    sh = guarded("shadow", lambda: shadow.step(con, bars, ind, through, start=shadow_start))
    con.commit()
    ig = guarded("grok_ingest", lambda: chains.ingest_grok(con))
    con.commit()
    if ig and (ig["added"] or ig["skipped"]):
        parts.append(f"Grok 事件鏈:新增 {ig['added']},略過 {ig['skipped']}。")
    sc = guarded("chains", lambda: chains.score_due(con, bars, through))
    con.commit()
    if sc:
        parts.append(f"事件鏈新評分 {sc} 筆。")
    iv = guarded("invalidation", lambda: invalidation.judge_due(con, bars, through))
    con.commit()
    if iv:
        parts.append(f"事件鏈推翻條件新判定 {iv} 筆。")
    co = guarded("corroboration", lambda: corroboration.refresh(con))
    con.commit()
    if co and co["added"]:
        parts.append(f"Muse 與 Grok 同標的新增 {co['added']} 組來源比對。")

    friday = through.weekday() == 4
    do_alloc = friday if with_allocator is None else with_allocator
    if do_alloc:
        def _alloc():
            runs = allocator.sleeve_runs(bars, ind)
            wf = allocator.walk_forward(runs, ind)
            rec = allocator.recommend(runs, ind, through)
            allocator.log_recommendation(con, rec)
            return allocator.report_text(wf, rec)
        alloc_text = guarded("allocator", _alloc)
        con.commit()
    else:
        alloc_text = None

    watch = guarded("earnings_watch", lambda: _earnings_watch(con, through.date()))
    if watch:
        parts += ["", watch]
    parts += ["", rep, "", guarded("shadow_report", lambda: shadow.report(con)) or "", "",
              guarded("chain_report", lambda: chains.report(con)) or ""]
    if alloc_text:
        parts += ["", alloc_text]
    if problems:
        parts += ["", "## 本次失敗的步驟", *[f"- {p}" for p in problems]]
    body = "\n".join(parts)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{day}.md").write_text(body + "\n")
    con.execute("INSERT OR REPLACE INTO learn_reports VALUES (?,?,?)", (day, body, store.now()))
    con.commit()
    if problems:
        notify.send(f"Desk 學習任務有失敗步驟 {day}", "\n".join(problems)[:1000], notify.WARN)
    elif friday and (hyps or alloc_text):
        notify.send(f"Desk 學習週報 {day}", "\n".join(hyps + ([alloc_text.split("\n")[-6]] if alloc_text else []))[:900],
                    notify.INFO)
    return body


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.error("another learn job is running")
        return 1
    try:
        with store.connect() as con:
            print(run(con, datetime.now(timezone.utc)))
        return 0
    except Exception as e:
        log.exception("learn job failed")
        notify.send("Desk 學習任務失敗", f"{type(e).__name__}: {e}", notify.WARN)
        return 1
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


if __name__ == "__main__":
    sys.exit(main())
