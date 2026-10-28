"""Counterfactual log for stock entries the desk blocked before submitting.

Read-only: this reads ``planned_orders`` rows with status 'blocked' (premarket
writes the block reason into ``note``) and the price cache, and writes only
``blocked_counterfactual``. Nothing in bridge, risk or execution code reads it,
and it never changes whether an order is blocked.

Convention (same as the shadow books, broker.Account.process_open):
- entry at the first session open after ``plan_date`` (the premarket where the
  order was blocked), filled at open * (1 + SLIPPAGE)
- ret_N = close of the N-th session after entry / entry_px - 1
- excess_N = ret_N - SPY's return from the entry-day open to the same close
- stop honored: from the entry day on, the first day whose low <= stop exits
  at min(open, stop) * (1 - SLIPPAGE); stop_ret_N is that exit's return when it
  happened by day N, else ret_N
Fields stay NULL until enough bars exist: status 'pending' (< 5 sessions after
entry), 'partial' (5..19), 'done' (20).

legacy_only: since the Muse downgrade (2026-10-21) premarket also logs buys
it let through that the old Muse rule would have blocked
(``policy.record_legacy``). Those rows get legacy_only=1 and are reported in
their own groups, so the old rule's blocks can be scored against what the new
rule did. A logged buy that was blocked anyway (for example by veto.check) is
already a blocked row and is not counted twice.

first_block: repeated ALERT blocks of one event span several plans. A block is
a repeat when the same symbol was blocked at most FLAG_DAYS calendar days
earlier (chained), so a run of daily re-blocks counts as one event.
"""
from __future__ import annotations

import re
from datetime import date

import pandas as pd

from stockmoney.desk import store
from stockmoney.desk.bridge.policy import FLAG_DAYS
from stockmoney.desk.broker import SLIPPAGE
from stockmoney.desk.data import Bars
from stockmoney.desk.universe import MARKET

HORIZONS = (5, 20)
_SEVERITY = ("info", "elevated", "critical")
_ALERT = re.compile(r"Muse ALERT (info|elevated|critical):")
_CLASS_LABEL = {"muse_alert": "Muse ALERT", "muse_risk_flags": "Muse 風險旗標(RESEARCH_REPLY)",
                "muse_other": "Muse 其他", "other": "非 Muse(對帳/風控/掛單衝突等)"}
COLS = ("client_order_id", "plan_date", "symbol", "strategy", "block_class", "severity", "note", "ref_price",
        "stop", "entry_day", "entry_px", "ret_5", "ret_20", "excess_5", "excess_20", "stop_ret_5", "stop_ret_20",
        "stop_hit", "first_block", "legacy_only", "status", "updated_at")


def classify(note: str | None) -> tuple[str, str | None]:
    """(block_class, severity). A note can join several reasons with " | ";
    risk flags win over alerts (a deliberate negative read, not just news),
    severity is the highest ALERT severity present, None without an ALERT."""
    note = note or ""
    sevs = _ALERT.findall(note)
    severity = max(sevs, key=_SEVERITY.index) if sevs else None
    if "Muse risk flags:" in note:
        return "muse_risk_flags", severity
    if sevs:
        return "muse_alert", severity
    if "Muse" in note:
        return "muse_other", severity
    return "other", severity


def _sym(symbol: str) -> str:
    return symbol.strip().upper().replace(".", "-").replace("/", "-")


def _num(x) -> float | None:
    return None if x is None or pd.isna(x) else float(x)


def outcome(bars: Bars, symbol: str, plan_date: str, stop: float | None = None) -> dict:
    """Simulated result of buying ``symbol`` at the first open after
    ``plan_date``. Only bars present in ``bars`` are used (pass bars.upto(through))."""
    out = {"entry_day": None, "entry_px": None, "stop_hit": None, "status": "pending",
           **{f"{k}_{h}": None for k in ("ret", "excess", "stop_ret") for h in HORIZONS}}
    sym = _sym(symbol)
    if sym not in bars.close.columns or MARKET not in bars.close.columns:
        return out
    after = bars.dates[bars.dates > pd.Timestamp(plan_date)]
    if not len(after):
        return out
    e = after[0]
    op = _num(bars.open.at[e, sym])
    spy0 = _num(bars.open.at[e, MARKET])
    if op is None or spy0 is None:
        return out
    entry = op * (1 + SLIPPAGE)
    out["entry_day"], out["entry_px"] = str(e.date()), entry
    days = after[:max(HORIZONS) + 1]  # entry day + 20 sessions
    exit_k, exit_px = None, None
    if stop is not None:
        for k, t in enumerate(days):
            low, o = _num(bars.low.at[t, sym]), _num(bars.open.at[t, sym])
            if low is None or low > stop:
                continue
            exit_k, exit_px = k, (min(o, stop) if o is not None else stop) * (1 - SLIPPAGE)
            break
    reached = 0
    for h in HORIZONS:
        if h >= len(days):
            break
        t = days[h]
        c, s1 = _num(bars.close.at[t, sym]), _num(bars.close.at[t, MARKET])
        if c is None or s1 is None:
            break
        r = c / entry - 1
        out[f"ret_{h}"], out[f"excess_{h}"] = r, r - (s1 / spy0 - 1)
        if stop is not None:
            out[f"stop_ret_{h}"] = exit_px / entry - 1 if exit_k is not None and exit_k <= h else r
        reached = h
    if stop is not None:
        if exit_k is not None:
            out["stop_hit"] = 1
        elif reached == max(HORIZONS):
            out["stop_hit"] = 0
    out["status"] = "done" if reached == max(HORIZONS) else "partial" if reached else "pending"
    return out


