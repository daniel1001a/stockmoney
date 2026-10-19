"""Ledger vs broker reconciliation. The broker is the source of truth; any
mismatch means the orchestrator must halt new entries and notify a human.
Exits and protective stops keep running for every symbol that reconciles.

Daily summary (``daily``): the broker's paper fills and open stop orders against
what the desk planned (slippage vs the plan reference price, planned orders that
did not fill, ledger stop vs the broker's stop order). It is a READ-ONLY report:
it never halts, never touches an order, and the postclose caller wraps it so a
failure here cannot break the run. Per-day rows go to ``reconcile_daily``, which
also carries the "consecutive zero-difference days" count.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from stockmoney.desk import store
from stockmoney.desk.execution.ledger import Ledger, is_crypto_symbol, norm
from stockmoney.desk.execution.types import AccountSnapshot, AssetClass, BrokerOrder, OrderType, Side

CRYPTO_QTY_TOL = 1e-6
EQUITY_QTY_TOL = 1e-9  # "exact": only absorbs float representation noise


def mismatches(ledger: Ledger | sqlite3.Connection, snapshot: AccountSnapshot) -> dict[str, str]:
    """{normalized symbol: human-readable problem}; empty when the ledger and
    the broker agree on every symbol. Crypto dust below CRYPTO_QTY_TOL is
    ignored on either side, including when only one side holds it."""
    if not isinstance(ledger, Ledger):
        ledger = Ledger(ledger)
    mine = {norm(s): p for s, p in ledger.positions().items()}
    theirs = {norm(p.symbol): p for p in snapshot.positions if abs(p.qty) > 0}
    out: dict[str, str] = {}
    for key in sorted(set(mine) | set(theirs)):
        led, brk = mine.get(key), theirs.get(key)
        crypto = ((brk is not None and brk.asset_class == AssetClass.CRYPTO)
                  or (led is not None and is_crypto_symbol(led.symbol)))
        tol = CRYPTO_QTY_TOL if crypto else EQUITY_QTY_TOL
        if led is not None and brk is not None:
            if abs(led.qty - brk.qty) > tol:
                out[key] = (f"{brk.symbol}: quantity mismatch, ledger {led.qty:g} ({led.strategy}) "
                            f"vs broker {brk.qty:g}")
        elif brk is not None:
            if abs(brk.qty) > tol:
                out[key] = f"{brk.symbol}: broker holds {brk.qty:g} but the ledger has no such position"
        elif abs(led.qty) > tol:
            out[key] = f"{led.symbol}: ledger holds {led.qty:g} ({led.strategy}) but the broker has no such position"
    return out


def reconcile(ledger: Ledger | sqlite3.Connection, snapshot: AccountSnapshot) -> list[str]:
    """Return human-readable mismatch strings; an empty list means the ledger
    and the broker agree on every symbol."""
    return list(mismatches(ledger, snapshot).values())


# ---- daily summary: Alpaca paper outcome vs the desk's own plan ----------------------------

ET = ZoneInfo("America/New_York")
SLIPPAGE_BPS_FLAG = 100.0  # adverse fill vs the plan reference price; plan-time reference, so overnight gaps count
STOP_PRICE_TOL = 0.011     # broker stops are rounded to cents
ALERT_DIFF_DAYS = 3        # consecutive days with any difference before the alert fires
_MISSED = ("canceled", "expired", "rejected", "not_sent", "pending_submit")

SCHEMA = """CREATE TABLE IF NOT EXISTS reconcile_daily (
  day TEXT PRIMARY KEY, n_fills INTEGER, n_orders INTEGER, n_slip_flag INTEGER, n_missed INTEGER,
  n_partial INTEGER, n_stop_diff INTEGER, mean_slip_bps REAL, zero_diff INTEGER, detail_json TEXT,
  recorded_at TEXT)"""


def ensure_schema(con: sqlite3.Connection) -> None:
    con.execute(SCHEMA)


def _tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _et_day(ts: str | None) -> str | None:
    if not ts:
        return None
    d = datetime.fromisoformat(ts)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(ET).date().isoformat()


def _slip_bps(side: str, ref: float, price: float) -> float:
    """Signed cost in basis points; positive = worse than the reference."""
    return ((price - ref) if side == Side.BUY.value else (ref - price)) / ref * 1e4


def summarize(con: sqlite3.Connection, day: str, open_orders: list[BrokerOrder] | None = None) -> dict:
    """Pure read. ``open_orders`` = the broker's live orders (None: the stop check is skipped and said so)."""
    have = _tables(con)
    near = (day, (date.fromisoformat(day) + timedelta(days=1)).isoformat())
    marks = ",".join("?" * len(near))
    slips: list[dict] = []
    if {"fills", "submitted"} <= have:
        ref_col = "p.ref_price" if "planned_orders" in have else "NULL"
        join = "LEFT JOIN planned_orders p ON p.client_order_id=f.client_order_id" if "planned_orders" in have else ""
        for r in con.execute(
                f"SELECT f.client_order_id cid, f.symbol, f.side, f.price, f.filled_at, s.kind, s.stop AS s_stop, "
                f"{ref_col} AS ref FROM fills f LEFT JOIN submitted s ON s.client_order_id=f.client_order_id {join} "
                f"WHERE substr(f.filled_at,1,10) IN ({marks})", near):
            if _et_day(r["filled_at"]) != day or r["kind"] not in ("entry", "exit", "stop"):
                continue
            ref = r["s_stop"] if r["kind"] == "stop" else r["ref"]
            if not ref or ref <= 0:
                continue
            slips.append({"cid": r["cid"], "symbol": r["symbol"], "side": r["side"], "kind": r["kind"],
                          "ref": float(ref), "price": float(r["price"]),
                          "bps": _slip_bps(r["side"], float(ref), float(r["price"]))})
    n_fills = 0
    missed: list[dict] = []
    partial: list[dict] = []
    n_orders = 0
    if "submitted" in have:
        if "fills" in have:
            n_fills = sum(1 for r in con.execute(f"SELECT filled_at FROM fills WHERE substr(filled_at,1,10) IN ({marks})", near)
                          if _et_day(r["filled_at"]) == day)
        for r in con.execute(
                f"SELECT client_order_id cid, kind, symbol, side, qty, status FROM submitted "
                f"WHERE kind IN ('entry','exit') AND substr(submitted_at,1,10) IN ({marks})", near):
            row = con.execute("SELECT submitted_at FROM submitted WHERE client_order_id=?", (r["cid"],)).fetchone()
            if _et_day(row["submitted_at"]) != day:
                continue
            n_orders += 1
            got = con.execute("SELECT qty FROM fills WHERE client_order_id=?", (r["cid"],)).fetchone() if "fills" in have else None
            filled = float(got["qty"]) if got else 0.0
            tol = float(r["qty"]) * 1e-6 + 1e-9
            item = {"cid": r["cid"], "symbol": r["symbol"], "kind": r["kind"], "side": r["side"],
                    "qty": float(r["qty"]), "filled": filled, "status": r["status"]}
            if r["status"] == "filled":
                if filled < float(r["qty"]) - tol:
                    partial.append(item)
            elif r["status"] in _MISSED:
                (partial if filled > tol else missed).append(item)
    stop_diffs: list[dict] = []
    if open_orders is not None and "sleeve_positions" in have:
        stops_by: dict[str, list[BrokerOrder]] = defaultdict(list)
        for o in open_orders:
            if o.side == Side.SELL and o.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
                stops_by[norm(o.symbol)].append(o)
        for r in con.execute("SELECT symbol, qty, stop FROM sleeve_positions WHERE qty > 0"):
            mine = stops_by.pop(norm(r["symbol"]), [])
            if is_crypto_symbol(r["symbol"]) or r["stop"] is None:
                continue
            if not mine:
                stop_diffs.append({"symbol": r["symbol"], "problem": f"券商沒有停損單(帳本停損 {r['stop']:g})"})
            elif len(mine) > 1:
                stop_diffs.append({"symbol": r["symbol"], "problem": f"券商同時有 {len(mine)} 張停損單"})
            elif abs((mine[0].stop_price or 0.0) - r["stop"]) > STOP_PRICE_TOL:
                stop_diffs.append({"symbol": r["symbol"],
                                   "problem": f"停損價不同:帳本 {r['stop']:g} vs 券商 {mine[0].stop_price or 0.0:g}"})
        for key, orders in sorted(stops_by.items()):
            stop_diffs.append({"symbol": orders[0].symbol, "problem": "券商有停損單但帳本無此持倉(孤兒停損)"})
    flagged = [x for x in slips if x["bps"] > SLIPPAGE_BPS_FLAG]
    n_diff = len(flagged) + len(missed) + len(partial) + len(stop_diffs)
    return {"day": day, "n_fills": n_fills, "n_orders": n_orders, "slips": slips, "slip_flags": flagged,
            "mean_slip_bps": (sum(x["bps"] for x in slips) / len(slips)) if slips else None,
            "missed": missed, "partial": partial, "stop_diffs": stop_diffs,
            "stop_checked": open_orders is not None, "n_diff": n_diff, "zero_diff": n_diff == 0}


