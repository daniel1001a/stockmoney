"""Rebuild closed round-trip trades from the ledger's fills (read-only)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
EPS = 1e-9


@dataclass
class ClosedTrade:
    trade_id: str
    symbol: str
    strategy: str
    entry_at: str
    exit_at: str
    entry_day: str
    exit_day: str
    qty: float
    entry_px: float
    exit_px: float
    pnl: float
    ret: float
    exit_kind: str = "unknown"  # stop | exit | unknown
    exit_reason: str = ""
    initial_stop: float | None = None
    signal_id: str = ""
    plan_ref_price: float | None = None


def et_day(ts: str | None) -> str:
    if not ts:
        return ""
    d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return str(d.astimezone(ET).date())


def _table_exists(con, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def closed_trades(con) -> list[ClosedTrade]:
    """One ClosedTrade per flat-to-flat round trip, in exit order. Stock and
    crypto both; an open position is not a trade yet."""
    if not _table_exists(con, "fills"):
        return []
    rows = con.execute(
        "SELECT client_order_id, symbol, strategy, side, qty, price, filled_at, realized_pnl "
        "FROM fills ORDER BY filled_at, id").fetchall()
    open_: dict[str, dict] = {}
    out: list[ClosedTrade] = []
    for r in rows:
        s = open_.get(r["symbol"])
        if r["side"] == "buy":
            if s is None:
                s = open_[r["symbol"]] = {"strategy": r["strategy"], "entry_at": r["filled_at"], "bought": 0.0,
                                          "cost": 0.0, "held": 0.0, "sold": 0.0, "proceeds": 0.0, "pnl": 0.0}
            s["bought"] += r["qty"]
            s["cost"] += r["qty"] * r["price"]
            s["held"] += r["qty"]
        elif s is not None:
            s["sold"] += r["qty"]
            s["proceeds"] += r["qty"] * r["price"]
            s["held"] -= r["qty"]
            s["pnl"] += r["realized_pnl"]
            s["last_cid"] = r["client_order_id"]
            s["exit_at"] = r["filled_at"]
            if s["held"] <= EPS:
                del open_[r["symbol"]]
                entry_px = s["cost"] / s["bought"]
                exit_px = s["proceeds"] / s["sold"]
                tid = hashlib.sha1(f"{r['symbol']}|{s['entry_at']}|{s['exit_at']}".encode()).hexdigest()[:16]
                out.append(ClosedTrade(
                    tid, r["symbol"], s["strategy"], s["entry_at"], s["exit_at"], et_day(s["entry_at"]),
                    et_day(s["exit_at"]), s["bought"], entry_px, exit_px, s["pnl"],
                    exit_px / entry_px - 1, exit_reason=s["last_cid"]))
    for t in out:
        _link(con, t)
    return out


def _link(con, t: ClosedTrade) -> None:
    """Attach what the system planned (stop, signal, exit reason) to the trade."""
    cid = t.exit_reason  # temporarily the last sell's client_order_id
    t.exit_reason = ""
    if _table_exists(con, "submitted"):
        r = con.execute("SELECT kind FROM submitted WHERE client_order_id=?", (cid.split("#")[0],)).fetchone()
        if r:
            t.exit_kind = "stop" if r["kind"] == "stop" else "exit"
    if _table_exists(con, "planned_orders"):
        r = con.execute("SELECT reason FROM planned_orders WHERE client_order_id=?", (cid.split("#")[0],)).fetchone()
        if r is None:
            r = con.execute("SELECT reason FROM planned_orders WHERE symbol=? AND side='sell' AND plan_date<=? "
                            "ORDER BY plan_date DESC LIMIT 1", (t.symbol, t.exit_day)).fetchone()
        if r and r["reason"]:
            t.exit_reason = r["reason"]
        b = con.execute("SELECT stop, ref_price, signal_id FROM planned_orders WHERE symbol=? AND side='buy' "
                        "AND strategy=? AND plan_date<? ORDER BY plan_date DESC LIMIT 1",
                        (t.symbol, t.strategy, t.entry_day)).fetchone()
        if b:
            t.initial_stop, t.plan_ref_price, t.signal_id = b["stop"], b["ref_price"], b["signal_id"] or ""
    if t.exit_kind == "stop" and not t.exit_reason:
        t.exit_reason = "觸發停損"