def _first_blocks(rows) -> dict[str, int]:
    last: dict[str, date] = {}
    first: dict[str, int] = {}
    for r in sorted(rows, key=lambda r: (r["plan_date"], r["client_order_id"])):
        d, s = date.fromisoformat(r["plan_date"]), _sym(r["symbol"])
        first[r["client_order_id"]] = int(s not in last or (d - last[s]).days > FLAG_DAYS)
        last[s] = d
    return first


def refresh(con, bars: Bars, through: pd.Timestamp) -> dict:
    """Upsert one row per blocked stock buy. Idempotent: an unchanged row is
    not rewritten. Returns {'added', 'updated', 'rows'}."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='planned_orders'").fetchone() is None:
        return {"added": 0, "updated": 0, "rows": 0}
    bars = bars.upto(through)
    rows = con.execute("SELECT client_order_id, plan_date, symbol, strategy, stop, ref_price, note "
                       "FROM planned_orders WHERE status='blocked' AND side='buy' AND asset_class='us_equity'"
                       ).fetchall()
    from stockmoney.desk.bridge.policy import legacy_rows
    blocked_ids = {r["client_order_id"] for r in rows}
    plans = {r["client_order_id"]: r for r in con.execute(
        "SELECT client_order_id, strategy, stop, ref_price FROM planned_orders").fetchall()}
    legacy = [{"client_order_id": r["client_order_id"], "plan_date": r["plan_date"], "symbol": r["symbol"],
               "strategy": plans[r["client_order_id"]]["strategy"], "stop": plans[r["client_order_id"]]["stop"],
               "ref_price": plans[r["client_order_id"]]["ref_price"], "note": r["reason"], "legacy_only": 1}
              for r in legacy_rows(con)
              if r["client_order_id"] not in blocked_ids and r["client_order_id"] in plans]
    rows = [{**dict(r), "legacy_only": 0} for r in rows]
    first = {**_first_blocks(rows), **_first_blocks(legacy)}
    rows = rows + legacy
    have = {r["client_order_id"]: tuple(r[c] for c in COLS[:-1])
            for r in con.execute("SELECT * FROM blocked_counterfactual").fetchall()}
    added = updated = 0
    for r in rows:
        cid = r["client_order_id"]
        block_class, severity = classify(r["note"])
        o = outcome(bars, r["symbol"], r["plan_date"], _num(r["stop"]))
        rec = {"client_order_id": cid, "plan_date": r["plan_date"], "symbol": r["symbol"], "strategy": r["strategy"],
               "block_class": block_class, "severity": severity, "note": r["note"], "ref_price": r["ref_price"],
               "stop": r["stop"], **o, "first_block": first[cid], "legacy_only": r["legacy_only"]}
        vals = tuple(rec[c] for c in COLS[:-1])
        if have.get(cid) == vals:
            continue
        con.execute(f"INSERT OR REPLACE INTO blocked_counterfactual ({','.join(COLS)}) "
                    f"VALUES ({','.join('?' * len(COLS))})", (*vals, store.now()))
        if cid in have:
            updated += 1
        else:
            added += 1
    return {"added": added, "updated": updated, "rows": len(rows)}


def _pct(v) -> str:
    return "n/a" if v is None or pd.isna(v) else f"{v:+.2%}"


def report(con) -> str:
    head = ["## 被擋買單的反事實(未成交,僅供參考)", ""]
    df = pd.read_sql_query("SELECT * FROM blocked_counterfactual", con)
    if df.empty:
        return "\n".join(head + ["尚無被擋下的股票買單。"])
    lines = head + ["假設照計畫在下一個開盤買進;報酬算到第 5 / 20 個交易日收盤,超額為減去 SPY 同期報酬。"
                    "平均值只用每個事件的第一次擋單計算(同一標的 3 天內重複擋單算同一事件)。"
                    "停損報酬假設計畫停損價被執行。這些都不是實際成交。", ""]
    df["group"] = df.apply(lambda r: f"{_CLASS_LABEL.get(r.block_class, r.block_class)}"
                                     + (f" {r.severity}" if r.block_class == "muse_alert" and r.severity else "")
                                     + ("(舊規則會擋,新規則放行)" if r.get("legacy_only") == 1 else ""),
                           axis=1)
    for g, d in df.groupby("group", sort=True):
        ev = d[d.first_block == 1]
        # the rate uses only events whose 20-day window is complete; an early
        # hit on an open window would bias it upward, so it is counted apart
        done = ev[ev.status == "done"]
        early = int(((ev.status != "done") & (ev.stop_hit == 1)).sum())
        lines.append(
            f"- {g}:{len(d)} 筆,獨立事件 {len(ev)} 件;"
            f"5 日平均 {_pct(ev.ret_5.mean())}(超額 {_pct(ev.excess_5.mean())},{ev.ret_5.count()} 件),"
            f"20 日平均 {_pct(ev.ret_20.mean())}(超額 {_pct(ev.excess_20.mean())},{ev.ret_20.count()} 件);"
            f"守停損 5 日平均 {_pct(ev.stop_ret_5.mean())}、20 日 {_pct(ev.stop_ret_20.mean())};"
            f"停損觸發率(已滿 20 日)"
            + (f"{int(done.stop_hit.sum())}/{done.stop_hit.count()}" if done.stop_hit.count() else "n/a")
            + (f",未滿 20 日已觸發 {early} 件" if early else "") + "。")
    return "\n".join(lines)