def streaks(con: sqlite3.Connection, summary: dict) -> dict:
    """Run length ending at ``summary['day']``: {'zero': consecutive zero-difference days,
    'diff': consecutive days with a difference}; one of them is always 0. Earlier days come from
    ``reconcile_daily`` (a day the job did not run is simply not counted)."""
    flags = [1 if summary["zero_diff"] else 0]
    if "reconcile_daily" in _tables(con):
        flags += [r["zero_diff"] for r in con.execute(
            "SELECT zero_diff FROM reconcile_daily WHERE day < ? ORDER BY day DESC LIMIT 400", (summary["day"],))]
    n = 0
    for f in flags:
        if f != flags[0]:
            break
        n += 1
    return {"zero": n if flags[0] == 1 else 0, "diff": n if flags[0] == 0 else 0}


def record(con: sqlite3.Connection, summary: dict) -> None:
    """Upsert the day's row (a re-run replaces it). Does not commit."""
    ensure_schema(con)
    detail = {k: summary[k] for k in ("slip_flags", "missed", "partial", "stop_diffs", "stop_checked")}
    con.execute(
        "INSERT INTO reconcile_daily (day, n_fills, n_orders, n_slip_flag, n_missed, n_partial, n_stop_diff, "
        "mean_slip_bps, zero_diff, detail_json, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(day) DO UPDATE SET n_fills=excluded.n_fills, n_orders=excluded.n_orders, "
        "n_slip_flag=excluded.n_slip_flag, n_missed=excluded.n_missed, n_partial=excluded.n_partial, "
        "n_stop_diff=excluded.n_stop_diff, mean_slip_bps=excluded.mean_slip_bps, zero_diff=excluded.zero_diff, "
        "detail_json=excluded.detail_json, recorded_at=excluded.recorded_at",
        (summary["day"], summary["n_fills"], summary["n_orders"], len(summary["slip_flags"]), len(summary["missed"]),
         len(summary["partial"]), len(summary["stop_diffs"]), summary["mean_slip_bps"], int(summary["zero_diff"]),
         json.dumps(detail, ensure_ascii=False), store.now()))


