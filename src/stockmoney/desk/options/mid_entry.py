"""Mid-price entry shadow comparison (owner 2026-11-05, EDGE_PLAN 7.4 item 2).
Shadow only: nothing here sends, sizes or blocks an order, and no live path
reads it. The owner decides the switch from these numbers (30+ resolved
comparisons per underlying class, reported first).

At every shadow entry of the live exit variant (position_id
``<setup>:shadow:<dte>:<live_exit>``) the engine records a hypothetical buy
limit at the mid of the entry quote. The row is then resolved from the
quotes the engine already takes of that position (no extra data call):

- FILLED: a fresh quote within ``cfg.mid_entry_wait_s`` has ask <= the limit
  (a conservative touch rule: a resting mid order may fill earlier on a
  print between bid and ask, which this does not count). Fill = the limit.
- FALLBACK: no touch by the wait; the order is replaced at the ask of the
  first fresh quote at or after the wait. Fill = that ask (can be above the
  original ask: the cost of waiting).
- NO_QUOTE: no fresh quote by twice the wait, or the position exited first;
  left out of the comparison.

saved_per_contract = signal ask - fill (premium per share; x multiplier for
dollars). The linked position's P&L is reported per outcome, so adverse
selection (fills that come because the option fell) is visible.
"""
from __future__ import annotations

from datetime import datetime

from stockmoney.desk.options import store as ostore
from stockmoney.desk.options._iface import OptionContract
from stockmoney.desk.options.config import OptionsConfig

VARIANT = "mid_entry"
PENDING, FILLED, FALLBACK, NO_QUOTE = "PENDING", "FILLED", "FALLBACK", "NO_QUOTE"
MIN_PER_CLASS = 30  # owner 2026-11-05: switch is discussed only after 30 resolved comparisons per class


def mid_limit(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return None
    return round((bid + ask) / 2, 2)


def get(con, position_id: str) -> dict | None:
    r = con.execute("SELECT * FROM options_mid_entry WHERE position_id=?", (position_id,)).fetchone()
    return dict(r) if r is not None else None


def open_row(con, linked: dict, contract: OptionContract, now: datetime) -> bool:
    """Record the hypothetical mid order for the shadow row ``linked``."""
    lim = mid_limit(contract.bid, contract.ask)
    if lim is None or get(con, linked["position_id"]) is not None:
        return False
    con.execute(
        "INSERT OR IGNORE INTO options_mid_entry (position_id, day, source, underlying, uclass, dte_label, occ, "
        "signal_time, signal_bid, signal_ask, mid_limit, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (linked["position_id"], linked["day"], linked["source"], linked["underlying"], linked["uclass"],
         linked["dte_label"], contract.occ, ostore.iso(now), contract.bid, contract.ask, lim, PENDING))
    return True


def resolve(con, cfg: OptionsConfig, row: dict, q: OptionContract | None, fresh: bool, now: datetime,
            exited: bool = False) -> str:
    """Advance one PENDING row with this tick's quote of its contract."""
    if row["status"] != PENDING:
        return row["status"]
    waited = (now - datetime.fromisoformat(row["signal_time"])).total_seconds()
    status, fill = PENDING, None
    if fresh and q is not None and q.ask is not None and q.ask <= row["mid_limit"] and waited > 0:
        status, fill = FILLED, row["mid_limit"]
    elif fresh and q is not None and q.ask is not None and waited >= cfg.mid_entry_wait_s:
        status, fill = FALLBACK, q.ask
    elif exited or waited >= 2 * cfg.mid_entry_wait_s:
        status = NO_QUOTE
    if status == PENDING:
        return status
    saved = None if fill is None else round(row["signal_ask"] - fill, 4)
    con.execute("UPDATE options_mid_entry SET status=?, resolved_at=?, waited_s=?, fill_price=?, "
                "saved_per_contract=? WHERE position_id=?",
                (status, ostore.iso(now), waited, fill, saved, row["position_id"]))
    return status


def pending_rows(con, day: str) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM options_mid_entry WHERE day=? AND status=?", (day, PENDING))]


def summary(con, cfg: OptionsConfig | None = None) -> list[dict]:
    """Per uclass: resolved n, fill rate, mean saving in dollars per contract,
    and the mean P&L of the linked shadow leg for filled vs fallback rows."""
    m = (cfg or OptionsConfig()).contract_multiplier
    rows = con.execute(
        "SELECT e.uclass, e.status, e.saved_per_contract, p.realized_usd, p.fees_usd, p.qty_initial, p.state "
        "FROM options_mid_entry e LEFT JOIN options_positions p ON p.position_id = e.position_id "
        "WHERE e.status IN (?, ?)", (FILLED, FALLBACK)).fetchall()
    out: dict[str, dict] = {}
    for uclass, status, saved, realized, fees, qty, state in rows:
        g = out.setdefault(uclass or "?", {"uclass": uclass or "?", "n": 0, "filled": 0, "saved": [],
                                           "pnl_filled": [], "pnl_fallback": []})
        g["n"] += 1
        g["filled"] += status == FILLED
        if saved is not None:
            g["saved"].append(saved * m)
        if state == "EXITED" and qty:
            (g["pnl_filled"] if status == FILLED else g["pnl_fallback"]).append(
                ((realized or 0.0) - (fees or 0.0)) / qty)
    res = []
    for g in sorted(out.values(), key=lambda x: x["uclass"]):
        avg = (lambda xs: sum(xs) / len(xs) if xs else None)
        res.append({"uclass": g["uclass"], "n": g["n"], "fill_rate": g["filled"] / g["n"],
                    "saved_usd": avg(g["saved"]), "pnl_filled": avg(g["pnl_filled"]),
                    "pnl_fallback": avg(g["pnl_fallback"]), "ready": g["n"] >= MIN_PER_CLASS})
    return res


def report_lines(con, cfg: OptionsConfig | None = None) -> list[str]:
    rs = summary(con, cfg)
    if not rs:
        return []
    f = (lambda x: "n/a" if x is None else f"{x:+.2f}")
    lines = ["", f"中價進場影子({VARIANT},每口美元;樣本滿 {MIN_PER_CLASS} 筆才討論切換):",
             "| 類別 | n | 中價成交率 | 平均每口省下 | 每口損益(中價成交) | 每口損益(改掛賣價) | 樣本 |",
             "|---|---|---|---|---|---|---|"]
    for r in rs:
        lines.append(f"| {r['uclass']} | {r['n']} | {r['fill_rate']:.0%} | {f(r['saved_usd'])} | {f(r['pnl_filled'])} "
                     f"| {f(r['pnl_fallback'])} | {'足夠' if r['ready'] else '不足'} |")
    return lines
