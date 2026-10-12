"""Judge the structured invalidation conditions of event chains.

A condition (see chains.PriceInvalidation / DataInvalidation) is judged on or
after its ``by_date`` using only the daily closes we stored, with the same
convention as chain scoring: the window starts AFTER the last close that was
public when the chain was written (chains.base_session, 16:05 ET), and today's
bar only counts once complete (the caller passes ``through``).

Verdicts:
  invalidated  a close inside the window crossed the level (definitive)
  held         every close in the window is on the right side of the level
  no_data      cannot be judged: no price series, missing closes, no sessions
               in the window, or a data_not_by condition (code never judges
               those true or false)

No_data is not final for price conditions: a later run re-judges it once the
closes exist. held / invalidated and data_not_by rows are never rewritten.
Read-only wrt execution: this module only reads chains and prices and writes
its own table.
"""
from __future__ import annotations

import json
from datetime import date

import pandas as pd

from stockmoney.desk import store
from stockmoney.desk.data import Bars
from stockmoney.desk.learn.chains import PRICE_KINDS, base_session

VERDICTS = ("held", "invalidated", "no_data")
# a by_date on a weekend or holiday: how far before it the last stored session may sit
_GAP_DAYS = 4


def _sym(t: str) -> str:
    return t.replace(".", "-")


def judge_condition(cond: dict, created_at: str, bars: Bars, through: pd.Timestamp) -> tuple[str, str]:
    """(verdict, detail) for one stored condition. The caller has checked it is due."""
    if cond["kind"] not in PRICE_KINDS:
        return "no_data", "not mechanically checkable"
    sessions = bars.dates[bars.dates <= through]
    base = base_session(created_at, sessions)
    if base is None:
        return "no_data", "no stored close before the chain was written"
    sym = _sym(cond["symbol"])
    if sym not in bars.close.columns:
        return "no_data", f"no price series for {cond['symbol']}"
    by = pd.Timestamp(cond["by_date"])
    window = sessions[(sessions > base) & (sessions <= by)]
    if len(window) == 0:
        return "no_data", "no complete session between the chain and by_date"
    if (by - window[-1]).days > _GAP_DAYS:
        return "no_data", f"stored closes end {window[-1].date()}, before by_date"
    closes = bars.close.loc[window, sym]
    level = float(cond["level"])
    bad = closes < level if cond["kind"] == "close_below" else closes > level
    if bad.any():
        d = bad[bad].index[0]
        return "invalidated", f"{d.date()} close {float(closes[d]):.2f} {'<' if cond['kind'] == 'close_below' else '>'} {level:g}"
    if closes.isna().any():
        return "no_data", f"{int(closes.isna().sum())} missing close(s) in the window"
    return "held", f"{len(window)} closes {float(closes.min()):.2f}..{float(closes.max()):.2f} vs {level:g}"


def judge_due(con, bars: Bars, through: pd.Timestamp) -> int:
    """Judge every condition whose by_date has arrived. Returns rows written."""
    n = 0
    today: date = through.date()
    for r in con.execute("SELECT chain_id, created_at, invalidations_json FROM chains "
                         "WHERE invalidations_json IS NOT NULL AND invalidations_json != '[]'").fetchall():
        try:
            conds = json.loads(r["invalidations_json"])
        except ValueError:
            continue
        for i, cond in enumerate(conds):
            try:
                if date.fromisoformat(cond["by_date"]) > today:
                    continue
                prev = con.execute("SELECT verdict FROM chain_invalidations WHERE chain_id=? AND idx=?",
                                   (r["chain_id"], i)).fetchone()
                if prev and (prev["verdict"] != "no_data" or cond["kind"] not in PRICE_KINDS):
                    continue
                verdict, detail = judge_condition(cond, r["created_at"], bars, through)
                if prev and verdict == "no_data":
                    continue  # still unjudgeable; keep the first row and its date
                desc = cond.get("symbol") or cond.get("description", "")
                con.execute("INSERT OR REPLACE INTO chain_invalidations VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (r["chain_id"], i, cond["kind"], desc, cond.get("level"), cond["by_date"],
                             verdict, detail, str(today), store.now()))
                n += 1
            except (KeyError, ValueError, TypeError):
                continue
    return n


def counts(con) -> dict[str, int]:
    out = {v: 0 for v in VERDICTS}
    for r in con.execute("SELECT verdict, COUNT(*) AS n FROM chain_invalidations GROUP BY verdict").fetchall():
        out[r["verdict"]] = r["n"]
    due_open = 0
    for r in con.execute("SELECT chain_id, invalidations_json FROM chains WHERE invalidations_json != '[]'").fetchall():
        due_open += len(json.loads(r["invalidations_json"]))
    out["pending"] = max(due_open - sum(out[v] for v in VERDICTS), 0)
    return out


def report(con) -> str:
    c = counts(con)
    if not any(c.values()):
        return ""
    return (f"推翻條件:守住 {c['held']}、已推翻 {c['invalidated']}、無法判定 {c['no_data']}、未到期 {c['pending']}。"
            "(data_not_by 一律是無法判定,不會被當成推翻。)")