def render(summary: dict, run: dict) -> str:
    s = summary
    slip = "無可比對的成交" if s["mean_slip_bps"] is None else f"平均不利滑價 {s['mean_slip_bps']:+.1f} bps"
    lines = [f"## 券商對帳摘要 {s['day']}(Alpaca 紙上成交 vs 內部計畫)", "",
             f"- 成交 {s['n_fills']} 筆,{slip}(單筆超過 {SLIPPAGE_BPS_FLAG:g} bps 才算差異);"
             f"今日送出進出場單 {s['n_orders']} 張。",
             f"- 連續零差異 {run['zero']} 天" + (f";連續有差異 {run['diff']} 天" if run["diff"] else "") + "。"]
    if not s["stop_checked"]:
        lines.append("- 停損單比對略過:沒有取得券商未成交單。")
    if s["zero_diff"]:
        lines.append("- 今日沒有差異。")
    for x in s["slip_flags"]:
        lines.append(f"- 滑價 {x['symbol']} {x['kind']} {x['side']}:參考 {x['ref']:g} 成交 {x['price']:g}({x['bps']:+.0f} bps)")
    for x in s["missed"]:
        lines.append(f"- 未成交 {x['symbol']} {x['kind']} {x['side']} {x['qty']:g} 股(狀態 {x['status']})")
    for x in s["partial"]:
        lines.append(f"- 部分成交 {x['symbol']} {x['kind']} {x['side']}:計畫 {x['qty']:g},成交 {x['filled']:g}(狀態 {x['status']})")
    for x in s["stop_diffs"]:
        lines.append(f"- 停損差異 {x['symbol']}:{x['problem']}")
    return "\n".join(lines)


def alert_message(summary: dict, run: dict) -> str | None:
    """Text for the push when differences have run for ALERT_DIFF_DAYS days in a row, else None."""
    if run["diff"] < ALERT_DIFF_DAYS:
        return None
    return f"券商對帳連續 {run['diff']} 天有差異(門檻 {ALERT_DIFF_DAYS} 天)。\n" + render(summary, run)


def daily(con: sqlite3.Connection, day: str, open_orders: list[BrokerOrder] | None = None,
          record_rows: bool = True) -> tuple[str, str | None]:
    """Summarize, optionally persist (caller commits), return (report text, alert text or None)."""
    s = summarize(con, day, open_orders)
    run = streaks(con, s)
    if record_rows:
        record(con, s)
    return render(s, run), alert_message(s, run)


def history(con: sqlite3.Connection, limit: int = 20) -> list[dict]:
    ensure_schema(con)
    return [dict(r) for r in con.execute("SELECT * FROM reconcile_daily ORDER BY day DESC LIMIT ?", (limit,))]
